#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
release_root="$(cd -- "$script_dir/.." && pwd)"
release_client="$release_root/scripts/release_client.py"

if [[ -f "$release_root/release-info.json" && -f "$release_client" ]]; then
    versions_root="$(dirname -- "$release_root")"
    install_root="$(dirname -- "$versions_root")"
    channel="${MANAGED_CLI_UPDATE_CHANNEL:-preview}"
    exec python3 "$release_client" startup \
        --release-root "$release_root" \
        --install-root "$install_root" \
        --channel "$channel" \
        -- "$@"
fi

exec node "$script_dir/managed-demo.js" "$@"
