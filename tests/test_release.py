from __future__ import annotations

import io
import json
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from release_tools import (  # noqa: E402
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    ReleaseError,
    _attestation_verification_command,
    _list_channel_releases,
    _resolve_tag_commit,
    _snappy_decompress_raw,
    build_release,
    canonical_json,
    check_update,
    install_from_dist,
    installation_update_lock,
    release_asset_names,
    release_is_newer,
    rollback_install,
    run_installed,
    select_channel_release,
    sha256_bytes,
    sha256_file,
    update_from_channel,
    validate_release,
    verify_release_dir,
)

from release_client import prompt_update, startup  # noqa: E402


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
        packaged_paths = {record["path"] for record in manifest["files"]}
        self.assertIn(
            "managed-cli-demo/scripts/release_client.py",
            packaged_paths,
        )
        self.assertIn(
            "managed-cli-demo/scripts/release_tools.py",
            packaged_paths,
        )
        with zipfile.ZipFile(
            first / manifest["artifact"]["name"],
            "r",
        ) as archive:
            self.assertIn(
                b"python3 -B",
                archive.read("managed-cli-demo/app/managed-demo.sh"),
            )
            self.assertIn(
                b"'-B'",
                archive.read("managed-cli-demo/app/managed-demo.ps1"),
            )

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
            "v1.2.3-1٢",
            "v1.2.3-a.lock",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ReleaseError):
                    validate_release(invalid)

    def test_snappy_bundle_decoder_enforces_bounds(self) -> None:
        self.assertEqual(
            _snappy_decompress_raw(b"\x03\x08abc", 10),
            b"abc",
        )
        self.assertEqual(
            _snappy_decompress_raw(b"\x06\x00a\x05\x01", 10),
            b"aaaaaa",
        )
        with self.assertRaisesRegex(ReleaseError, "copy exceeds"):
            _snappy_decompress_raw(b"\x06\x00a\x05\x00", 10)
        with self.assertRaisesRegex(ReleaseError, "size limit"):
            _snappy_decompress_raw(b"\x0b", 10)

    def test_attestation_is_bound_to_workflow_and_commit(self) -> None:
        commit = "a" * 40
        command = _attestation_verification_command(
            Path("/tmp/demo.zip"),
            REPOSITORY,
            commit,
            Path("/tmp/demo.bundle.json"),
        )
        self.assertIn(
            f"{REPOSITORY}/.github/workflows/release.yml",
            command,
        )
        self.assertEqual(command.count(commit), 2)
        self.assertIn("--bundle", command)
        self.assertIn(str(Path("/tmp/demo.bundle.json")), command)
        self.assertIn("--signer-digest", command)
        self.assertIn("--source-digest", command)
        self.assertIn("--source-ref", command)
        self.assertIn("refs/heads/main", command)
        self.assertEqual(
            set(release_asset_names("v1.2.3")),
            {
                "managed-cli-demo_v1.2.3.zip",
                "release-manifest.json",
                "SHA256SUMS.txt",
            },
        )

    def test_tag_resolution_uses_exact_tag_ref_and_peels_annotated_tag(self) -> None:
        tag_object = "b" * 40
        commit = "a" * 40
        responses = [
            {"object": {"sha": tag_object, "type": "tag"}},
            {"object": {"sha": commit, "type": "commit"}},
        ]
        with mock.patch(
            "release_tools._github_api_json",
            side_effect=responses,
        ) as api:
            self.assertEqual(
                _resolve_tag_commit(REPOSITORY, "v1.2.3"),
                commit,
            )
        self.assertEqual(
            api.call_args_list[0].args[0],
            f"repos/{REPOSITORY}/git/ref/tags/v1.2.3",
        )
        self.assertEqual(
            api.call_args_list[1].args[0],
            f"repos/{REPOSITORY}/git/tags/{tag_object}",
        )

    def test_semver_ordering_and_channel_selection(self) -> None:
        self.assertTrue(release_is_newer("v0.10.0", "v0.9.9"))
        self.assertTrue(release_is_newer("v1.0.0", "v1.0.0-preview.9"))
        self.assertTrue(
            release_is_newer("v1.0.0-preview.10", "v1.0.0-preview.2")
        )
        releases = [
            self._release("v0.9.9", prerelease=False),
            self._release("v0.10.0", prerelease=False),
            self._release("v1.0.0-preview.2", prerelease=True),
            self._release("v1.0.0-preview.10", prerelease=True),
            self._release("v9.0.0", prerelease=False, draft=True),
            self._release("v8.0.0", prerelease=False, complete=False),
            self._release("v7.0.0", prerelease=False, immutable=False),
        ]
        self.assertEqual(
            select_channel_release(releases, "stable")["tag"],
            "v0.10.0",
        )
        self.assertEqual(
            select_channel_release(releases, "preview")["tag"],
            "v1.0.0-preview.10",
        )

    def test_update_check_never_downgrades(self) -> None:
        install_root = self.root / "no-downgrade"
        installed = self.build("v0.2.0", "installed-v020")
        install_from_dist(installed, install_root)
        older = {
            "isPrerelease": True,
            "publishedAt": "2026-01-01T00:00:00Z",
            "tag": "v0.1.0",
            "url": "https://example.invalid/v0.1.0",
        }
        with mock.patch(
            "release_tools._list_channel_releases",
            return_value=older,
        ):
            status = check_update(REPOSITORY, "preview", install_root)
        self.assertFalse(status["updateAvailable"])
        self.assertEqual(status["reason"], "up-to-date-or-newer")

    def test_update_rejects_install_from_different_repository(self) -> None:
        install_root = self.root / "wrong-repository"
        output = self.root / "wrong-repository-release"
        build_release(
            repo_root=REPO_ROOT,
            output_dir=output,
            release="v0.1.0",
            repository="someone-else/managed-cli-release-demo",
        )
        install_from_dist(output, install_root)
        with self.assertRaisesRegex(
            ReleaseError,
            "different repository",
        ):
            check_update(
                REPOSITORY,
                "preview",
                install_root,
            )

    def test_auto_update_requires_attestation_and_channel_match(self) -> None:
        install_root = self.root / "auto-update"
        installed = self.build("v0.1.0", "installed-v010")
        install_from_dist(installed, install_root)
        latest = {
            "isPrerelease": True,
            "publishedAt": "2026-01-02T00:00:00Z",
            "tag": "v0.2.0",
            "url": "https://example.invalid/v0.2.0",
        }
        expected_path = install_root / "versions" / "v0.2.0"
        with (
            mock.patch(
                "release_tools._list_channel_releases",
                return_value=latest,
            ),
            mock.patch(
                "release_tools.download_verified_release",
                return_value={},
            ) as download,
            mock.patch(
                "release_tools.install_from_dist",
                return_value=expected_path,
            ) as install,
        ):
            status, path = update_from_channel(
                REPOSITORY,
                "preview",
                install_root,
            )
        self.assertTrue(status["updateAvailable"])
        self.assertEqual(path, expected_path)
        download.assert_called_once_with(
            repository=REPOSITORY,
            tag="v0.2.0",
            destination=mock.ANY,
            verify_attestation=True,
            expected_prerelease=True,
        )
        install.assert_called_once_with(mock.ANY, install_root)

    def test_failed_auto_update_preserves_current_version(self) -> None:
        install_root = self.root / "failed-auto-update"
        installed = self.build("v0.1.0", "failed-update-current")
        install_from_dist(installed, install_root)
        latest = {
            "isPrerelease": True,
            "publishedAt": "2026-01-02T00:00:00Z",
            "tag": "v0.2.0",
            "url": "https://example.invalid/v0.2.0",
        }
        with (
            mock.patch(
                "release_tools._list_channel_releases",
                return_value=latest,
            ),
            mock.patch(
                "release_tools.download_verified_release",
                side_effect=ReleaseError("signature rejected"),
            ),
        ):
            with self.assertRaisesRegex(ReleaseError, "signature rejected"):
                update_from_channel(REPOSITORY, "preview", install_root)
        pointer = json.loads(
            (install_root / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["current"], "v0.1.0")
        self.assertIsNone(pointer["previous"])

    def test_update_lock_serializes_installation(self) -> None:
        install_root = self.root / "locked-update"
        with installation_update_lock(install_root):
            with self.assertRaisesRegex(ReleaseError, "update lock"):
                with installation_update_lock(
                    install_root,
                    timeout_seconds=0.05,
                ):
                    self.fail("A concurrent update acquired the same lock.")

    def test_newer_concurrent_install_wins_during_download(self) -> None:
        install_root = self.root / "concurrent-update"
        first = self.build("v0.1.0", "concurrent-v010")
        third = self.build("v0.3.0", "concurrent-v030")
        install_from_dist(first, install_root)
        candidate = {
            "isPrerelease": True,
            "publishedAt": "2026-01-02T00:00:00Z",
            "tag": "v0.2.0",
            "url": "https://example.invalid/v0.2.0",
        }

        def promote_newer(**_arguments: object) -> dict[str, object]:
            install_from_dist(third, install_root)
            return {}

        with (
            mock.patch(
                "release_tools._list_channel_releases",
                return_value=candidate,
            ),
            mock.patch(
                "release_tools.download_verified_release",
                side_effect=promote_newer,
            ),
        ):
            status, path = update_from_channel(
                REPOSITORY,
                "preview",
                install_root,
            )
        self.assertIsNone(path)
        self.assertEqual(status["currentVersion"], "v0.3.0")
        self.assertEqual(status["reason"], "superseded-during-download")
        pointer = json.loads(
            (install_root / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["current"], "v0.3.0")

    def test_release_listing_fetches_and_flattens_all_pages(self) -> None:
        preview = self._release("v0.2.0", prerelease=True)
        stable = self._release("v0.1.0", prerelease=False)
        with mock.patch(
            "release_tools._github_api_json",
            side_effect=[[preview] * 100, [stable]],
        ) as api:
            selected = _list_channel_releases(REPOSITORY, "stable")
        self.assertEqual(selected["tag"], "v0.1.0")
        self.assertEqual(api.call_count, 2)
        self.assertEqual(
            api.call_args_list[0].args[1],
            {"per_page": 100, "page": 1},
        )
        self.assertEqual(
            api.call_args_list[1].args[1],
            {"per_page": 100, "page": 2},
        )

    def test_prompted_update_decline_keeps_current_version(self) -> None:
        status = {
            "channel": "preview",
            "currentVersion": "v0.2.0",
            "latestVersion": "v0.3.0",
            "reason": "newer-release",
            "releaseUrl": "https://example.invalid/v0.3.0",
            "updateAvailable": True,
        }
        output = io.StringIO()
        with (
            mock.patch("release_client.check_update", return_value=status),
            mock.patch("release_client.update_from_channel") as update,
        ):
            result, path = prompt_update(
                REPOSITORY,
                "preview",
                self.root / "decline",
                input_fn=lambda _prompt: "n",
                output=output,
            )
        self.assertIsNone(path)
        self.assertEqual(result["reason"], "declined")
        self.assertIn("Update declined", output.getvalue())
        update.assert_not_called()

    def test_prompted_update_installs_only_after_yes(self) -> None:
        status = {
            "channel": "preview",
            "currentVersion": "v0.2.0",
            "latestVersion": "v0.3.0",
            "reason": "newer-release",
            "releaseUrl": "https://example.invalid/v0.3.0",
            "updateAvailable": True,
        }
        installed_path = self.root / "versions" / "v0.3.0"
        output = io.StringIO()
        with (
            mock.patch("release_client.check_update", return_value=status),
            mock.patch(
                "release_client.update_from_channel",
                return_value=(status, installed_path),
            ) as update,
        ):
            result, path = prompt_update(
                REPOSITORY,
                "preview",
                self.root / "accept",
                input_fn=lambda _prompt: "yes",
                output=output,
            )
        self.assertEqual(result, status)
        self.assertEqual(path, installed_path)
        self.assertIn("Updated to v0.3.0", output.getvalue())
        update.assert_called_once_with(
            REPOSITORY,
            "preview",
            self.root / "accept",
            expected_version="v0.3.0",
        )

    def test_prompted_update_never_updates_without_a_tty(self) -> None:
        status = {
            "channel": "preview",
            "currentVersion": "v0.2.0",
            "latestVersion": "v0.3.0",
            "reason": "newer-release",
            "releaseUrl": "https://example.invalid/v0.3.0",
            "updateAvailable": True,
        }
        output = io.StringIO()
        with (
            mock.patch("release_client.check_update", return_value=status),
            mock.patch("release_client.sys.stdin.isatty", return_value=False),
            mock.patch("release_client.update_from_channel") as update,
        ):
            result, path = prompt_update(
                REPOSITORY,
                "preview",
                self.root / "noninteractive",
                output=output,
            )
        self.assertIsNone(path)
        self.assertEqual(result["reason"], "confirmation-required")
        self.assertIn("interactive confirmation is required", output.getvalue())
        update.assert_not_called()

    def test_reviewed_version_change_requires_another_confirmation(self) -> None:
        install_root = self.root / "channel-change"
        installed = self.build("v0.2.0", "channel-change-current")
        install_from_dist(installed, install_root)
        changed = {
            "isPrerelease": True,
            "publishedAt": "2026-01-03T00:00:00Z",
            "tag": "v0.4.0",
            "url": "https://example.invalid/v0.4.0",
        }
        with (
            mock.patch(
                "release_tools._list_channel_releases",
                return_value=changed,
            ),
            mock.patch("release_tools.download_verified_release") as download,
        ):
            with self.assertRaisesRegex(
                ReleaseError,
                "changed after confirmation",
            ):
                update_from_channel(
                    REPOSITORY,
                    "preview",
                    install_root,
                    expected_version="v0.3.0",
                )
        download.assert_not_called()

    def test_installed_startup_checks_then_runs_active_version(self) -> None:
        install_root = self.root / "startup"
        release = self.build("v0.3.0", "startup-release")
        installed_path = install_from_dist(release, install_root)
        no_update = {
            "channel": "preview",
            "currentVersion": "v0.3.0",
            "latestVersion": "v0.3.0",
            "reason": "up-to-date-or-newer",
            "releaseUrl": "https://example.invalid/v0.3.0",
            "updateAvailable": False,
        }
        with mock.patch(
            "release_client.prompt_update",
            return_value=(no_update, None),
        ) as prompt:
            result = startup(
                installed_path,
                install_root,
                "preview",
                ["--json"],
            )
        self.assertEqual(result, 0)
        prompt.assert_called_once_with(
            REPOSITORY,
            "preview",
            install_root,
            report_current=False,
        )

    @staticmethod
    def _release(
        tag: str,
        *,
        prerelease: bool,
        draft: bool = False,
        complete: bool = True,
        immutable: bool = True,
    ) -> dict[str, object]:
        assets = (
            [{"name": name} for name in release_asset_names(tag)]
            if complete
            else [{"name": f"managed-cli-demo_{tag}.zip"}]
        )
        return {
            "assets": assets,
            "draft": draft,
            "html_url": f"https://example.invalid/{tag}",
            "immutable": immutable,
            "prerelease": prerelease,
            "published_at": "2026-01-01T00:00:00Z",
            "tag_name": tag,
        }

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
