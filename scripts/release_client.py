#!/usr/bin/env python3
"""Download, verify, install, run, and roll back demo releases."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from release_tools import (
    ReleaseError,
    download_and_install,
    install_from_dist,
    rollback_install,
    run_installed,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download")
    download.add_argument("--repo", required=True)
    download.add_argument("--tag", required=True)
    download.add_argument("--install-root", type=Path, required=True)
    download.add_argument("--verify-attestation", action="store_true")

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
                verify_attestation=arguments.verify_attestation,
            )
            print(f"installed {path.name} at {path}")
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
