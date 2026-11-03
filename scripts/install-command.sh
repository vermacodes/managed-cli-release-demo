#!/usr/bin/env bash
set -euo pipefail

version="v0.5.0"

script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        temporary="$(mktemp -d "${TMPDIR:-/tmp}/managed-cli-installer.XXXXXXXX")"
        trap 'rm -rf -- "$temporary"' EXIT
        if [[ -n "$script_dir" && -f "$script_dir/Install-Command.ps1" ]]; then
            powershell_script="$script_dir/Install-Command.ps1"
        else
            powershell_script="$temporary/Install-Command.ps1"
            curl --proto '=https' --tlsv1.2 -fsSLo "$powershell_script" \
                "https://raw.githubusercontent.com/vermacodes/managed-cli-release-demo/$version/scripts/Install-Command.ps1"
        fi
        if command -v cygpath >/dev/null 2>&1; then
            powershell_script="$(cygpath -w "$powershell_script")"
        fi
        exec powershell.exe -NoLogo -NoProfile -File "$powershell_script" "$@"
        ;;
esac

command -v node >/dev/null 2>&1 || {
    printf 'Node.js is required.\n' >&2
    exit 1
}
command -v gh >/dev/null 2>&1 || {
    printf 'GitHub CLI is required for offline signature verification.\n' >&2
    exit 1
}

if [[ -n "$script_dir" &&
      -f "$script_dir/install-command.js" &&
      -f "$script_dir/release-runtime.js" ]]; then
    installer="$script_dir/install-command.js"
    exec node "$installer" "$@"
fi

temporary="$(mktemp -d "${TMPDIR:-/tmp}/managed-cli-installer.XXXXXXXX")"
cleanup() {
    rm -rf -- "$temporary"
}
trap cleanup EXIT
base="https://raw.githubusercontent.com/vermacodes/managed-cli-release-demo/$version/scripts"
for file in install-command.js release-runtime.js; do
    curl --proto '=https' --tlsv1.2 -fsSLo "$temporary/$file" "$base/$file"
done
node "$temporary/install-command.js" "$@"
