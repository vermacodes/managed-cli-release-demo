#!/usr/bin/env python3
"""Deterministic release build, verification, download, and installation tools."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


MANIFEST_NAME = "release-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS.txt"
PROVENANCE_NAME = "build-provenance.jsonl"
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
GITHUB_API_ROOT = "https://api.github.com"
HTTP_USER_AGENT = "managed-cli-release-demo/1"
MAX_REMOTE_ASSET_BYTES = 25 * 1024 * 1024
MAX_ATTESTATION_BUNDLE_BYTES = 5 * 1024 * 1024
ATTESTATION_BUNDLE_HOST = "tmaproduction.blob.core.windows.net"
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"


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


def release_version_key(
    release: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    validate_release(release)
    version = release[1:]
    core, separator, prerelease = version.partition("-")
    major, minor, patch = (int(value) for value in core.split("."))
    if not separator:
        return major, minor, patch, 1, ()
    identifiers: list[tuple[int, int | str]] = []
    for identifier in prerelease.split("."):
        if identifier.isascii() and identifier.isdigit():
            identifiers.append((0, int(identifier)))
        else:
            identifiers.append((1, identifier))
    return major, minor, patch, 0, tuple(identifiers)


def release_is_newer(candidate: str, current: str) -> bool:
    return release_version_key(candidate) > release_version_key(current)


def release_asset_names(release: str) -> tuple[str, str, str]:
    validate_release(release)
    return (
        f"managed-cli-demo_{release}.zip",
        MANIFEST_NAME,
        CHECKSUMS_NAME,
    )


def published_asset_names(release: str) -> tuple[str, str, str, str]:
    return (*release_asset_names(release), PROVENANCE_NAME)


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


@contextmanager
def installation_update_lock(
    install_root: Path, timeout_seconds: float = 120
) -> Iterator[None]:
    if timeout_seconds < 0:
        raise ReleaseError("Update lock timeout must not be negative.")
    install_root = _absolute_path(install_root)
    if install_root.exists() and _is_link_or_junction(install_root):
        raise ReleaseError("Installation root must not be a link.")
    install_root.mkdir(parents=True, exist_ok=True)
    lock_path = install_root / ".update.lock"
    if lock_path.exists() and _is_link_or_junction(lock_path):
        raise ReleaseError("Update lock must not be a link.")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ReleaseError(f"Cannot open the update lock: {error}") from error
    deadline = time.monotonic() + timeout_seconds
    with os.fdopen(descriptor, "r+b") as lock_stream:
        metadata = os.fstat(lock_stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseError("Update lock must be a regular file.")
        lock_stream.seek(0, os.SEEK_END)
        if lock_stream.tell() == 0:
            lock_stream.write(b"\0")
            lock_stream.flush()
        acquired = False
        while not acquired:
            try:
                lock_stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        lock_stream.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                acquired = True
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise ReleaseError("Timed out waiting for the update lock.") from error
                time.sleep(0.1)
        try:
            yield
        finally:
            lock_stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)


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


def _github_api_json(
    path: str,
    query: dict[str, str | int] | None = None,
) -> Any:
    url = f"{GITHUB_API_ROOT}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": HTTP_USER_AGENT,
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read()
    except HTTPError as error:
        raise ReleaseError(
            f"GitHub API request failed ({error.code}): {path}"
        ) from error
    except (OSError, URLError) as error:
        raise ReleaseError(f"GitHub API request failed: {error}") from error
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise ReleaseError("GitHub returned invalid JSON.") from error


def _download_release_asset(
    url: str,
    destination: Path,
    repository: str,
    tag: str,
    asset_name: str,
) -> None:
    expected_url = (
        f"https://github.com/{repository}/releases/download/"
        f"{quote(tag, safe='')}/{quote(asset_name, safe='')}"
    )
    if url != expected_url:
        raise ReleaseError(f"Unexpected download URL for {asset_name}.")
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=120) as response:
            final_url = urlparse(response.geturl())
            hostname = (final_url.hostname or "").casefold()
            if (
                final_url.scheme != "https"
                or not (
                    hostname == "github.com"
                    or hostname == "objects.githubusercontent.com"
                    or hostname.endswith(".githubusercontent.com")
                )
            ):
                raise ReleaseError(
                    f"Download redirected to an untrusted host: {hostname}"
                )
            declared_size = response.headers.get("Content-Length")
            if (
                declared_size is not None
                and int(declared_size) > MAX_REMOTE_ASSET_BYTES
            ):
                raise ReleaseError(f"Remote asset is too large: {asset_name}")
            total = 0
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REMOTE_ASSET_BYTES:
                        raise ReleaseError(
                            f"Remote asset exceeded its size limit: {asset_name}"
                        )
                    output.write(chunk)
    except HTTPError as error:
        destination.unlink(missing_ok=True)
        raise ReleaseError(
            f"Release asset download failed ({error.code}): {asset_name}"
        ) from error
    except (OSError, URLError, ValueError, ReleaseError):
        destination.unlink(missing_ok=True)
        raise


def _read_snappy_varint(payload: bytes) -> tuple[int, int]:
    value = 0
    shift = 0
    for index, byte in enumerate(payload[:10]):
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index + 1
        shift += 7
    raise ReleaseError("Snappy bundle has an invalid length prefix.")


def _snappy_decompress_raw(payload: bytes, max_output: int) -> bytes:
    expected_length, position = _read_snappy_varint(payload)
    if expected_length > max_output:
        raise ReleaseError("Snappy bundle exceeds its expanded size limit.")
    output = bytearray()
    while position < len(payload) and len(output) < expected_length:
        tag = payload[position]
        position += 1
        kind = tag & 0x03
        if kind == 0:
            encoded_length = tag >> 2
            if encoded_length < 60:
                length = encoded_length + 1
            else:
                length_bytes = encoded_length - 59
                if (
                    length_bytes > 4
                    or position + length_bytes > len(payload)
                ):
                    raise ReleaseError("Snappy literal length is invalid.")
                length = (
                    int.from_bytes(
                        payload[position : position + length_bytes],
                        "little",
                    )
                    + 1
                )
                position += length_bytes
            end = position + length
            if end > len(payload) or len(output) + length > expected_length:
                raise ReleaseError("Snappy literal exceeds its bounds.")
            output.extend(payload[position:end])
            position = end
            continue

        if kind == 1:
            length = 4 + ((tag >> 2) & 0x07)
            if position >= len(payload):
                raise ReleaseError("Snappy copy offset is missing.")
            offset = ((tag & 0xE0) << 3) | payload[position]
            position += 1
        elif kind == 2:
            length = 1 + (tag >> 2)
            if position + 2 > len(payload):
                raise ReleaseError("Snappy copy offset is truncated.")
            offset = int.from_bytes(payload[position : position + 2], "little")
            position += 2
        else:
            length = 1 + (tag >> 2)
            if position + 4 > len(payload):
                raise ReleaseError("Snappy copy offset is truncated.")
            offset = int.from_bytes(payload[position : position + 4], "little")
            position += 4
        if (
            offset <= 0
            or offset > len(output)
            or len(output) + length > expected_length
        ):
            raise ReleaseError("Snappy copy exceeds its bounds.")
        for _index in range(length):
            output.append(output[-offset])

    if position != len(payload) or len(output) != expected_length:
        raise ReleaseError("Snappy bundle length does not match its payload.")
    return bytes(output)


def _download_attestation_bundle(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != ATTESTATION_BUNDLE_HOST
    ):
        raise ReleaseError("Attestation bundle URL uses an untrusted host.")
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            final = urlparse(response.geturl())
            if (
                final.scheme != "https"
                or (final.hostname or "").casefold()
                != ATTESTATION_BUNDLE_HOST
            ):
                raise ReleaseError(
                    "Attestation bundle redirected to an untrusted host."
                )
            payload = response.read(MAX_ATTESTATION_BUNDLE_BYTES + 1)
    except HTTPError as error:
        raise ReleaseError(
            f"Attestation bundle download failed ({error.code})."
        ) from error
    except (OSError, URLError) as error:
        raise ReleaseError(
            f"Attestation bundle download failed: {error}"
        ) from error
    if len(payload) > MAX_ATTESTATION_BUNDLE_BYTES:
        raise ReleaseError("Attestation bundle exceeded its size limit.")
    if response.headers.get("Content-Type") == "application/x-snappy":
        payload = _snappy_decompress_raw(
            payload,
            MAX_ATTESTATION_BUNDLE_BYTES,
        )
    try:
        bundle = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError("Attestation bundle is invalid JSON.") from error
    if not isinstance(bundle, dict):
        raise ReleaseError("Attestation bundle must be a JSON object.")
    return bundle


def _workflow_bundle_for_digest(
    repository: str,
    digest: str,
) -> dict[str, Any]:
    response = _github_api_json(
        f"repos/{repository}/attestations/sha256:{digest}",
        {
            "per_page": 100,
            "predicate_type": SLSA_PREDICATE,
        },
    )
    attestations = response.get("attestations") if isinstance(response, dict) else None
    if not isinstance(attestations, list):
        raise ReleaseError("GitHub returned invalid attestation metadata.")
    for attestation in attestations:
        if not isinstance(attestation, dict):
            continue
        bundle = attestation.get("bundle")
        if not isinstance(bundle, dict):
            bundle_url = attestation.get("bundle_url")
            if isinstance(bundle_url, str):
                bundle = _download_attestation_bundle(bundle_url)
        try:
            encoded = bundle["dsseEnvelope"]["payload"]
            statement = json.loads(base64.b64decode(encoded))
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            continue
        if statement.get("predicateType") != SLSA_PREDICATE:
            continue
        subjects = statement.get("subject")
        if not isinstance(subjects, list):
            continue
        if any(
            isinstance(subject, dict)
            and isinstance(subject.get("digest"), dict)
            and subject["digest"].get("sha256") == digest
            for subject in subjects
        ):
            return bundle
    raise ReleaseError(f"No build-provenance attestation found for sha256:{digest}.")


def _resolve_tag_commit(repository: str, tag: str) -> str:
    def api(path: str) -> dict[str, Any]:
        value = _github_api_json(path)
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
    artifact_path: Path,
    repository: str,
    source_commit: str,
    bundle_path: Path,
) -> list[str]:
    return [
        "gh",
        "attestation",
        "verify",
        str(artifact_path),
        "--bundle",
        str(bundle_path),
        "--repo",
        repository,
        "--signer-workflow",
        f"{repository}/.github/workflows/release.yml",
        "--signer-digest",
        source_commit,
        "--source-digest",
        source_commit,
        "--source-ref",
        "refs/heads/main",
    ]


def download_verified_release(
    repository: str,
    tag: str,
    destination: Path,
    verify_attestation: bool = True,
    expected_prerelease: bool | None = None,
) -> dict[str, Any]:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseError("Repository must use the OWNER/REPOSITORY form.")
    validate_release(tag)
    if verify_attestation and shutil.which("gh") is None:
        raise ReleaseError("GitHub CLI is required for offline attestation verification.")
    destination = _absolute_path(destination)
    if (
        not destination.is_dir()
        or _is_link_or_junction(destination)
        or any(destination.iterdir())
    ):
        raise ReleaseError("Download destination must be an empty real directory.")

    release = _github_api_json(
        f"repos/{repository}/releases/tags/{quote(tag, safe='')}"
    )
    if not isinstance(release, dict):
        raise ReleaseError("GitHub returned invalid release metadata.")
    if release.get("tag_name") != tag or release.get("draft") is not False:
        raise ReleaseError("The requested release is missing or still a draft.")
    if (
        expected_prerelease is not None
        and release.get("prerelease") is not expected_prerelease
    ):
        raise ReleaseError("The requested release is in the wrong update channel.")
    if (
        expected_prerelease is not None
        and release.get("immutable") is not True
    ):
        raise ReleaseError("Automatic updates require an immutable release.")

    tag_commit = _resolve_tag_commit(repository, tag)
    expected_names = set(published_asset_names(tag))
    download_names = set(release_asset_names(tag))
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseError("GitHub returned invalid release assets.")
    by_name = {
        asset.get("name"): asset
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    }
    if len(assets) != len(expected_names) or set(by_name) != expected_names:
        raise ReleaseError("Release assets do not match the release contract.")

    for asset_name in sorted(expected_names):
        asset = by_name[asset_name]
        digest_text = asset.get("digest")
        if (
            asset.get("state") != "uploaded"
            or not isinstance(digest_text, str)
            or not digest_text.startswith("sha256:")
            or not SHA256_PATTERN.fullmatch(digest_text[7:])
        ):
            raise ReleaseError(f"Release asset metadata is invalid: {asset_name}")
        if asset_name not in download_names:
            continue
        _download_release_asset(
            str(asset.get("browser_download_url")),
            destination / asset_name,
            repository,
            tag,
            asset_name,
        )
        if sha256_file(destination / asset_name) != digest_text[7:]:
            raise ReleaseError(f"GitHub asset digest mismatch: {asset_name}")

    manifest = verify_release_dir(destination)
    if (
        manifest["release"] != tag
        or manifest["source"]["repository"].casefold() != repository.casefold()
        or manifest["source"]["commit"] != tag_commit
    ):
        raise ReleaseError("Downloaded release identity does not match the request.")
    if _resolve_tag_commit(repository, tag) != tag_commit:
        raise ReleaseError("The release tag changed during download.")

    if verify_attestation:
        with tempfile.TemporaryDirectory(
            prefix="managed-cli-attestations-"
        ) as bundle_directory:
            bundle_root = Path(bundle_directory)
            for asset_name in release_asset_names(tag):
                artifact_path = destination / asset_name
                digest = sha256_file(artifact_path)
                bundle = _workflow_bundle_for_digest(repository, digest)
                bundle_path = bundle_root / f"{asset_name}.bundle.json"
                bundle_path.write_bytes(canonical_json(bundle))
                attestation = subprocess.run(
                    _attestation_verification_command(
                        artifact_path,
                        repository,
                        tag_commit,
                        bundle_path,
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if attestation.returncode != 0:
                    raise ReleaseError(
                        f"Attestation verification failed for {asset_name}: "
                        f"{attestation.stderr.strip()}"
                    )
    return manifest


def download_and_install(
    repository: str,
    tag: str,
    install_root: Path,
    verify_attestation: bool = True,
    expected_prerelease: bool | None = None,
) -> Path:
    with tempfile.TemporaryDirectory(prefix="managed-cli-download-") as directory:
        download_dir = Path(directory)
        download_verified_release(
            repository=repository,
            tag=tag,
            destination=download_dir,
            verify_attestation=verify_attestation,
            expected_prerelease=expected_prerelease,
        )
        return install_from_dist(download_dir, install_root)


def select_channel_release(
    releases: list[dict[str, Any]], channel: str
) -> dict[str, Any] | None:
    if channel not in ("stable", "preview"):
        raise ReleaseError("Update channel must be stable or preview.")
    candidates: list[dict[str, Any]] = []
    for release in releases:
        if (
            not isinstance(release, dict)
            or release.get("draft") is not False
            or release.get("immutable") is not True
        ):
            continue
        is_prerelease = release.get("prerelease") is True
        if (channel == "preview") != is_prerelease:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        try:
            validate_release(tag)
        except ReleaseError:
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        asset_names = {
            asset.get("name")
            for asset in assets
            if isinstance(asset, dict) and isinstance(asset.get("name"), str)
        }
        expected_assets = set(published_asset_names(tag))
        if len(assets) != len(expected_assets) or asset_names != expected_assets:
            continue
        candidates.append(
            {
                "isPrerelease": is_prerelease,
                "publishedAt": release.get("published_at"),
                "tag": tag,
                "url": release.get("html_url"),
            }
        )
    if not candidates:
        return None
    return max(candidates, key=lambda value: release_version_key(value["tag"]))


def _list_channel_releases(
    repository: str, channel: str
) -> dict[str, Any] | None:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseError("Repository must use the OWNER/REPOSITORY form.")
    releases: list[dict[str, Any]] = []
    for page in range(1, 101):
        value = _github_api_json(
            f"repos/{repository}/releases",
            {"per_page": 100, "page": page},
        )
        if not isinstance(value, list):
            raise ReleaseError("GitHub returned invalid release metadata.")
        releases.extend(
            release for release in value if isinstance(release, dict)
        )
        if len(value) < 100:
            break
    else:
        raise ReleaseError("Release pagination exceeded its safety limit.")
    return select_channel_release(releases, channel)


def check_update(
    repository: str, channel: str, install_root: Path
) -> dict[str, Any]:
    install_root = _absolute_path(install_root)
    pointer = _read_pointer(install_root) if install_root.exists() else None
    current = pointer["current"] if pointer else None
    if current is not None:
        installed_manifest = _verify_installed_version(
            install_root / "versions" / current
        )
        if (
            installed_manifest["source"]["repository"].casefold()
            != repository.casefold()
        ):
            raise ReleaseError("Installed release belongs to a different repository.")
    latest = _list_channel_releases(repository, channel)
    latest_tag = latest["tag"] if latest else None
    update_available = bool(
        latest_tag is not None
        and (current is None or release_is_newer(latest_tag, current))
    )
    if latest_tag is None:
        reason = "channel-empty"
    elif current is None:
        reason = "not-installed"
    elif update_available:
        reason = "newer-release"
    else:
        reason = "up-to-date-or-newer"
    return {
        "channel": channel,
        "currentVersion": current,
        "latestVersion": latest_tag,
        "releaseUrl": latest["url"] if latest else None,
        "reason": reason,
        "updateAvailable": update_available,
    }


def update_from_channel(
    repository: str,
    channel: str,
    install_root: Path,
    expected_version: str | None = None,
) -> tuple[dict[str, Any], Path | None]:
    with installation_update_lock(install_root):
        status = check_update(repository, channel, install_root)
        if (
            expected_version is not None
            and status["updateAvailable"]
            and status["latestVersion"] != expected_version
        ):
            raise ReleaseError(
                "The update channel changed after confirmation; review it again."
            )
        if not status["updateAvailable"]:
            return status, None
        selected_tag = status["latestVersion"]
        with tempfile.TemporaryDirectory(
            prefix="managed-cli-update-"
        ) as directory:
            download_dir = Path(directory)
            download_verified_release(
                repository=repository,
                tag=selected_tag,
                destination=download_dir,
                verify_attestation=True,
                expected_prerelease=channel == "preview",
            )
            install_root = _absolute_path(install_root)
            pointer = _read_pointer(install_root)
            active = pointer["current"] if pointer else None
            if active is not None:
                active_manifest = _verify_installed_version(
                    install_root / "versions" / active
                )
                if (
                    active_manifest["source"]["repository"].casefold()
                    != repository.casefold()
                ):
                    raise ReleaseError("Installed release belongs to a different repository.")
            if active is not None and not release_is_newer(selected_tag, active):
                status = {
                    **status,
                    "currentVersion": active,
                    "reason": "superseded-during-download",
                    "updateAvailable": False,
                }
                return status, None
            path = install_from_dist(download_dir, install_root)
            return status, path
