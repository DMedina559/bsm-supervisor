#!/usr/bin/env bash
# Bootstrap bsm-supervisor on Debian/Ubuntu with systemd. Never starts BSM.
set -Eeuo pipefail
umask 022
# BEGIN SHARED BOOTSTRAP
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
# END SHARED BOOTSTRAP
ref=latest
source_dir=
package_version=
initial_ref=$ref
package_version_explicit=false
install_unit=false
check_only=false
config=/etc/bsm-supervisor/config.toml
usage() {
    echo "Usage: ${BSM_INSTALL_ENTRY:-install-lxc.sh} [--ref BRANCH_OR_COMMIT] [--source-dir CHECKOUT] [--install-bsm-unit] [--check] [--config PATH] [--package-version VERSION]"
    echo 'Private repository downloads use GH_TOKEN (or an interactive hidden prompt).'
}
while (($#)); do
    case "$1" in
        --ref|--source-dir|--config|--package-version)
            (($# >= 2)) || { usage; exit 2; }
            [[ $1 != --package-version ]] || package_version_explicit=true
            if [[ $1 == --ref ]]; then ref=$2; elif [[ $1 == --config ]]; then config=$2; elif [[ $1 == --package-version ]]; then package_version=$2; else source_dir=$2; fi
            shift 2 ;;
        --check) check_only=true; shift ;;
        --install-bsm-unit) install_unit=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if [[ $ref != "$initial_ref" ]] && ! $package_version_explicit; then package_version=; fi
[[ $EUID == 0 ]] || { echo 'Run this script as root on the target Linux system.' >&2; exit 1; }
[[ $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && $ref != *..* ]] || { echo 'Invalid ref.' >&2; exit 2; }
[[ -d /run/systemd/system ]] || { echo 'A running systemd instance is required.' >&2; exit 1; }
if $check_only; then bsm_linux_preflight "$config"; echo 'Preflight checks passed; no installation performed.'; exit 0; fi
bsm_log_begin install
trap 'bsm_finish $?' EXIT
bsm_linux_preflight "$config"
exec 9>/run/lock/bsm-supervisor-install.lock
flock --nonblock 9 || { echo 'Another supervisor installer is running.' >&2; exit 1; }
command -v apt-get >/dev/null || { echo 'Debian/Ubuntu with apt-get is required.' >&2; exit 1; }
# Refuse unit replacement before making changes.
unit=/etc/systemd/system/bedrock-server-manager-webui.service
if $install_unit && systemctl cat bedrock-server-manager-webui.service >/dev/null 2>&1; then
    echo 'An existing BSM unit was found; review/adopt it manually. Nothing replaced.' >&2
    exit 1
fi
bsm_stage 'Distribution dependencies'
export DEBIAN_FRONTEND=noninteractive
# Root can resolve successfully even when APT's sandbox cannot read resolver files.
if id _apt >/dev/null 2>&1; then
    for resolver_file in /etc/resolv.conf /etc/nsswitch.conf; do
        runuser -u _apt -- test -r "$resolver_file" || {
            echo "APT user cannot read $resolver_file. Check /etc traversal and resolver file permissions." >&2
            exit 1
        }
    done
fi
apt-get -o APT::Update::Error-Mode=any update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip git curl ca-certificates util-linux
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required (Debian 12+/Ubuntu 24.04+)"'
work=$(mktemp -d)
trap 'result=$?; rm -rf -- "$work"; bsm_finish "$result"' EXIT
bsm_stage 'Acquire pinned supervisor source'
if [[ -z $source_dir ]]; then
    token=${GH_TOKEN:-}
    # Curl config keeps the token out of process arguments and redirect requests.
    python3 - "$work/auth" <<'PY'
import pathlib
path = pathlib.Path(__import__('sys').argv[1])
path.touch(mode=0o600)
PY
    if [[ -n $token ]]; then
        [[ $token =~ ^[A-Za-z0-9_]+$ ]] || { echo 'Invalid token format.' >&2; exit 1; }
        printf 'header = "Authorization: Bearer %s"\n' "$token" > "$work/auth"
    fi
    unset token GH_TOKEN
    # API redirect is to codeload; curl does not forward Authorization across hosts.

    if [[ $ref == latest ]]; then
        github_download 'https://api.github.com/repos/DMedina559/bsm-supervisor/releases/latest' "$work/release.json"
        ref=$(python3 - "$work/release.json" <<'PYTAG'
import json, re, sys
value = json.load(open(sys.argv[1]))
tag = value.get('tag_name', '')
if value.get('draft') or value.get('prerelease') or not re.fullmatch(r'v[0-9][A-Za-z0-9.+!-]*', tag):
    raise SystemExit('No eligible stable supervisor release; select an explicit prerelease tag or --ref dev')
print(tag)
PYTAG
)
    fi
    # Resolve once; archive directory names may contain only an abbreviated SHA.
    github_download "https://api.github.com/repos/DMedina559/bsm-supervisor/commits/$ref" "$work/commit.json"
    resolved_revision=$(python3 - "$work/commit.json" "$ref" <<'PYCOMMIT'
import json, re, sys
value = json.load(open(sys.argv[1]))
revision = value.get('sha') if isinstance(value, dict) else None
if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
    raise SystemExit('GitHub did not return a full source revision')
if re.fullmatch(r'[0-9a-f]{40}', sys.argv[2]) and revision != sys.argv[2]:
    raise SystemExit('GitHub source revision differs from requested commit')
print(revision)
PYCOMMIT
)
    github_download "https://api.github.com/repos/DMedina559/bsm-supervisor/tarball/$resolved_revision" "$work/source.tar.gz"
    rm -f "$work/auth"
    python3 - "$work" "$resolved_revision" "$ref" <<'PYSOURCE'
import json, pathlib, re, sys, tarfile
work = pathlib.Path(sys.argv[1])
revision, requested = sys.argv[2:]
if not re.fullmatch(r'[0-9a-f]{40}', revision):
    raise SystemExit('Invalid resolved source revision')
with tarfile.open(work / 'source.tar.gz') as archive:
    for entry in archive.getmembers():
        path = pathlib.PurePosixPath(entry.name)
        if path.is_absolute() or '..' in path.parts or not (entry.isfile() or entry.isdir()):
            raise SystemExit('Unsafe source archive')
    options = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
    archive.extractall(work / 'source', **options)
roots = list((work / 'source').iterdir())
if len(roots) != 1 or not (roots[0] / 'pyproject.toml').is_file():
    raise SystemExit('Invalid source checkout')
match = re.fullmatch(r'DMedina559-bsm-supervisor-([0-9a-f]{7,40})', roots[0].name)
if match is None or not revision.startswith(match.group(1)):
    raise SystemExit('Source archive identity differs from the resolved commit')
record = {'source': 'github', 'ref': requested, 'revision': revision, 'dirty': None}
(roots[0] / 'supervisor-source.json').write_text(json.dumps(record) + '\n')
(work / 'source-path').write_text(str(roots[0]))
PYSOURCE
    source_dir=$(cat "$work/source-path")
fi
unset GH_TOKEN
[[ -f $source_dir/pyproject.toml && -d $source_dir/src/bsm_supervisor ]] || { echo 'Invalid supervisor source directory.' >&2; exit 1; }
# The source bundle owns installation behavior; the downloaded entry stays standalone.
engine="$source_dir/scripts/lib/install-systemd.sh"
if [[ ! -f $engine ]]; then
    # Releases predating the shared engine retain their own standalone installer.
    legacy="$source_dir/scripts/install-lxc.sh"
    [[ -f $legacy ]] || { echo 'Source bundle has no supported installer.' >&2; exit 1; }
    if grep -q '^bsm_install_systemd ' "$legacy"; then
        echo 'Incomplete source bundle: shared installation module is missing.' >&2
        exit 1
    fi
    flock --unlock 9
    arguments=(--source-dir "$source_dir" --ref "$ref" --package-version "$package_version" --config "$config")
    $install_unit && arguments+=(--install-bsm-unit)
    bash "$legacy" "${arguments[@]}" 9>&-
    exit
fi
# shellcheck source=scripts/lib/install-systemd.sh
source "$engine"
bsm_install_systemd "$source_dir" "$work" "$ref" "$package_version" "$config" "$install_unit"
