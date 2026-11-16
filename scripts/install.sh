#!/usr/bin/env bash
# Generic Linux entry point from a complete source bundle.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f $script_dir/install-lxc.sh ]] || {
    echo 'Run install.sh from a complete source bundle; use install-lxc.sh for a single-file bootstrap.' >&2
    exit 1
}
# The legacy entry remains the self-contained download/bootstrap interface.
export BSM_INSTALL_ENTRY=install.sh
exec bash "$script_dir/install-lxc.sh" "$@"
