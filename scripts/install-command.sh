#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        if command -v py.exe >/dev/null 2>&1; then
            exec py.exe -3 -B "$script_dir/install_command.py" "$@"
        fi
        if command -v python.exe >/dev/null 2>&1; then
            exec python.exe -B "$script_dir/install_command.py" "$@"
        fi
        printf 'Python 3 is required. Install it and reopen Git Bash.\n' >&2
        exit 1
        ;;
    *)
        exec python3 -B "$script_dir/install_command.py" "$@"
        ;;
esac
