#!/usr/bin/env bash
# Bootstrap bsm-supervisor in a Debian/Ubuntu LXC. Never starts BSM.
set -euo pipefail
umask 022
ref=dev
source_dir=
install_unit=false
usage() {
    echo 'Usage: install-lxc.sh [--ref BRANCH_OR_COMMIT] [--source-dir CHECKOUT] [--install-bsm-unit]'
    echo 'Private repository downloads use GH_TOKEN (or an interactive hidden prompt).'
}
# Publish only our own launchers; never replace an unrelated system command.
publish_launcher() {
    local destination=$1 target=/opt/bsm-supervisor/current/bin/bsm-supervisor
    if [[ -e $destination || -L $destination ]]; then
        if [[ ! -L $destination ]] || [[ $(readlink -- "$destination") != "$target" ]]; then
            echo "Refusing to replace unrelated launcher: $destination" >&2
            return 1
        fi
    fi
    ln -sfn -- "$target" "$destination"
}
while (($#)); do
    case "$1" in
        --ref|--source-dir)
            (($# >= 2)) || { usage; exit 2; }
            if [[ $1 == --ref ]]; then ref=$2; else source_dir=$2; fi
            shift 2 ;;
        --install-bsm-unit) install_unit=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ $EUID == 0 ]] || { echo 'Run this script as root inside the LXC.' >&2; exit 1; }
[[ $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && $ref != *..* ]] || { echo 'Invalid ref.' >&2; exit 2; }
[[ -d /run/systemd/system ]] || { echo 'A running systemd LXC is required.' >&2; exit 1; }
exec 9>/run/lock/bsm-supervisor-install.lock
flock --nonblock 9 || { echo 'Another supervisor installer is running.' >&2; exit 1; }
command -v apt-get >/dev/null || { echo 'Debian/Ubuntu with apt-get is required.' >&2; exit 1; }
# Refuse unit replacement before making changes.
unit=/etc/systemd/system/bedrock-server-manager-webui.service
if $install_unit && systemctl cat bedrock-server-manager-webui.service >/dev/null 2>&1; then
    echo 'An existing BSM unit was found; review/adopt it manually. Nothing replaced.' >&2
    exit 1
fi
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
trap 'rm -rf -- "$work"' EXIT
if [[ -z $source_dir ]]; then
    token=${GH_TOKEN:-}
    if [[ -z $token && -t 0 ]]; then
        read -r -s -p 'GitHub token with read access to bsm-supervisor: ' token
        echo
    fi
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
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 20 --max-time 300 --config "$work/auth" \
        "https://api.github.com/repos/DMedina559/bsm-supervisor/tarball/$ref" -o "$work/source.tar.gz"
    rm -f "$work/auth"
    python3 - "$work" <<'PY'
import pathlib, sys, tarfile
work = pathlib.Path(sys.argv[1])
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
(work / 'source-path').write_text(str(roots[0]))
PY
    source_dir=$(cat "$work/source-path")
fi
unset GH_TOKEN
[[ -f $source_dir/pyproject.toml && -d $source_dir/src/bsm_supervisor ]] || { echo 'Invalid supervisor source directory.' >&2; exit 1; }
# Each bootstrap gets a permanent venv so existing CLI installations remain intact.
install -d -m 0755 /opt/bsm-supervisor /opt/bsm-supervisor/versions
version_dir=$(mktemp -d /opt/bsm-supervisor/versions/install-XXXXXXXX)
chmod 0755 "$version_dir"
python3 -m venv "$version_dir"
"$version_dir/bin/python" -I -m pip --isolated install --index-url https://pypi.org/simple "$source_dir"
"$version_dir/bin/python" -I -m pip check
if ! id bsm >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /srv/bsm --shell /usr/sbin/nologin bsm
fi
install -d -m 0700 /var/lib/bsm-supervisor
install -d -m 0755 /etc/bsm-supervisor /opt/bsm/installations
# Avoid changing ownership of adopted BSM data.
if [[ ! -e /srv/bsm ]]; then install -d -o bsm -g "$(id -gn bsm)" -m 0750 /srv/bsm; fi
if [[ ! -e /srv/bsm/config ]]; then install -d -o bsm -g "$(id -gn bsm)" -m 0750 /srv/bsm/config; fi
config=/etc/bsm-supervisor/config.toml
if [[ ! -e $config ]]; then
    install -m 0600 "$source_dir/config.example.toml" "$config"
fi
"$version_dir/bin/bsm-supervisor" --config "$config" check
"$version_dir/bin/bsm-supervisor" --config "$config" init
"$version_dir/bin/python" -I - <<'PYSETUP'
import os, pwd
from pathlib import Path
from bsm_supervisor.config import Config
config = Config.load(Path('/etc/bsm-supervisor/config.toml'))
account = pwd.getpwnam('bsm')
for path in (config.data_dir, config.config_dir):
    if not path.exists():
        path.mkdir(mode=0o750, parents=True)
        os.chown(path, account.pw_uid, account.pw_gid)
PYSETUP
service_unit=$("$version_dir/bin/python" -I -c 'from pathlib import Path; from bsm_supervisor.config import Config; print(Config.load(Path("/etc/bsm-supervisor/config.toml")).service_unit)')
unit=/etc/systemd/system/$service_unit
if $install_unit && systemctl cat "$service_unit" >/dev/null 2>&1; then
    echo 'The configured BSM unit already exists; omit --install-bsm-unit to preserve it.' >&2
    exit 1
fi
if $install_unit; then
    "$version_dir/bin/bsm-supervisor" --config "$config" service-environment --offline-confirmed
    "$version_dir/bin/bsm-supervisor" --config "$config" unit --user bsm > "$work/bsm.service"
    systemd-analyze verify --man=no "$work/bsm.service"
fi
# Publish only after package, configuration, and candidate unit validation succeed.
link=/opt/bsm-supervisor/.current-$$
ln -s "$version_dir" "$link"
mv -Tf "$link" /opt/bsm-supervisor/current
publish_launcher /usr/local/bin/bsm-supervisor
# pct enter can inherit a minimal PATH and does not necessarily load login profiles.
publish_launcher /usr/bin/bsm-supervisor
PATH=/usr/bin:/bin command -v bsm-supervisor >/dev/null
/usr/bin/bsm-supervisor --help >/dev/null
if $install_unit; then
    install -m 0644 "$work/bsm.service" "$unit"
    systemctl daemon-reload
fi
printf '\nSupervisor installed. Configuration: %s\n' "$config"
echo 'BSM has not been started. Install/select a BSM version, configure its bearer token,'
echo 'and review the generated unit before starting BSM. See docs/install-lxc.md.'
