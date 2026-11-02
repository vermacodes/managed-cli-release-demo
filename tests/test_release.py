from __future__ import annotations

import json
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from release_tools import (  # noqa: E402
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    ReleaseError,
    _attestation_verification_command,
    build_release,
    canonical_json,
    install_from_dist,
    rollback_install,
    run_installed,
    sha256_bytes,
    sha256_file,
    validate_release,
    verify_release_dir,
)


REPOSITORY = "vermacodes/managed-cli-release-demo"


class ReleasePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="release-demo-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, version: str, name: str) -> Path:
        output = self.root / name
        build_release(
            repo_root=REPO_ROOT,
            output_dir=output,
            release=version,
            repository=REPOSITORY,
        )
        return output

    def test_build_is_reproducible_and_valid(self) -> None:
        first = self.build("v0.1.0", "first")
        second = self.build("v0.1.0", "second")
        self.assertEqual(
            {path.name: path.read_bytes() for path in first.iterdir()},
            {path.name: path.read_bytes() for path in second.iterdir()},
        )
        manifest = verify_release_dir(first)
        self.assertEqual(manifest["release"], "v0.1.0")
        self.assertEqual(manifest["source"]["repository"], REPOSITORY)

    def test_untracked_local_file_cannot_enter_bundle(self) -> None:
        secret = REPO_ROOT / "local-secret.txt"
        secret.write_text("synthetic-local-only-value\n", encoding="utf-8")
        try:
            output = self.build("v0.1.0", "untracked")
            manifest = verify_release_dir(output)
            self.assertNotIn(
                "local-secret.txt",
                {record["path"] for record in manifest["files"]},
            )
        finally:
            secret.unlink(missing_ok=True)

    def test_tampered_archive_is_rejected(self) -> None:
        output = self.build("v0.1.0", "tampered")
        manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
        artifact = output / manifest["artifact"]["name"]
        with artifact.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(ReleaseError, "Checksum mismatch"):
            verify_release_dir(output)

    def test_path_traversal_is_rejected(self) -> None:
        output = self.build("v0.1.0", "traversal")
        self._append_entry(output, "../escape.txt", b"unsafe")
        with self.assertRaisesRegex(ReleaseError, "Unsafe repository path"):
            verify_release_dir(output)

    def test_case_collision_is_rejected(self) -> None:
        output = self.build("v0.1.0", "collision")
        self._append_entry(output, "managed-cli-demo/README.MD", b"collision")
        with self.assertRaisesRegex(ReleaseError, "case-colliding"):
            verify_release_dir(output)

    def test_symbolic_link_is_rejected(self) -> None:
        output = self.build("v0.1.0", "symlink")
        self._append_entry(
            output,
            "managed-cli-demo/link",
            b"README.md",
            zip_mode=stat.S_IFLNK | 0o777,
            manifest_mode="0755",
        )
        with self.assertRaisesRegex(ReleaseError, "Symbolic links"):
            verify_release_dir(output)

    def test_staged_install_is_idempotent_and_rolls_back(self) -> None:
        install_root = self.root / "install"
        first = self.build("v0.1.0", "install-first")
        second = self.build("v0.2.0", "install-second")

        first_path = install_from_dist(first, install_root)
        self.assertEqual(first_path.name, "v0.1.0")
        self.assertEqual(
            install_from_dist(first, install_root),
            first_path,
        )
        second_path = install_from_dist(second, install_root)
        self.assertEqual(second_path.name, "v0.2.0")

        pointer = json.loads(
            (install_root / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["current"], "v0.2.0")
        self.assertEqual(pointer["previous"], "v0.1.0")

        rolled_back = rollback_install(install_root)
        self.assertEqual(rolled_back.name, "v0.1.0")
        pointer = json.loads(
            (install_root / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["current"], "v0.1.0")
        self.assertEqual(pointer["previous"], "v0.2.0")

    def test_installed_file_tampering_is_rejected(self) -> None:
        install_root = self.root / "install-tamper"
        output = self.build("v0.1.0", "install-tamper-release")
        installed = install_from_dist(output, install_root)
        target = installed / "app" / "managed-demo.js"
        tampered = bytearray(target.read_bytes())
        tampered[-2] ^= 1
        target.write_bytes(tampered)
        with self.assertRaisesRegex(ReleaseError, "digest mismatch"):
            install_from_dist(output, install_root)

    def test_pointer_path_escape_is_rejected(self) -> None:
        install_root = self.root / "pointer-escape"
        output = self.build("v0.1.0", "pointer-release")
        install_from_dist(output, install_root)
        (install_root / "current.json").write_text(
            json.dumps(
                {
                    "current": "../../outside",
                    "previous": None,
                    "schemaVersion": 1,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ReleaseError, "Release must match"):
            run_installed(install_root, [])

    def test_release_version_uses_strict_semver(self) -> None:
        validate_release("v1.2.3")
        validate_release("v1.2.3-preview.1")
        for invalid in (
            "v01.2.3",
            "v1.02.3",
            "v1.2.3-..",
            "v1.2.3-.preview",
            "v1٢.2.3",
            "v1.2.3-a.lock",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ReleaseError):
                    validate_release(invalid)

    def test_attestation_is_bound_to_workflow_and_commit(self) -> None:
        commit = "a" * 40
        command = _attestation_verification_command(
            Path("/tmp/demo.zip"),
            REPOSITORY,
            commit,
        )
        self.assertIn(
            f"{REPOSITORY}/.github/workflows/release.yml",
            command,
        )
        self.assertEqual(command.count(commit), 2)
        self.assertIn("--signer-digest", command)
        self.assertIn("--source-digest", command)

    def _append_entry(
        self,
        output: Path,
        path: str,
        content: bytes,
        zip_mode: int = 0o644,
        manifest_mode: str = "0644",
    ) -> None:
        manifest_path = output / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_path = output / manifest["artifact"]["name"]

        rewritten = self.root / f"rewrite-{artifact_path.name}"
        with zipfile.ZipFile(artifact_path, "r") as source, zipfile.ZipFile(
            rewritten, "w", allowZip64=False
        ) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info))
            malicious = zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0))
            malicious.create_system = 3
            malicious.compress_type = zipfile.ZIP_STORED
            malicious.external_attr = (zip_mode & 0xFFFF) << 16
            target.writestr(malicious, content)
        shutil.move(rewritten, artifact_path)

        manifest["files"].append(
            {
                "mode": manifest_mode,
                "path": path,
                "sha256": sha256_bytes(content),
                "sizeBytes": len(content),
            }
        )
        manifest["artifact"]["entryCount"] = len(manifest["files"])
        manifest["artifact"]["sha256"] = sha256_file(artifact_path)
        manifest["artifact"]["sizeBytes"] = artifact_path.stat().st_size
        manifest_bytes = canonical_json(manifest)
        manifest_path.write_bytes(manifest_bytes)
        (output / CHECKSUMS_NAME).write_text(
            f"{sha256_file(artifact_path)}  {artifact_path.name}\n"
            f"{sha256_bytes(manifest_bytes)}  {MANIFEST_NAME}\n",
            encoding="ascii",
        )


if __name__ == "__main__":
    unittest.main()
