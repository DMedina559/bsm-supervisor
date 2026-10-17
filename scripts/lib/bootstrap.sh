#!/usr/bin/env bash
# Common bootstrap functions embedded into standalone entry points.
bsm_stage() {
    bsm_current_stage=$1
    printf '\n[%s] %s\n' "$(date -u +%H:%M:%S)" "$bsm_current_stage"
}
bsm_log_begin() {
    local kind=$1
    local directory=${BSM_SUPERVISOR_LOG_DIR:-/var/log/bsm-supervisor}
    mkdir -p -- "$directory"
    chmod 0700 "$directory"
    bsm_log=$(umask 077; mktemp "$directory/$kind-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.log")
    exec > >(tee -a "$bsm_log") 2>&1
    bsm_started=$SECONDS
    bsm_stage 'Preflight checks'
    trap 'printf "Failed during %s (line %s). Inspect %s\n" "${bsm_current_stage:-startup}" "$LINENO" "$bsm_log" >&2' ERR
}
bsm_finish() {
    local result=$1
    if [[ -n ${bsm_log:-} ]]; then
        printf '\nResult: %s | Stage: %s | Elapsed: %ss | Log: %s\n' "$result" "${bsm_current_stage:-startup}" "$((SECONDS - bsm_started))" "$bsm_log"
    fi
}
bsm_linux_preflight() {
    local config=$1 path available
    for path in /opt /var/lib /srv; do
        available=$(df -Pk "$path" | awk 'NR==2 {print $4}')
        [[ $available =~ ^[0-9]+$ && $available -ge 524288 ]] || {
            echo "At least 512 MiB free is required on $path." >&2; return 1;
        }
    done
    [[ $config == /* && ! -L $config ]] || { echo 'Configuration must be an absolute, non-symlink path.' >&2; return 1; }
    if command -v python3 >/dev/null; then
        python3 - "$config" <<'PYPREFLIGHT'
import pathlib, sys
if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ is required; use Debian 12+ or Ubuntu 24.04+')
import tomllib
path = pathlib.Path(sys.argv[1])
if path.exists():
    values = tomllib.loads(path.read_text())
    roots = [pathlib.Path(values[key]).resolve() for key in ('state_dir', 'installations_dir', 'data_dir')]
    if any(left == right or left in right.parents or right in left.parents
           for index, left in enumerate(roots) for right in roots[index + 1:]):
        raise SystemExit('State, installation and data directories must not overlap')
PYPREFLIGHT
    fi
}
github_download() {
    local url=$1 destination=$2 status work=${work:?Bootstrap work directory required}
    if status=$(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --retry 2 --retry-max-time 60 --connect-timeout 20 --max-time 300 --write-out '%{http_code}' --config "$work/auth" \
        "$url" -o "$destination"); then return; fi
    if [[ ! -s $work/auth && $status == 404 ]]; then
        local visibility
        visibility=$(curl --silent --output /dev/null --write-out '%{http_code}' \
            --connect-timeout 10 --max-time 20 --proto '=https' \
            'https://api.github.com/repos/DMedina559/bsm-supervisor') || visibility=000
        if [[ $visibility == 200 ]]; then
            echo 'Public repository resource not found. Choose a published release tag or --ref dev.' >&2
            return 1
        fi
    fi
    if [[ ! -s $work/auth && -t 0 && ( $status == 401 || $status == 403 || $status == 404 ) ]]; then
        read -r -s -p 'GitHub token for private repository access: ' token
        echo
        [[ $token =~ ^[A-Za-z0-9_]+$ ]] || { echo 'Invalid token format.' >&2; return 1; }
        printf 'header = "Authorization: Bearer %s"\n' "$token" > "$work/auth"
        unset token
        curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
            --retry 2 --retry-max-time 60 --connect-timeout 20 --max-time 300 --config "$work/auth" "$url" -o "$destination"
    else
        echo 'Download failed. Check the release/ref and network; private repositories require GH_TOKEN.' >&2
        return 1
    fi
}
