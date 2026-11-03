#!/usr/bin/env python3
"""Download, verify, install, update, run, and roll back demo releases."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Callable, TextIO

from release_tools import (
    ReleaseError,
    check_update,
    download_and_install,
    download_verified_release,
    install_from_dist,
    rollback_install,
    run_installed,
    update_from_channel,
)


def prompt_update(
    repository: str,
    channel: str,
    install_root: Path,
    *,
    assume_yes: bool = False,
    input_fn: Callable[[str], str] | None = None,
    report_current: bool = True,
    output: TextIO = sys.stdout,
) -> tuple[dict[str, object], Path | None]:
    status = check_update(repository, channel, install_root)
    if not status["updateAvailable"]:
        if report_current:
            print(json.dumps(status, indent=2, sort_keys=True), file=output)
        return status, None

    current = status["currentVersion"] or "not-installed"
    latest = status["latestVersion"]
    print(f"Update available: {current} -> {latest}", file=output)
    if status["releaseUrl"]:
        print(f"Release: {status['releaseUrl']}", file=output)

    approved = assume_yes
    if not assume_yes:
        if input_fn is None and not sys.stdin.isatty():
            print(
                "Update skipped: interactive confirmation is required.",
                file=output,
            )
            return {**status, "reason": "confirmation-required"}, None
        reader = input_fn or input
        answer = reader("Install this update now? [y/N] ").strip().casefold()
        approved = answer in ("y", "yes")

    if not approved:
        print("Update declined; continuing with the current version.", file=output)
        return {**status, "reason": "declined"}, None

    reviewed_version = str(latest)
    updated_status, path = update_from_channel(
        repository,
        channel,
        install_root,
        expected_version=reviewed_version,
    )
    if path is None:
        print(json.dumps(updated_status, indent=2, sort_keys=True), file=output)
    else:
        print(f"Updated to {path.name}.", file=output)
    return updated_status, path


def startup(
    release_root: Path,
    install_root: Path,
    channel: str,
    arguments: list[str],
) -> int:
    info_path = release_root / "release-info.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        repository = info["sourceRepository"]
        if not isinstance(repository, str):
            raise TypeError("sourceRepository must be a string")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Installed release identity is invalid: {error}") from error

    try:
        prompt_update(
            repository,
            channel,
            install_root,
            report_current=False,
        )
    except ReleaseError as error:
        print(
            f"Update check failed; continuing with the current version: {error}",
            file=sys.stderr,
        )
    return run_installed(install_root, arguments)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download")
    download.add_argument("--repo", required=True)
    download.add_argument("--tag", required=True)
    download.add_argument("--install-root", type=Path, required=True)

    verify_remote = commands.add_parser("verify-remote")
    verify_remote.add_argument("--repo", required=True)
    verify_remote.add_argument("--tag", required=True)

    check = commands.add_parser("check-update")
    check.add_argument("--repo", required=True)
    check.add_argument("--channel", choices=("stable", "preview"), required=True)
    check.add_argument("--install-root", type=Path, required=True)

    update = commands.add_parser("update")
    update.add_argument("--repo", required=True)
    update.add_argument("--channel", choices=("stable", "preview"), required=True)
    update.add_argument("--install-root", type=Path, required=True)
    update.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly approve the available update without an interactive prompt.",
    )

    startup_command = commands.add_parser("startup")
    startup_command.add_argument("--release-root", type=Path, required=True)
    startup_command.add_argument("--install-root", type=Path, required=True)
    startup_command.add_argument(
        "--channel",
        choices=("stable", "preview"),
        required=True,
    )
    startup_command.add_argument("arguments", nargs=argparse.REMAINDER)

    install = commands.add_parser("install")
    install.add_argument("--dist", type=Path, required=True)
    install.add_argument("--install-root", type=Path, required=True)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--install-root", type=Path, required=True)

    run = commands.add_parser("run")
    run.add_argument("--install-root", type=Path, required=True)
    run.add_argument("arguments", nargs=argparse.REMAINDER)

    arguments = parser.parse_args()
    try:
        if arguments.command == "download":
            path = download_and_install(
                repository=arguments.repo,
                tag=arguments.tag,
                install_root=arguments.install_root,
                verify_attestation=True,
            )
            print(f"installed {path.name} at {path}")
        elif arguments.command == "verify-remote":
            with tempfile.TemporaryDirectory(
                prefix="managed-cli-verify-"
            ) as directory:
                manifest = download_verified_release(
                    repository=arguments.repo,
                    tag=arguments.tag,
                    destination=Path(directory),
                    verify_attestation=True,
                )
            print(
                f"verified {manifest['release']} from "
                f"{manifest['source']['repository']}@"
                f"{manifest['source']['commit']}"
            )
        elif arguments.command == "check-update":
            status = check_update(
                repository=arguments.repo,
                channel=arguments.channel,
                install_root=arguments.install_root,
            )
            print(json.dumps(status, indent=2, sort_keys=True))
        elif arguments.command == "update":
            prompt_update(
                repository=arguments.repo,
                channel=arguments.channel,
                install_root=arguments.install_root,
                assume_yes=arguments.yes,
            )
        elif arguments.command == "startup":
            return startup(
                release_root=arguments.release_root,
                install_root=arguments.install_root,
                channel=arguments.channel,
                arguments=arguments.arguments,
            )
        elif arguments.command == "install":
            path = install_from_dist(arguments.dist, arguments.install_root)
            print(f"installed {path.name} at {path}")
        elif arguments.command == "rollback":
            path = rollback_install(arguments.install_root)
            print(f"activated {path.name} at {path}")
        elif arguments.command == "run":
            return run_installed(arguments.install_root, arguments.arguments)
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
