#!/usr/bin/env python3
"""Install the managed demo as a command on Windows, Linux/WSL, or macOS."""

from __future__ import annotations

import argparse
import ctypes
import os
import shlex
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from release_tools import (
    ReleaseError,
    check_update,
    download_and_install,
    update_from_channel,
    validate_release,
)


COMMAND_NAME = "managed-cli-demo"
DEFAULT_REPOSITORY = "vermacodes/managed-cli-release-demo"
VERSION_PATTERN = (
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
PATH_BLOCK_START = "# >>> managed-cli-demo PATH >>>"
PATH_BLOCK_END = "# <<< managed-cli-demo PATH <<<"


@dataclass(frozen=True)
class InstallLayout:
    platform: str
    install_root: Path
    bin_dir: Path


@dataclass(frozen=True)
class PathUpdate:
    changed: bool
    location: str | None
    restart_required: bool


def platform_name(value: str | None = None) -> str:
    candidate = value or sys.platform
    if candidate.startswith("win"):
        return "windows"
    if candidate == "darwin":
        return "macos"
    return "linux"


def default_layout(
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> InstallLayout:
    selected = platform_name(platform)
    env = environment or os.environ
    user_home = (home or Path.home()).expanduser()
    if selected == "windows":
        local_app_data = env.get("LOCALAPPDATA")
        base = (
            Path(local_app_data)
            if local_app_data
            else user_home / "AppData" / "Local"
        )
        root = base / "Programs" / "ManagedCliDemo"
        return InstallLayout(selected, root, root / "bin")
    if selected == "macos":
        root = user_home / "Library" / "Application Support" / "ManagedCliDemo"
        return InstallLayout(selected, root, user_home / ".local" / "bin")
    data_home = env.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else user_home / ".local" / "share"
    root = base / "managed-cli-demo"
    return InstallLayout(selected, root, user_home / ".local" / "bin")


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _ensure_real_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    if absolute.exists() and _is_link_or_junction(absolute):
        raise ReleaseError(f"Installer directory must not be a link: {absolute}")
    absolute.mkdir(parents=True, exist_ok=True)
    if not absolute.is_dir():
        raise ReleaseError(f"Installer path is not a directory: {absolute}")
    return absolute


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _posix_shim(install_root: Path) -> str:
    quoted_root = shlex.quote(str(install_root))
    return f"""#!/usr/bin/env bash
set -euo pipefail

install_root={quoted_root}
pointer="$install_root/current.json"
version="$(
python3 -B - "$pointer" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
version = value.get("current")
pattern = re.compile({VERSION_PATTERN!r}, re.ASCII)
if not isinstance(version, str) or not pattern.fullmatch(version):
    raise SystemExit("The managed-cli-demo installation pointer is invalid.")
print(version)
PY
)"
target="$install_root/versions/$version/app/managed-demo.sh"
if [[ ! -x "$target" ]]; then
    printf 'The managed-cli-demo installation is incomplete: %s\\n' "$target" >&2
    exit 1
fi
exec "$target" "$@"
"""


def _powershell_shim(install_root: Path) -> str:
    escaped_root = str(install_root).replace("'", "''")
    return f"""$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$installRoot = '{escaped_root}'
$pointerPath = Join-Path $installRoot 'current.json'
$pointer = Get-Content -LiteralPath $pointerPath -Raw | ConvertFrom-Json
$version = [string]$pointer.current
if ($version -notmatch '{VERSION_PATTERN}') {{
    throw 'The managed-cli-demo installation pointer is invalid.'
}}
$versionsRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $installRoot 'versions')
).TrimEnd('\\', '/') + [System.IO.Path]::DirectorySeparatorChar
$target = [System.IO.Path]::GetFullPath(
    (Join-Path $versionsRoot "$version\\app\\managed-demo.ps1")
)
if (-not $target.StartsWith(
        $versionsRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {{
    throw 'The managed-cli-demo installation pointer escaped its version root.'
}}
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {{
    throw "The managed-cli-demo installation is incomplete: $target"
}}
& $target @args
exit $LASTEXITCODE
"""


def _cmd_shim() -> str:
    return """@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0managed-cli-demo.ps1" %*
exit /b %errorlevel%
"""


def create_command_shims(layout: InstallLayout) -> list[Path]:
    root = _ensure_real_directory(layout.install_root)
    bin_dir = _ensure_real_directory(layout.bin_dir)
    if layout.platform == "windows":
        ps1 = bin_dir / f"{COMMAND_NAME}.ps1"
        cmd = bin_dir / f"{COMMAND_NAME}.cmd"
        _atomic_write(ps1, _powershell_shim(root))
        _atomic_write(cmd, _cmd_shim())
        return [ps1, cmd]
    shim = bin_dir / COMMAND_NAME
    _atomic_write(shim, _posix_shim(root), 0o755)
    return [shim]


def _path_contains(path_value: str, directory: Path, separator: str) -> bool:
    target = os.path.normcase(os.path.normpath(str(directory)))
    return any(
        os.path.normcase(os.path.normpath(part.strip().strip('"'))) == target
        for part in path_value.split(separator)
        if part.strip()
    )


def _broadcast_windows_environment_change() -> None:
    try:
        send = ctypes.windll.user32.SendMessageTimeoutW
        result = ctypes.c_ulong()
        send(
            0xFFFF,
            0x001A,
            0,
            "Environment",
            0x0002,
            5000,
            ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass


def _update_windows_path(bin_dir: Path) -> PathUpdate:
    try:
        import winreg
    except ImportError as error:
        raise ReleaseError("Windows registry support is unavailable.") from error
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        if not isinstance(current, str):
            raise ReleaseError("The user PATH registry value is not a string.")
        if _path_contains(current, bin_dir, ";"):
            return PathUpdate(False, "HKCU\\Environment\\Path", False)
        updated = f"{current.rstrip(';')};{bin_dir}" if current else str(bin_dir)
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    _broadcast_windows_environment_change()
    return PathUpdate(True, "HKCU\\Environment\\Path", True)


def _profile_path(platform: str, home: Path, shell: str) -> Path:
    if platform == "macos" or shell.endswith("/zsh"):
        return home / ".zprofile"
    return home / ".profile"


def _update_posix_path(
    bin_dir: Path,
    *,
    platform: str,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> PathUpdate:
    env = environment or os.environ
    if _path_contains(env.get("PATH", ""), bin_dir, ":"):
        return PathUpdate(False, None, False)
    user_home = (home or Path.home()).expanduser()
    profile = _profile_path(platform, user_home, env.get("SHELL", ""))
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if PATH_BLOCK_START in existing or PATH_BLOCK_END in existing:
        return PathUpdate(False, str(profile), True)
    quoted = shlex.quote(str(bin_dir))
    block = (
        f"{PATH_BLOCK_START}\n"
        f'case ":$PATH:" in\n'
        f"  *:{quoted}:*) ;;\n"
        f"  *) export PATH={quoted}:$PATH ;;\n"
        f"esac\n"
        f"{PATH_BLOCK_END}\n"
    )
    separator = "" if not existing or existing.endswith("\n") else "\n"
    mode = stat.S_IMODE(profile.stat().st_mode) if profile.exists() else 0o644
    _atomic_write(profile, f"{existing}{separator}{block}", mode)
    return PathUpdate(True, str(profile), True)


def update_user_path(
    layout: InstallLayout,
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> PathUpdate:
    if layout.platform == "windows":
        return _update_windows_path(layout.bin_dir)
    return _update_posix_path(
        layout.bin_dir,
        platform=layout.platform,
        environment=environment,
        home=home,
    )


def install_command(
    *,
    repository: str,
    channel: str,
    tag: str | None,
    layout: InstallLayout,
    update_path: bool = True,
) -> tuple[Path, list[Path], PathUpdate]:
    root = _ensure_real_directory(layout.install_root)
    if tag:
        validate_release(tag)
        installed = download_and_install(
            repository=repository,
            tag=tag,
            install_root=root,
            verify_attestation=True,
            expected_prerelease=channel == "preview",
        )
    else:
        status = check_update(repository, channel, root)
        current = status["currentVersion"]
        if current is None and status["latestVersion"] is None:
            raise ReleaseError(f"No immutable {channel} release is available.")
        if current is None:
            installed = download_and_install(
                repository=repository,
                tag=str(status["latestVersion"]),
                install_root=root,
                verify_attestation=True,
                expected_prerelease=channel == "preview",
            )
        elif status["updateAvailable"]:
            _status, updated = update_from_channel(repository, channel, root)
            if updated is None:
                raise ReleaseError("The selected update was not installed.")
            installed = updated
        else:
            installed = root / "versions" / str(current)
    shims = create_command_shims(layout)
    path_update = (
        update_user_path(layout)
        if update_path
        else PathUpdate(False, None, False)
    )
    return installed, shims, path_update


def main() -> int:
    default = default_layout()
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--channel", choices=("stable", "preview"), default="preview")
    parser.add_argument("--tag")
    parser.add_argument("--install-root", type=Path, default=default.install_root)
    parser.add_argument("--bin-dir", type=Path, default=default.bin_dir)
    parser.add_argument("--no-path-update", action="store_true")
    arguments = parser.parse_args()
    layout = InstallLayout(
        default.platform,
        arguments.install_root,
        arguments.bin_dir,
    )
    try:
        installed, shims, path_update = install_command(
            repository=arguments.repo,
            channel=arguments.channel,
            tag=arguments.tag,
            layout=layout,
            update_path=not arguments.no_path_update,
        )
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Installed {installed.name} at {installed}")
    print("Command launchers:")
    for shim in shims:
        print(f"  {shim}")
    if path_update.changed:
        print(f"Updated user PATH via {path_update.location}.")
    if path_update.restart_required:
        print("Open a new terminal before running managed-cli-demo.")
    else:
        print("Run: managed-cli-demo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
