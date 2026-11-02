#!/usr/bin/env python3
"""Build deterministic release assets from an exact Git commit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from release_tools import ReleaseError, build_release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-ref", default="HEAD")
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    try:
        manifest = build_release(
            repo_root=repo_root,
            output_dir=arguments.output,
            release=arguments.version,
            repository=arguments.repository,
            source_ref=arguments.source_ref,
            force=arguments.force,
        )
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        f"built {manifest['artifact']['name']} "
        f"({manifest['artifact']['sizeBytes']} bytes, "
        f"sha256:{manifest['artifact']['sha256']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
