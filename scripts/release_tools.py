#!/usr/bin/env python3
"""Deterministic release build, verification, download, and installation tools."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote


MANIFEST_NAME = "release-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS.txt"
CONTRACT_PATH = "release/contract.json"
SEMVER_NUMBER = r"(?:0|[1-9][0-9]*)"
SEMVER_IDENTIFIER = (
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
RELEASE_PATTERN = re.compile(
    rf"^v{SEMVER_NUMBER}\.{SEMVER_NUMBER}\.{SEMVER_NUMBER}"
    rf"(?:-{SEMVER_IDENTIFIER}(?:\.{SEMVER_IDENTIFIER})*)?$"
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class ReleaseError(RuntimeError):
    """Raised when a release violates the release contract."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo_root: Path, *arguments: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=False,
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseError(f"git {' '.join(arguments)} failed: {detail}")
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8").strip()


def resolve_source(repo_root: Path, source_ref: str) -> tuple[str, int]:
    commit = str(_git(repo_root, "rev-parse", f"{source_ref}^{{commit}}"))
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseError(f"Git resolved an invalid commit: {commit}")
    epoch_text = str(_git(repo_root, "show", "-s", "--format=%ct", commit))
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise ReleaseError(f"Git returned an invalid commit timestamp: {epoch_text}") from error
    return commit, epoch


def git_blob(repo_root: Path, commit: str, relative_path: str) -> tuple[bytes, int]:
    validate_repo_path(relative_path)
    listing = str(_git(repo_root, "ls-tree", commit, "--", relative_path))
    lines = listing.splitlines()
    if len(lines) != 1 or "\t" not in lines[0]:
        raise ReleaseError(f"Required tracked file is missing at {commit}: {relative_path}")
    metadata, listed_path = lines[0].split("\t", 1)
    mode, object_type, _object_id = metadata.split()
    if listed_path != relative_path or object_type != "blob":
        raise ReleaseError(f"Expected a regular tracked file: {relative_path}")
    if mode == "120000":
        raise ReleaseError(f"Symbolic links are not allowed in releases: {relative_path}")
    if mode == "100755":
        file_mode = 0o755
    elif mode == "100644":
        file_mode = 0o644
    else:
        raise ReleaseError(f"Unsupported Git mode {mode} for {relative_path}")
    content = _git(repo_root, "show", f"{commit}:{relative_path}", binary=True)
    assert isinstance(content, bytes)
    return content, file_mode


def validate_release(release: str) -> None:
    if (
        len(release) > 128
        or release.casefold().endswith(".lock")
        or not RELEASE_PATTERN.fullmatch(release)
    ):
        raise ReleaseError(
            "Release must match vMAJOR.MINOR.PATCH with an optional prerelease suffix."
        )
    try:
        ref_check = subprocess.run(
            ["git", "check-ref-format", f"refs/tags/{release}"],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise ReleaseError("Git is required to validate release tags.") from error
    if ref_check.returncode != 0:
        raise ReleaseError("Release is not a valid Git tag.")


def validate_repo_path(path: str) -> None:
    if (
        not path
        or "\0" in path
        or "\\" in path
        or path.startswith("/")
        or DRIVE_PATTERN.match(path)
    ):
        raise ReleaseError(f"Unsafe repository path: {path!r}")
    parts = PurePosixPath(path).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ReleaseError(f"Unsafe repository path: {path!r}")
    if str(PurePosixPath(*parts)) != path:
        raise ReleaseError(f"Repository path is not normalized: {path!r}")


def validate_archive_path(path: str) -> tuple[str, ...]:
    validate_repo_path(path)
    return PurePosixPath(path).parts


def _strict_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseError(f"{context} keys differ: missing={missing}, extra={extra}")


def validate_contract(contract: dict[str, Any]) -> None:
    _strict_keys(
        contract,
        {
            "archiveRoot",
            "forbiddenPathSegments",
            "includedFiles",
            "maxArchiveBytes",
            "maxEntryBytes",
            "product",
            "requiredFiles",
            "schemaVersion",
        },
        "contract",
    )
    if contract["schemaVersion"] != 1:
        raise ReleaseError("Unsupported contract schema.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", contract["product"]):
        raise ReleaseError("Contract product is invalid.")
    root = contract["archiveRoot"]
    if not isinstance(root, str) or "/" in root or "\\" in root:
        raise ReleaseError("archiveRoot must be one safe path segment.")
    validate_repo_path(root)
    for key in ("includedFiles", "requiredFiles", "forbiddenPathSegments"):
        if not isinstance(contract[key], list) or not all(
            isinstance(item, str) for item in contract[key]
        ):
            raise ReleaseError(f"{key} must be an array of strings.")
    included = contract["includedFiles"]
    required = contract["requiredFiles"]
    if len(set(included)) != len(included) or len(set(required)) != len(required):
        raise ReleaseError("Contract paths must be unique.")
    for path in included + required:
        validate_repo_path(path)
    forbidden = {segment.casefold() for segment in contract["forbiddenPathSegments"]}
    for path in included:
        if any(part.casefold() in forbidden for part in PurePosixPath(path).parts):
            raise ReleaseError(f"Included path contains a forbidden segment: {path}")
    for key in ("maxArchiveBytes", "maxEntryBytes"):
        if not isinstance(contract[key], int) or contract[key] <= 0:
            raise ReleaseError(f"{key} must be a positive integer.")


def load_contract(repo_root: Path, commit: str) -> tuple[dict[str, Any], bytes]:
    raw, _mode = git_blob(repo_root, commit, CONTRACT_PATH)
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ReleaseError("Release contract is not valid JSON.") from error
    if not isinstance(contract, dict):
        raise ReleaseError("Release contract must be a JSON object.")
    validate_contract(contract)
    return contract, raw


def _zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    value = datetime.fromtimestamp(epoch, timezone.utc)
    if value.year < 1980:
        value = datetime(1980, 1, 1, tzinfo=timezone.utc)
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second - value.second % 2,
    )


def _zip_info(name: str, mode: int, epoch: int) -> zipfile.ZipInfo:
    validate_archive_path(name)
    info = zipfile.ZipInfo(name, _zip_timestamp(epoch))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _copy_regular_file(source: Path, destination: Path) -> None:
    if _is_link_or_junction(source):
        raise ReleaseError(f"Release asset must not be a link: {source.name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ReleaseError(f"Cannot open release asset {source.name}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseError(f"Release asset is not a regular file: {source.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as input_stream:
            with destination.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        os.chmod(destination, 0o600)
    finally:
        os.close(descriptor)


def _stage_release_assets(source_dir: Path, destination_dir: Path) -> None:
    source_dir = _absolute_path(source_dir)
    if not source_dir.is_dir() or _is_link_or_junction(source_dir):
        raise ReleaseError("Release input must be a real directory.")
    _copy_regular_file(source_dir / MANIFEST_NAME, destination_dir / MANIFEST_NAME)
    manifest = _load_manifest(destination_dir / MANIFEST_NAME)
    validate_manifest(manifest)
    artifact_name = manifest["artifact"]["name"]
    _copy_regular_file(source_dir / CHECKSUMS_NAME, destination_dir / CHECKSUMS_NAME)
    _copy_regular_file(source_dir / artifact_name, destination_dir / artifact_name)


def build_release(
    repo_root: Path,
    output_dir: Path,
    release: str,
    repository: str,
    source_ref: str = "HEAD",
    force: bool = False,
) -> dict[str, Any]:
    validate_release(release)
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseError("Repository must use the OWNER/REPOSITORY form.")
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    commit, epoch = resolve_source(repo_root, source_ref)
    contract, contract_raw = load_contract(repo_root, commit)
    archive_name = f"managed-cli-demo_{release}.zip"
    expected_outputs = {
        archive_name,
        MANIFEST_NAME,
        CHECKSUMS_NAME,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in expected_outputs:
        target = output_dir / name
        if target.exists() and not force:
            raise ReleaseError(f"Output already exists: {target}")
        if target.is_symlink():
            raise ReleaseError(f"Output must not be a symbolic link: {target}")

    root = contract["archiveRoot"]
    entries: list[tuple[str, bytes, int]] = []
    seen_casefold: set[str] = set()
    for relative_path in sorted(contract["includedFiles"]):
        content, mode = git_blob(repo_root, commit, relative_path)
        if len(content) > contract["maxEntryBytes"]:
            raise ReleaseError(f"Included file is too large: {relative_path}")
        archive_path = f"{root}/{relative_path}"
        folded = archive_path.casefold()
        if folded in seen_casefold:
            raise ReleaseError(f"Case-insensitive path collision: {archive_path}")
        seen_casefold.add(folded)
        entries.append((archive_path, content, mode))

    release_info = canonical_json(
        {
            "product": contract["product"],
            "release": release,
            "schemaVersion": 1,
            "sourceCommit": commit,
            "sourceRepository": repository,
        }
    )
    entries.append((f"{root}/release-info.json", release_info, 0o644))
    entries.sort(key=lambda item: item[0])

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", allowZip64=False) as archive:
        for archive_path, content, mode in entries:
            archive.writestr(_zip_info(archive_path, mode, epoch), content)
    archive_bytes = archive_buffer.getvalue()
    if len(archive_bytes) > contract["maxArchiveBytes"]:
        raise ReleaseError("Built archive exceeds the contract size limit.")

    file_records = [
        {
            "mode": f"{mode:04o}",
            "path": archive_path,
            "sha256": sha256_bytes(content),
            "sizeBytes": len(content),
        }
        for archive_path, content, mode in entries
    ]
    generated_at = (
        datetime.fromtimestamp(epoch, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    manifest = {
        "artifact": {
            "entryCount": len(entries),
            "format": "zip-store-v1",
            "name": archive_name,
            "sha256": sha256_bytes(archive_bytes),
            "sizeBytes": len(archive_bytes),
        },
        "contractSha256": sha256_bytes(contract_raw),
        "files": file_records,
        "generatedAt": generated_at,
        "limits": {
            "maxArchiveBytes": contract["maxArchiveBytes"],
            "maxEntryBytes": contract["maxEntryBytes"],
        },
        "product": contract["product"],
        "release": release,
        "requiredFiles": [
            f"{root}/{path}" for path in sorted(contract["requiredFiles"])
        ],
        "schemaVersion": 1,
        "source": {
            "commit": commit,
            "repository": repository,
        },
    }
    manifest_bytes = canonical_json(manifest)
    checksums = (
        f"{sha256_bytes(archive_bytes)}  {archive_name}\n"
        f"{sha256_bytes(manifest_bytes)}  {MANIFEST_NAME}\n"
    ).encode("ascii")

    _write_atomic(output_dir / archive_name, archive_bytes)
    _write_atomic(output_dir / MANIFEST_NAME, manifest_bytes)
    _write_atomic(output_dir / CHECKSUMS_NAME, checksums)
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Cannot read {MANIFEST_NAME}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError("Release manifest must be a JSON object.")
    return value


def validate_manifest(manifest: dict[str, Any]) -> None:
    _strict_keys(
        manifest,
        {
            "artifact",
            "contractSha256",
            "files",
            "generatedAt",
            "limits",
            "product",
            "release",
            "requiredFiles",
            "schemaVersion",
            "source",
        },
        "manifest",
    )
    if manifest["schemaVersion"] != 1:
        raise ReleaseError("Unsupported manifest schema.")
    if manifest["product"] != "managed-cli-release-demo":
        raise ReleaseError("Unexpected product identity.")
    validate_release(manifest["release"])
    if not SHA256_PATTERN.fullmatch(manifest["contractSha256"]):
        raise ReleaseError("Invalid contract digest.")
    try:
        datetime.fromisoformat(manifest["generatedAt"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ReleaseError("Invalid generatedAt timestamp.") from error

    source = manifest["source"]
    if not isinstance(source, dict):
        raise ReleaseError("source must be an object.")
    _strict_keys(source, {"commit", "repository"}, "source")
    if not COMMIT_PATTERN.fullmatch(source["commit"]):
        raise ReleaseError("Invalid source commit.")
    if not REPOSITORY_PATTERN.fullmatch(source["repository"]):
        raise ReleaseError("Invalid source repository.")

    limits = manifest["limits"]
    if not isinstance(limits, dict):
        raise ReleaseError("limits must be an object.")
    _strict_keys(limits, {"maxArchiveBytes", "maxEntryBytes"}, "limits")
    for key in limits:
        if not isinstance(limits[key], int) or limits[key] <= 0:
            raise ReleaseError(f"Invalid {key}.")

    artifact = manifest["artifact"]
    if not isinstance(artifact, dict):
        raise ReleaseError("artifact must be an object.")
    _strict_keys(
        artifact,
        {"entryCount", "format", "name", "sha256", "sizeBytes"},
        "artifact",
    )
    expected_name = f"managed-cli-demo_{manifest['release']}.zip"
    if artifact["name"] != expected_name:
        raise ReleaseError("Artifact name does not match the release.")
    if artifact["format"] != "zip-store-v1":
        raise ReleaseError("Unsupported archive format.")
    if not SHA256_PATTERN.fullmatch(artifact["sha256"]):
        raise ReleaseError("Invalid artifact digest.")
    if (
        not isinstance(artifact["sizeBytes"], int)
        or artifact["sizeBytes"] <= 0
        or artifact["sizeBytes"] > limits["maxArchiveBytes"]
    ):
        raise ReleaseError("Invalid artifact size.")
    if not isinstance(artifact["entryCount"], int) or artifact["entryCount"] <= 0:
        raise ReleaseError("Invalid artifact entry count.")

    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ReleaseError("files must be a non-empty array.")
    if artifact["entryCount"] != len(files):
        raise ReleaseError("Artifact entry count does not match files.")
    exact: set[str] = set()
    folded: set[str] = set()
    for index, file_record in enumerate(files):
        if not isinstance(file_record, dict):
            raise ReleaseError(f"files[{index}] must be an object.")
        _strict_keys(
            file_record,
            {"mode", "path", "sha256", "sizeBytes"},
            f"files[{index}]",
        )
        path = file_record["path"]
        validate_archive_path(path)
        if path in exact or path.casefold() in folded:
            raise ReleaseError(f"Duplicate or case-colliding manifest path: {path}")
        exact.add(path)
        folded.add(path.casefold())
        if file_record["mode"] not in ("0644", "0755"):
            raise ReleaseError(f"Unsupported mode for {path}.")
        if not SHA256_PATTERN.fullmatch(file_record["sha256"]):
            raise ReleaseError(f"Invalid digest for {path}.")
        if (
            not isinstance(file_record["sizeBytes"], int)
            or file_record["sizeBytes"] < 0
            or file_record["sizeBytes"] > limits["maxEntryBytes"]
        ):
            raise ReleaseError(f"Invalid size for {path}.")

    required = manifest["requiredFiles"]
    if not isinstance(required, list) or not all(
        isinstance(path, str) for path in required
    ):
        raise ReleaseError("requiredFiles must be an array of strings.")
    for path in required:
        validate_archive_path(path)
    if not set(required).issubset(exact):
        raise ReleaseError("The manifest omits one or more required files.")


def parse_checksums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as error:
        raise ReleaseError(f"Cannot read {CHECKSUMS_NAME}: {error}") from error
    checksums: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match:
            raise ReleaseError(f"Malformed checksum line: {line!r}")
        digest, name = match.groups()
        if name in checksums:
            raise ReleaseError(f"Duplicate checksum entry: {name}")
        checksums[name] = digest
    return checksums


def _entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def verify_release_dir(dist_dir: Path) -> dict[str, Any]:
    dist_dir = dist_dir.resolve()
    if not dist_dir.is_dir() or dist_dir.is_symlink():
        raise ReleaseError("Release input must be a real directory.")
    manifest_path = dist_dir / MANIFEST_NAME
    checksums_path = dist_dir / CHECKSUMS_NAME
    manifest = _load_manifest(manifest_path)
    validate_manifest(manifest)
    artifact_name = manifest["artifact"]["name"]
    expected_names = {artifact_name, MANIFEST_NAME, CHECKSUMS_NAME}
    actual_names = {
        item.name
        for item in dist_dir.iterdir()
        if item.is_file() and not item.is_symlink()
    }
    if actual_names != expected_names or any(
        item.is_dir() or item.is_symlink() for item in dist_dir.iterdir()
    ):
        raise ReleaseError(
            f"Release directory differs: expected={sorted(expected_names)}, "
            f"actual={sorted(item.name for item in dist_dir.iterdir())}"
        )

    checksums = parse_checksums(checksums_path)
    if set(checksums) != {artifact_name, MANIFEST_NAME}:
        raise ReleaseError("Checksum file must cover exactly the ZIP and manifest.")
    for name, expected_digest in checksums.items():
        if sha256_file(dist_dir / name) != expected_digest:
            raise ReleaseError(f"Checksum mismatch: {name}")

    artifact_path = dist_dir / artifact_name
    artifact = manifest["artifact"]
    if artifact_path.stat().st_size != artifact["sizeBytes"]:
        raise ReleaseError("Artifact size does not match the manifest.")
    if sha256_file(artifact_path) != artifact["sha256"]:
        raise ReleaseError("Artifact digest does not match the manifest.")

    records = {record["path"]: record for record in manifest["files"]}
    exact: set[str] = set()
    folded: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(artifact_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) != artifact["entryCount"]:
                raise ReleaseError("ZIP entry count does not match the manifest.")
            for info in infos:
                path = info.filename
                validate_archive_path(path)
                if path in exact or path.casefold() in folded:
                    raise ReleaseError(f"Duplicate or case-colliding ZIP path: {path}")
                exact.add(path)
                folded.add(path.casefold())
                if info.is_dir() or path.endswith("/"):
                    raise ReleaseError(f"Directory entries are not allowed: {path}")
                if _entry_is_symlink(info):
                    raise ReleaseError(f"Symbolic links are not allowed: {path}")
                if info.flag_bits & 0x1:
                    raise ReleaseError(f"Encrypted entries are not allowed: {path}")
                if info.compress_type != zipfile.ZIP_STORED:
                    raise ReleaseError(f"Unsupported compression for {path}")
                if info.file_size > manifest["limits"]["maxEntryBytes"]:
                    raise ReleaseError(f"ZIP entry exceeds its size limit: {path}")
                total_size += info.file_size
                if total_size > manifest["limits"]["maxArchiveBytes"]:
                    raise ReleaseError("Expanded ZIP exceeds its size limit.")
                record = records.get(path)
                if record is None:
                    raise ReleaseError(f"ZIP contains an unmanifested file: {path}")
                content = archive.read(info)
                if len(content) != record["sizeBytes"]:
                    raise ReleaseError(f"File size mismatch: {path}")
                if sha256_bytes(content) != record["sha256"]:
                    raise ReleaseError(f"File digest mismatch: {path}")
                mode = stat.S_IMODE(info.external_attr >> 16)
                if f"{mode:04o}" != record["mode"]:
                    raise ReleaseError(f"File mode mismatch: {path}")
            if exact != set(records):
                raise ReleaseError("Manifest and ZIP file sets differ.")
            if not set(manifest["requiredFiles"]).issubset(exact):
                raise ReleaseError("ZIP is missing one or more required files.")
            root = PurePosixPath(next(iter(exact))).parts[0]
            release_info_path = f"{root}/release-info.json"
            try:
                release_info = json.loads(archive.read(release_info_path))
            except (KeyError, json.JSONDecodeError) as error:
                raise ReleaseError("release-info.json is missing or invalid.") from error
    except zipfile.BadZipFile as error:
        raise ReleaseError("Artifact is not a valid ZIP file.") from error

    expected_release_info = {
        "product": manifest["product"],
        "release": manifest["release"],
        "schemaVersion": 1,
        "sourceCommit": manifest["source"]["commit"],
        "sourceRepository": manifest["source"]["repository"],
    }
    if release_info != expected_release_info:
        raise ReleaseError("release-info.json does not match the manifest.")
    return manifest


def safe_extract(
    artifact_path: Path, destination: Path, manifest: dict[str, Any]
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve()
    records = {record["path"]: record for record in manifest["files"]}
    archive_root = PurePosixPath(next(iter(records))).parts[0]
    with zipfile.ZipFile(artifact_path, "r") as archive:
        for info in archive.infolist():
            parts = validate_archive_path(info.filename)
            if not parts or parts[0] != archive_root or len(parts) < 2:
                raise ReleaseError(f"Unexpected archive root: {info.filename}")
            relative = Path(*parts[1:])
            target = (destination_root / relative).resolve()
            if destination_root not in target.parents:
                raise ReleaseError(f"Extraction escaped the destination: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, int(records[info.filename]["mode"], 8))


def _read_pointer(install_root: Path) -> dict[str, Any] | None:
    pointer_path = install_root / "current.json"
    if not pointer_path.exists():
        return None
    if _is_link_or_junction(pointer_path) or not pointer_path.is_file():
        raise ReleaseError("The installation pointer must be a regular file.")
    try:
        value = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError("The installation pointer is invalid.") from error
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != 1
        or not isinstance(value.get("current"), str)
        or value.get("previous") is not None
        and not isinstance(value.get("previous"), str)
    ):
        raise ReleaseError("The installation pointer has an invalid schema.")
    validate_release(value["current"])
    if value["previous"] is not None:
        validate_release(value["previous"])
    return value


def _write_pointer(
    install_root: Path, current: str, previous: str | None
) -> None:
    validate_release(current)
    if previous is not None:
        validate_release(previous)
    _write_atomic(
        install_root / "current.json",
        canonical_json(
            {
                "current": current,
                "previous": previous,
                "schemaVersion": 1,
            }
        ),
    )


def _verify_installed_version(
    version_dir: Path, expected_manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    if (
        not version_dir.is_dir()
        or _is_link_or_junction(version_dir)
        or _is_link_or_junction(version_dir.parent)
    ):
        raise ReleaseError("Installed version must be a real directory.")
    manifest_path = version_dir / ".release-manifest.json"
    digest_path = version_dir / ".artifact-sha256"
    for metadata_path in (manifest_path, digest_path):
        if (
            not metadata_path.is_file()
            or _is_link_or_junction(metadata_path)
        ):
            raise ReleaseError(
                f"Installed metadata is missing or linked: {metadata_path.name}"
            )
    manifest = _load_manifest(manifest_path)
    validate_manifest(manifest)
    if expected_manifest is not None and manifest != expected_manifest:
        raise ReleaseError("Installed manifest differs from the release manifest.")
    if digest_path.read_text(encoding="ascii").strip() != manifest["artifact"]["sha256"]:
        raise ReleaseError("Installed artifact identity does not match its manifest.")

    expected_files: dict[str, dict[str, Any]] = {}
    archive_roots: set[str] = set()
    for record in manifest["files"]:
        parts = validate_archive_path(record["path"])
        archive_roots.add(parts[0])
        relative = PurePosixPath(*parts[1:]).as_posix()
        expected_files[relative] = record
    if len(archive_roots) != 1:
        raise ReleaseError("Installed manifest contains multiple archive roots.")

    actual_files: set[str] = set()
    for directory, directories, files in os.walk(version_dir, followlinks=False):
        directory_path = Path(directory)
        for name in directories:
            child = directory_path / name
            if _is_link_or_junction(child):
                raise ReleaseError(f"Installed directory is linked: {child}")
        for name in files:
            child = directory_path / name
            if _is_link_or_junction(child):
                raise ReleaseError(f"Installed file is linked: {child}")
            actual_files.add(child.relative_to(version_dir).as_posix())
    allowed_files = set(expected_files) | {
        ".artifact-sha256",
        ".release-manifest.json",
    }
    if actual_files != allowed_files:
        raise ReleaseError("Installed file set differs from the release manifest.")

    version_root = version_dir.resolve()
    for relative, record in expected_files.items():
        target = version_dir / Path(*PurePosixPath(relative).parts)
        resolved = target.resolve()
        if version_root not in resolved.parents:
            raise ReleaseError(f"Installed file escaped its version directory: {relative}")
        if not target.is_file() or _is_link_or_junction(target):
            raise ReleaseError(f"Installed file is missing or linked: {relative}")
        if target.stat().st_size != record["sizeBytes"]:
            raise ReleaseError(f"Installed file size mismatch: {relative}")
        if sha256_file(target) != record["sha256"]:
            raise ReleaseError(f"Installed file digest mismatch: {relative}")
        if os.name != "nt":
            actual_mode = stat.S_IMODE(target.stat().st_mode)
            if f"{actual_mode:04o}" != record["mode"]:
                raise ReleaseError(f"Installed file mode mismatch: {relative}")
    return manifest


def _health_check(
    version_dir: Path, expected_manifest: dict[str, Any] | None = None
) -> None:
    _verify_installed_version(version_dir, expected_manifest)
    result = subprocess.run(
        ["node", str(version_dir / "app" / "managed-demo.js"), "--self-test"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "self-test passed":
        raise ReleaseError(
            "Staged application health check failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def install_from_dist(dist_dir: Path, install_root: Path) -> Path:
    install_root = _absolute_path(install_root)
    if install_root.exists() and _is_link_or_junction(install_root):
        raise ReleaseError("Installation root must not be a link.")
    install_root.mkdir(parents=True, exist_ok=True)
    versions_root = install_root / "versions"
    if versions_root.exists() and _is_link_or_junction(versions_root):
        raise ReleaseError("Versions root must not be a link.")
    versions_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".release-assets-", dir=install_root
    ) as private_directory:
        private_dist = Path(private_directory)
        _stage_release_assets(dist_dir, private_dist)
        manifest = verify_release_dir(private_dist)
        if os.name != "nt":
            for asset in private_dist.iterdir():
                os.chmod(asset, 0o400)

        release = manifest["release"]
        final_dir = versions_root / release
        expected_digest = manifest["artifact"]["sha256"]
        if final_dir.exists():
            _health_check(final_dir, manifest)
        else:
            staging = versions_root / f".{release}.staging-{uuid.uuid4().hex}"
            try:
                safe_extract(
                    private_dist / manifest["artifact"]["name"],
                    staging,
                    manifest,
                )
                _write_atomic(
                    staging / ".artifact-sha256",
                    (expected_digest + "\n").encode("ascii"),
                )
                _write_atomic(
                    staging / ".release-manifest.json",
                    canonical_json(manifest),
                )
                _health_check(staging, manifest)
                os.replace(staging, final_dir)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

        pointer = _read_pointer(install_root)
        old_current = pointer["current"] if pointer else None
        old_previous = pointer["previous"] if pointer else None
        if old_current == release:
            previous = old_previous
        else:
            previous = old_current
        _write_pointer(install_root, release, previous)
        return final_dir


def rollback_install(install_root: Path) -> Path:
    install_root = _absolute_path(install_root)
    if _is_link_or_junction(install_root):
        raise ReleaseError("Installation root must not be a link.")
    pointer = _read_pointer(install_root)
    if not pointer or not pointer["previous"]:
        raise ReleaseError("No previous installed release is available.")
    current = pointer["current"]
    previous = pointer["previous"]
    previous_dir = install_root / "versions" / previous
    _health_check(previous_dir)
    _write_pointer(install_root, previous, current)
    return previous_dir


def run_installed(install_root: Path, arguments: Iterable[str]) -> int:
    install_root = _absolute_path(install_root)
    if _is_link_or_junction(install_root):
        raise ReleaseError("Installation root must not be a link.")
    pointer = _read_pointer(install_root)
    if not pointer:
        raise ReleaseError("No release is installed.")
    version_dir = install_root / "versions" / pointer["current"]
    _health_check(version_dir)
    script = version_dir / "app" / "managed-demo.js"
    return subprocess.run(["node", str(script), *arguments], check=False).returncode


def _resolve_tag_commit(repository: str, tag: str) -> str:
    def api(path: str) -> dict[str, Any]:
        result = subprocess.run(
            ["gh", "api", path],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ReleaseError(
                f"Cannot resolve tag {tag} to a commit: {result.stderr.strip()}"
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseError("GitHub returned invalid tag metadata.") from error
        if not isinstance(value, dict):
            raise ReleaseError("GitHub returned invalid tag metadata.")
        return value

    reference = api(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    target = reference.get("object")
    for _depth in range(5):
        if not isinstance(target, dict):
            break
        target_type = target.get("type")
        target_sha = target.get("sha")
        if not isinstance(target_sha, str) or not COMMIT_PATTERN.fullmatch(target_sha):
            break
        if target_type == "commit":
            return target_sha
        if target_type != "tag":
            break
        annotated = api(f"repos/{repository}/git/tags/{target_sha}")
        target = annotated.get("object")
    raise ReleaseError(f"Tag {tag} does not resolve to a valid commit.")


def _attestation_verification_command(
    artifact_path: Path, repository: str, source_commit: str
) -> list[str]:
    return [
        "gh",
        "attestation",
        "verify",
        str(artifact_path),
        "--repo",
        repository,
        "--signer-workflow",
        f"{repository}/.github/workflows/release.yml",
        "--signer-digest",
        source_commit,
        "--source-digest",
        source_commit,
    ]


def download_and_install(
    repository: str,
    tag: str,
    install_root: Path,
    verify_attestation: bool = False,
) -> Path:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseError("Repository must use the OWNER/REPOSITORY form.")
    validate_release(tag)
    if shutil.which("gh") is None:
        raise ReleaseError("GitHub CLI is required.")
    view = subprocess.run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "tagName,isDraft,isPrerelease",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if view.returncode != 0:
        raise ReleaseError(f"Cannot read release {tag}: {view.stderr.strip()}")
    try:
        release = json.loads(view.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseError("GitHub CLI returned invalid release metadata.") from error
    if release.get("tagName") != tag or release.get("isDraft"):
        raise ReleaseError("The requested release is missing or still a draft.")
    tag_commit = _resolve_tag_commit(repository, tag)

    with tempfile.TemporaryDirectory(prefix="managed-cli-download-") as directory:
        download_dir = Path(directory)
        artifact_name = f"managed-cli-demo_{tag}.zip"
        command = [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            artifact_name,
            "--pattern",
            MANIFEST_NAME,
            "--pattern",
            CHECKSUMS_NAME,
            "--dir",
            str(download_dir),
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise ReleaseError(f"Release download failed: {result.stderr.strip()}")
        manifest = verify_release_dir(download_dir)
        if (
            manifest["release"] != tag
            or manifest["source"]["repository"].casefold()
            != repository.casefold()
            or manifest["source"]["commit"] != tag_commit
        ):
            raise ReleaseError("Downloaded release identity does not match the request.")
        if _resolve_tag_commit(repository, tag) != tag_commit:
            raise ReleaseError("The release tag changed during download.")
        if verify_attestation:
            attestation = subprocess.run(
                _attestation_verification_command(
                    download_dir / artifact_name,
                    repository,
                    tag_commit,
                ),
                check=False,
                capture_output=True,
                text=True,
            )
            if attestation.returncode != 0:
                raise ReleaseError(
                    f"Artifact attestation verification failed: "
                    f"{attestation.stderr.strip()}"
                )
        return install_from_dist(download_dir, install_root)
