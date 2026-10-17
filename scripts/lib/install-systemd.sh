#!/usr/bin/env bash
# Shared Debian/Ubuntu systemd installation, after source acquisition/preflight.
# Sourced from a validated source bundle; never downloaded independently.
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
bsm_install_systemd() {
    local source_dir=$1 work=$2 ref=$3 package_version=$4 config=$5 install_unit=$6
    local version_dir package_dir service_unit link unit
    # Each bootstrap gets a permanent venv so existing CLI installations remain intact.
    install -d -m 0755 /opt/bsm-supervisor /opt/bsm-supervisor/versions
    version_dir=$(mktemp -d /opt/bsm-supervisor/versions/install-XXXXXXXX)
    chmod 0755 "$version_dir"
    python3 -m venv "$version_dir"
    # Build release metadata from a private copy, preserving local checkouts.
    [[ -n $package_version || ! $ref =~ ^v[0-9] ]] || package_version=${ref#v}
    package_dir=$source_dir
    if [[ -n $package_version ]]; then
        package_dir=$work/package
        mkdir "$package_dir"
        cp -R -- "$source_dir/src" "$source_dir/pyproject.toml" "$source_dir/README.md" "$source_dir/LICENSE" "$package_dir/"
        "$version_dir/bin/python" -I -m pip --isolated install --index-url https://pypi.org/simple 'packaging>=24,<27'
        "$version_dir/bin/python" -I - "$package_dir" "$package_version" <<'PYVERSION'
import pathlib, re, sys, tomllib
from packaging.version import Version
version = str(Version(sys.argv[2]))
if version != sys.argv[2]:
    raise SystemExit('Package version must be normalized')
root = pathlib.Path(sys.argv[1])
project = root / 'pyproject.toml'
if project.exists() and 'setuptools_scm' in tomllib.loads(project.read_text()).get('tool', {}):
    raise SystemExit(0)
# Older release archives used a static module attribute.
path = root / 'src/bsm_supervisor/__init__.py'
content, count = re.subn(r'^__version__ = "[^\"]+"$', f'__version__ = "{version}"', path.read_text(), count=1, flags=re.MULTILINE)
if count != 1:
    raise SystemExit('Missing package version declaration')
path.write_text(content)
PYVERSION
        export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSM_SUPERVISOR="$package_version"
    fi
    "$version_dir/bin/python" -I -m pip --isolated install --index-url https://pypi.org/simple "${package_dir}[all]"
    "$version_dir/bin/python" -I -m pip check
    if ! id bsm >/dev/null 2>&1; then
        useradd --system --user-group --home-dir /srv/bsm --shell /usr/sbin/nologin bsm
    fi
    install -d -m 0700 /var/lib/bsm-supervisor
    install -d -m 0755 /etc/bsm-supervisor /opt/bsm/installations
    # Avoid changing ownership of adopted BSM data.
    if [[ ! -e /srv/bsm ]]; then install -d -o bsm -g "$(id -gn bsm)" -m 0750 /srv/bsm; fi
    if [[ ! -e /srv/bsm/config ]]; then install -d -o bsm -g "$(id -gn bsm)" -m 0750 /srv/bsm/config; fi
    python3 - "$config" <<'PYCONFIG'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
if not path.is_absolute():
    raise SystemExit('Configuration path must be absolute')
path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
PYCONFIG
    if [[ ! -e $config ]]; then
        install -m 0600 "$source_dir/config.example.toml" "$config"
    fi
    "$version_dir/bin/bsm-supervisor" --config "$config" check
    "$version_dir/bin/bsm-supervisor" --config "$config" init
    "$version_dir/bin/python" -I - "$config" <<'PYSETUP'
import os, pwd, sys
from pathlib import Path
from bsm_supervisor.config import Config
config = Config.load(Path(sys.argv[1]))
account = pwd.getpwnam('bsm')
for path in (config.data_dir, config.config_dir):
    if not path.exists():
        path.mkdir(mode=0o750, parents=True)
        os.chown(path, account.pw_uid, account.pw_gid)
PYSETUP
    service_unit=$("$version_dir/bin/python" -I -c 'import sys; from pathlib import Path; from bsm_supervisor.config import Config; print(Config.load(Path(sys.argv[1])).service_unit)' "$config")
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
    # Record source identity in the permanent environment before publishing it.
    "$version_dir/bin/python" -I - "$source_dir" "$version_dir" "$ref" <<'PYREVISION'
import json, pathlib, re, subprocess, sys
source, target, requested = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
revision, dirty = None, None
match = re.search(r'-([0-9a-f]{40})$', source.name)
if match:
    revision = match.group(1)
elif (source / '.git').exists():
    result = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', result.stdout.strip()):
        revision = result.stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', str(source), 'status', '--porcelain'], capture_output=True, text=True).stdout)
record = {'source': 'github' if match else 'local', 'ref': requested, 'revision': revision, 'dirty': dirty}
if not match and not (source / '.git').exists() and (source / 'supervisor-source.json').is_file():
    supplied = json.loads((source / 'supervisor-source.json').read_text())
    if (supplied.get('source') not in ('github', 'local')
            or (supplied.get('revision') is not None and not re.fullmatch(r'[0-9a-f]{40}', str(supplied['revision'])))
            or not isinstance(supplied.get('ref'), str)
            or len(supplied['ref']) > 255
            or supplied.get('dirty') not in (None, True, False)):
        raise SystemExit('Invalid supervisor source provenance')
    record = {key: supplied.get(key) for key in ('source', 'ref', 'revision', 'dirty')}
(target / 'supervisor-installation.json').write_text(json.dumps(record, indent=2) + '\n')
PYREVISION
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
    if $install_unit; then
        echo 'Next: install a BSM release, activate its installation ID, then start BSM.'
        echo 'Or run: bsm-supervisor setup --offline-confirmed'
        echo 'Complete BSM user setup in the browser; local readiness authentication is automatic on supported BSM versions.'
    else
        echo 'Existing BSM selection and service state were preserved.'
        echo 'When BSM is stopped, run: bsm-supervisor unit-refresh --offline-confirmed'
    fi
    echo 'See docs/install-lxc.md and docs/update-supervisor.md.'

}
