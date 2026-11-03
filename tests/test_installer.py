from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import install_command as installer  # noqa: E402


class CommandInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="installer-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_layouts_are_per_user(self) -> None:
        home = self.root / "home"
        windows = installer.default_layout(
            platform="win32",
            environment={"LOCALAPPDATA": str(self.root / "local")},
            home=home,
        )
        self.assertEqual(windows.platform, "windows")
        self.assertEqual(
            windows.install_root,
            self.root / "local" / "Programs" / "ManagedCliDemo",
        )
        self.assertEqual(windows.bin_dir, windows.install_root / "bin")

        linux = installer.default_layout(
            platform="linux",
            environment={},
            home=home,
        )
        self.assertEqual(
            linux.install_root,
            home / ".local" / "share" / "managed-cli-demo",
        )
        self.assertEqual(linux.bin_dir, home / ".local" / "bin")

        macos = installer.default_layout(
            platform="darwin",
            environment={},
            home=home,
        )
        self.assertEqual(
            macos.install_root,
            home / "Library" / "Application Support" / "ManagedCliDemo",
        )
        self.assertEqual(macos.bin_dir, home / ".local" / "bin")

    def test_posix_command_shim_runs_active_version(self) -> None:
        layout = installer.InstallLayout(
            "linux",
            self.root / "install",
            self.root / "bin",
        )
        app = (
            layout.install_root
            / "versions"
            / "v1.2.3"
            / "app"
            / "managed-demo.sh"
        )
        app.parent.mkdir(parents=True)
        app.write_text(
            "#!/usr/bin/env bash\nprintf 'active:%s\\n' \"$*\"\n",
            encoding="utf-8",
        )
        os.chmod(app, 0o755)
        layout.install_root.mkdir(exist_ok=True)
        (layout.install_root / "current.json").write_text(
            json.dumps(
                {
                    "current": "v1.2.3",
                    "previous": None,
                    "schemaVersion": 1,
                }
            ),
            encoding="utf-8",
        )
        shims = installer.create_command_shims(layout)
        self.assertEqual(len(shims), 1)
        self.assertEqual(stat.S_IMODE(shims[0].stat().st_mode), 0o755)
        result = subprocess.run(
            [str(shims[0]), "one", "two"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "active:one two")

    def test_posix_path_registration_is_idempotent(self) -> None:
        home = self.root / "home"
        home.mkdir()
        layout = installer.InstallLayout(
            "linux",
            self.root / "install",
            home / ".local" / "bin",
        )
        layout.bin_dir.mkdir(parents=True)
        profile = home / ".profile"
        profile.write_text("# existing\n", encoding="utf-8")
        os.chmod(profile, 0o600)
        environment = {"PATH": "/usr/bin", "SHELL": "/bin/bash"}
        first = installer.update_user_path(
            layout,
            environment=environment,
            home=home,
        )
        second = installer.update_user_path(
            layout,
            environment=environment,
            home=home,
        )
        profile = home / ".profile"
        content = profile.read_text(encoding="utf-8")
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(content.count(installer.PATH_BLOCK_START), 1)
        self.assertEqual(content.count(installer.PATH_BLOCK_END), 1)
        self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)

    def test_windows_shims_support_powershell_and_cmd(self) -> None:
        layout = installer.InstallLayout(
            "windows",
            self.root / "ManagedCliDemo",
            self.root / "ManagedCliDemo" / "bin",
        )
        shims = installer.create_command_shims(layout)
        self.assertEqual(
            {path.name for path in shims},
            {"managed-cli-demo.ps1", "managed-cli-demo.cmd"},
        )
        ps1 = (layout.bin_dir / "managed-cli-demo.ps1").read_text(
            encoding="utf-8"
        )
        cmd = (layout.bin_dir / "managed-cli-demo.cmd").read_text(
            encoding="utf-8"
        )
        self.assertIn(str(layout.install_root), ps1)
        self.assertIn("current.json", ps1)
        self.assertIn("managed-demo.ps1", ps1)
        self.assertIn("managed-cli-demo.ps1", cmd)

    def test_install_registers_command_after_verified_install(self) -> None:
        layout = installer.InstallLayout(
            "linux",
            self.root / "install",
            self.root / "bin",
        )
        installed = layout.install_root / "versions" / "v1.2.3"
        with mock.patch(
            "install_command.download_and_install",
            return_value=installed,
        ) as download:
            path, shims, path_update = installer.install_command(
                repository=installer.DEFAULT_REPOSITORY,
                channel="preview",
                tag="v1.2.3",
                layout=layout,
                update_path=False,
            )
        self.assertEqual(path, installed)
        self.assertTrue(all(shim.exists() for shim in shims))
        self.assertFalse(path_update.changed)
        download.assert_called_once_with(
            repository=installer.DEFAULT_REPOSITORY,
            tag="v1.2.3",
            install_root=layout.install_root,
            verify_attestation=True,
            expected_prerelease=True,
        )

    def test_repeat_channel_install_never_downgrades(self) -> None:
        layout = installer.InstallLayout(
            "linux",
            self.root / "install-newer",
            self.root / "bin-newer",
        )
        current = layout.install_root / "versions" / "v2.0.0"
        current.mkdir(parents=True)
        status = {
            "channel": "preview",
            "currentVersion": "v2.0.0",
            "latestVersion": "v1.0.0",
            "releaseUrl": "https://example.invalid/v1.0.0",
            "reason": "up-to-date-or-newer",
            "updateAvailable": False,
        }
        with (
            mock.patch(
                "install_command.check_update",
                return_value=status,
            ),
            mock.patch(
                "install_command.download_and_install",
            ) as download,
            mock.patch(
                "install_command.update_from_channel",
            ) as update,
        ):
            installed, _shims, _path = installer.install_command(
                repository=installer.DEFAULT_REPOSITORY,
                channel="preview",
                tag=None,
                layout=layout,
                update_path=False,
            )
        self.assertEqual(installed, current)
        download.assert_not_called()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
