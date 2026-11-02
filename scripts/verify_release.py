#!/usr/bin/env python3
"""Verify a complete release asset directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from release_tools import ReleaseError, verify_release_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    arguments = parser.parse_args()
    try:
        manifest = verify_release_dir(arguments.dist)
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        f"verified {manifest['release']} from "
        f"{manifest['source']['repository']}@{manifest['source']['commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
