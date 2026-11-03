#!/usr/bin/env python3
"""Download, verify, install, run, and roll back demo releases."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

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
            status, path = update_from_channel(
                repository=arguments.repo,
                channel=arguments.channel,
                install_root=arguments.install_root,
            )
            if path is None:
                print(json.dumps(status, indent=2, sort_keys=True))
            else:
                print(
                    f"updated {status['currentVersion'] or 'not-installed'} "
                    f"to {path.name} at {path}"
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
