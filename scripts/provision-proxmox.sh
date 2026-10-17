#!/usr/bin/env bash
# Run on a Proxmox VE host; create a new unprivileged LXC and bootstrap supervisor.
set -Eeuo pipefail
umask 077
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
ctid=
hostname=bsm-supervisor
bsm_host=0.0.0.0
bsm_host_explicit=0
bsm_env_file=
storage=local-lvm
template_storage=local
template=
bridge=vmbr0
address=dhcp
gateway=
nameserver=
vlan=
cores=2
memory=2048
swap=512
disk=16
onboot=0
ref=latest
source_dir=
package_version=
initial_ref=$ref
package_version_explicit=false
dry_run=false
creation_attempted=false
interactive=false
resume=false
resume_id=
profile=
save_profile=
state_root=${BSM_SUPERVISOR_PROVISION_DIR:-/var/lib/bsm-supervisor/provisioning}
if (($# == 0)) && [[ -t 0 ]]; then interactive=true; fi
work=
usage() {
    cat <<'HELP'
Usage: provision-proxmox.sh [OPTIONS]
  --profile PATH             Load public JSON defaults; CLI values override them
  --save-profile PATH        Save reusable defaults (no guest ID, static IP or secrets)
  --resume ID                Resume an owned deployment using its pinned cached bundle
  --ctid ID                  Default: next free cluster ID
  --hostname NAME            Default: bsm-supervisor
  --storage NAME             Root disk storage (default: local-lvm)
  --template VOLUME           Existing Debian 12+/Ubuntu 24.04+ template volume
  --template-storage NAME    Download storage (default: local)
                             Without --template, download latest Debian 13/12 amd64
  --bsm-env-file JSON_FILE    Public BSM environment values (private JSON object)
  --bsm-host ADDRESS         0.0.0.0 (default), 127.0.0.1, ::, or ::1
  --bridge NAME              Default: vmbr0
  --ip dhcp|IPV4/CIDR         Default: dhcp
  --gateway IPV4             Required with static --ip
  --nameserver IP            Optional DNS server; otherwise inherit host DNS
  --vlan ID                  Optional VLAN tag
  --cores N --memory MB --swap MB --disk GB
                             Defaults: 2 CPUs, 2048 MB RAM, 512 MB swap, 16 GB disk
  --onboot                   Start container when host boots
  --ref BRANCH_OR_SHA         Supervisor release/ref (default: latest stable)
  --source-dir CHECKOUT       Use local source instead of private GitHub download
  --interactive              Guided Default/Advanced setup wizard
  --profile JSON_FILE         Load reusable, non-secret defaults; CLI values override
  --save-profile JSON_FILE    Save validated reusable defaults (mode 0600)
  --resume CTID               Resume only this recorded, unchanged deployment
  --dry-run                  Read-only preflight and command preview
  --help
The container is always unprivileged. Resume requires the recorded ownership marker
and unchanged configuration. Other existing guests are never reused/deleted.
HELP
}
fail() { echo "$*" >&2; exit 1; }

# Profiles contain data only; never source shell configuration files.
load_defaults() {
    python3 - "$1" "$2" <<'PYDEFAULTS'
import json, os, pathlib, stat, sys
path, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
    raise SystemExit('Invalid provisioning defaults file')
value = json.loads(path.read_text())
keys = {'hostname','storage','template_storage','template','bridge','nameserver','vlan','cores','memory','swap','disk','onboot','ref','bsm_host'}
if mode == 'resume':
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit('Provisioning journal must be privately owned by this operator')
    if not isinstance(value, dict) or value.get('schema') != 1:
        raise SystemExit('Unsupported provisioning journal')
    value = value['settings']
    keys |= {'address','gateway'}
if not isinstance(value, dict) or value.keys() - keys:
    raise SystemExit('Unsupported provisioning defaults; guest IDs, static addresses and secrets do not belong in profiles')
for key, item in value.items():
    if type(item) not in (str, int) or len(str(item)) > 4096 or any(ord(c) < 32 for c in str(item)):
        raise SystemExit('Invalid provisioning default value')
    print(key); print(item)
PYDEFAULTS
}
scan_args=("$@")
for ((scan=0; scan<${#scan_args[@]}; scan++)); do
    case ${scan_args[scan]} in
        --profile|--resume)
            ((scan + 1 < ${#scan_args[@]})) || fail 'Missing profile or resume value.'
            if [[ ${scan_args[scan]} == --profile ]]; then profile=${scan_args[scan+1]}; else resume_id=${scan_args[scan+1]}; resume=true; fi
            ((scan+=1)) ;;
    esac
 done
[[ -z $profile || -z $resume_id ]] || fail 'Use either a profile or a recorded deployment, not both.'
if [[ -n $profile || -n $resume_id ]]; then
    if $resume; then
        [[ $resume_id =~ ^[1-9][0-9]{2,8}$ ]] || fail 'Invalid resume container ID.'
        ctid=$resume_id
        defaults=$(load_defaults "$state_root/$ctid/plan.json" resume)
        package_version=$(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])).get("package_version", ""); assert isinstance(value,str) and all(ord(c)>=32 for c in value); print(value)' "$state_root/$ctid/plan.json")
        package_version_explicit=true
    else
        defaults=$(load_defaults "$profile" profile)
    fi
    while IFS= read -r key && IFS= read -r value; do printf -v "$key" '%s' "$value"; done <<< "$defaults"
fi
settings_payload() {
    local key
    local -a entries=()
    for key in hostname storage template_storage template bridge address gateway nameserver vlan cores memory swap disk onboot ref bsm_host; do
        entries+=("$key=${!key}")
    done
    python3 - "${entries[@]}" <<'PYSETTINGS'
import json, sys
print(json.dumps(dict(value.split('=', 1) for value in sys.argv[1:]), sort_keys=True))
PYSETTINGS
}
provision_record() {
    local action=$1
    python3 - "$action" "$state_root" "$ctid" "$work" "$ref" "$package_version" <<'PYJOURNAL'
import datetime, hashlib, json, os, pathlib, shutil, socket, stat, sys, tempfile, uuid
operation, base, ctid, temporary, ref, package_version = sys.argv[1:]
base, work = pathlib.Path(base), pathlib.Path(temporary)
base.mkdir(mode=0o700, parents=True, exist_ok=True)
if base.is_symlink() or base.stat().st_uid != os.geteuid() or stat.S_IMODE(base.stat().st_mode) & 0o077:
    raise SystemExit('Provisioning state directory must be private and owned by this operator')
root = base / ctid
path = root / 'plan.json'
def sha(value): return hashlib.sha256(value).hexdigest()
def payload(name): return sha(json.dumps(json.loads((work / name).read_text()), sort_keys=True).encode())
def guest_fingerprint():
    text = (work / 'guest-config').read_text()
    marker = 'BSM Supervisor deployment ' + record['id']
    if not any(line == 'description: ' + marker for line in text.splitlines()):
        raise SystemExit('Container ownership marker does not match this deployment')
    return sha('\n'.join(sorted(line for line in text.splitlines() if not line.startswith(('description:', 'lock:')))).encode())
if operation == 'prepare':
    root.mkdir(mode=0o700)
    shutil.copyfile(work / 'bootstrap.tar.gz', root / 'bootstrap.tar.gz')
    (root / 'bootstrap.tar.gz').chmod(0o600)
    record = {'schema':1, 'id':uuid.uuid4().hex, 'ctid':ctid, 'node':socket.gethostname(),
              'phase':'prepared', 'settings':json.loads((work / 'settings.json').read_text()),
              'environment_sha256':payload('environment.json'),
              'bootstrap_sha256':sha((root / 'bootstrap.tar.gz').read_bytes()),
              'ref':ref, 'package_version':package_version, 'guest_fingerprint':None,
              'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
else:
    if root.is_symlink() or root.stat().st_uid != os.geteuid() or stat.S_IMODE(root.stat().st_mode) & 0o077 or path.is_symlink() or path.stat().st_size > 262144 or path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit('Invalid provisioning journal ownership or permissions')
    record = json.loads(path.read_text())
    if record.get('schema') != 1 or record.get('ctid') != ctid or record.get('node') != socket.gethostname():
        raise SystemExit('Deployment journal does not belong to this guest and host')
    if operation in ('verify', 'verify-empty'):
        if record['settings'] != json.loads((work / 'settings.json').read_text()) or record['environment_sha256'] != payload('environment.json'):
            raise SystemExit('Resume inputs differ; use the original settings and BSM environment file')
        cache = root / 'bootstrap.tar.gz'
        if cache.is_symlink() or cache.stat().st_uid != os.geteuid() or stat.S_IMODE(cache.stat().st_mode) & 0o077 or sha(cache.read_bytes()) != record['bootstrap_sha256']:
            raise SystemExit('Cached source bundle changed; resume refused')
        if record.get('package_version', '') != package_version or record.get('ref') != ref:
            raise SystemExit('Resume source selection differs from the recorded deployment')
        if operation == 'verify-empty':
            if record['phase'] != 'prepared' or record['guest_fingerprint'] is not None:
                raise SystemExit('A previously created container is missing; refusing to recreate it')
            print(record['id']); raise SystemExit(0)
        current = guest_fingerprint()
        if record['guest_fingerprint'] and record['guest_fingerprint'] != current:
            raise SystemExit('Container configuration changed; inspect it before recovery')
        print(record['id']); raise SystemExit(0)
    if operation == 'phase':
        print(record['phase']); raise SystemExit(0)
    if operation not in ('created', 'environment', 'installed', 'completed'):
        raise SystemExit('Invalid provisioning phase')
    record['phase'] = operation
    record['guest_fingerprint'] = guest_fingerprint()
record['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
fd, name = tempfile.mkstemp(dir=root)
with os.fdopen(fd, 'w') as stream:
    json.dump(record, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
os.replace(name, path)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try: os.fsync(directory)
finally: os.close(directory)
print(record['id'])
PYJOURNAL
}

while (($#)); do
    case "$1" in
        --profile|--resume) shift 2 ;;
        --interactive) interactive=true; shift ;;
        --dry-run) dry_run=true; shift ;;
        --onboot) onboot=1; shift ;;
        --help|-h) usage; exit 0 ;;
        --save-profile|--ctid|--bsm-env-file|--bsm-host|--hostname|--storage|--template-storage|--template|--bridge|--ip|--gateway|--nameserver|--vlan|--cores|--memory|--swap|--disk|--ref|--source-dir|--package-version)
            (($# >= 2)) || fail "Missing value for $1"
            [[ $1 != --package-version ]] || package_version_explicit=true
            [[ $1 != --bsm-host ]] || bsm_host_explicit=1
            option=${1#--}; option=${option//-/_}
            [[ $option != ip ]] || option=address
            printf -v "$option" '%s' "$2"
            shift 2 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done
if $resume; then [[ $ctid == "$resume_id" && -z $source_dir ]] || fail 'Resume requires its recorded ID and cached source; do not pass --source-dir.'; fi
[[ $EUID == 0 ]] || $dry_run || fail 'Run as root on the Proxmox host.'
for command in pct pvesh pvesm pveversion python3 ip tar flock perl; do
    command -v "$command" >/dev/null || fail "Missing Proxmox host prerequisite: $command"
done
# Proxmox inherits the caller's umask during template extraction and guest setup.
# Keep host credentials private without applying that mask to guest system files.
pct() {
    (umask 022; command pct "$@")
}

host_version=$(pveversion)
[[ $host_version =~ pve-manager/([0-9]+)\.([0-9]+)\. ]] || fail 'Cannot identify Proxmox version.'
host_major=${BASH_REMATCH[1]}
host_minor=${BASH_REMATCH[2]}
((host_major > 9 || (host_major == 9 && host_minor >= 2))) || fail 'The host provisioner targets Proxmox VE 9.2+.'
host_help=$(pct help create 2>&1)
[[ $host_help == *--env* ]] || fail 'This Proxmox build lacks native container environment support.'

# Self-contained community-script style wizard; no remote helper code is sourced.
ask() {
    local variable=$1 title=$2 fallback=$3 answer
    if command -v whiptail >/dev/null; then
        answer=$(whiptail --title 'BSM Supervisor LXC' --inputbox "$title" 10 76 "$fallback" 3>&1 1>&2 2>&3) || exit 0
    else
        read -r -p "$title [$fallback]: " answer || exit 0
    fi
    printf -v "$variable" '%s' "${answer:-$fallback}"
}
choose_storage() {
    local variable=$1 content=$2 fallback=$3 answer row
    local -a choices=()
    while read -r row; do
        [[ -z $row ]] || choices+=("$row" 'Available storage')
    done < <(pvesm status --content "$content" | awk 'NR > 1 && $3 == "active" {print $1}')
    ((${#choices[@]})) || fail "No active $content storage found."
    if command -v whiptail >/dev/null; then
        answer=$(whiptail --title 'BSM Supervisor LXC' --menu "Select $content storage" 18 76 8 "${choices[@]}" 3>&1 1>&2 2>&3) || exit 0
        printf -v "$variable" '%s' "$answer"
    else
        printf 'Available %s storage: ' "$content"
        printf '%s ' "${choices[@]}"; printf '\n'
        ask "$variable" "Storage for $content" "$fallback"
    fi
}
if $resume && $interactive; then fail 'Resume uses recorded settings; omit --interactive.'; fi
if ! $dry_run; then bsm_log_begin provision; trap 'bsm_finish $?' EXIT; fi
if $interactive; then
    [[ -t 0 ]] || fail '--interactive requires a terminal.'
    printf '\n  BSM Supervisor — Proxmox LXC Setup\n\n'
    mode=default
    if command -v whiptail >/dev/null; then
        mode=$(whiptail --title 'BSM Supervisor LXC' --menu 'Choose setup mode' 16 76 3 \
            default 'Default settings: 2 CPU, 2 GB RAM, 16 GB disk, DHCP' \
            advanced 'Customize resources, storage, and networking' \
            exit 'Exit without changes' 3>&1 1>&2 2>&3) || exit 0
    else
        echo '1) Default settings   2) Advanced settings   3) Exit'
        read -r -p 'Choose [1]: ' mode || exit 0
        case "$mode" in ''|1) mode=default ;; 2) mode=advanced ;; *) mode="exit" ;; esac
    fi
    [[ $mode != exit ]] || exit 0
    if ! $resume; then
    available_kib=$(pvesm status --storage "$storage" --content rootdir | awk 'NR>1 {print $6; exit}')
    [[ $available_kib =~ ^[0-9]+$ && $available_kib -ge $((disk * 1048576)) ]] || fail 'Selected storage has insufficient free space for the requested disk.'
fi
if [[ -z $ctid ]]; then ctid=$(pvesh get /cluster/nextid); fi
    if [[ $mode == advanced ]]; then
        ask ctid 'Container ID' "$ctid"
        ask hostname 'Container hostname' "$hostname"
        ask cores 'CPU cores' "$cores"
        ask memory 'RAM in MB' "$memory"
        ask swap 'Swap in MB' "$swap"
        ask disk 'Root disk size in GB' "$disk"
        ask bsm_host "BSM bind address" "$bsm_host"
        bsm_host_explicit=1
        ask bsm_env_file "BSM environment JSON file (optional)" "$bsm_env_file"
        ask bridge 'Network bridge' "$bridge"
        ask address 'IPv4: dhcp or address/prefix' "$address"
        if [[ $address != dhcp ]]; then ask gateway 'IPv4 gateway' "$gateway"; fi
        ask nameserver 'DNS server (blank inherits host)' "$nameserver"
        ask vlan 'VLAN tag (blank for untagged)' "$vlan"
        ask onboot 'Start container on host boot? 0=no, 1=yes' "$onboot"
        ask ref 'Supervisor branch or full commit' "$ref"
    fi
    # Storage names differ across hosts, even when using default resources.
    choose_storage storage rootdir "$storage"
    if [[ -z $template ]]; then choose_storage template_storage vztmpl "$template_storage"; fi
fi
[[ $bsm_host == 0.0.0.0 || $bsm_host == 127.0.0.1 || $bsm_host == :: || $bsm_host == ::1 ]] || fail 'Invalid BSM bind address.'
# One JSON payload supports all native entries, including values with spaces.
# Never put database URLs or a NUL-separated list into process arguments.
prepare_environment() {
    python3 - "$bsm_env_file" "$bsm_host" "$bsm_host_explicit" <<'PYENV'
import json, pathlib, re, sys
filename, host, explicit = sys.argv[1:]
values = {}
if filename:
    path = pathlib.Path(filename)
    if path.stat().st_size > 65536:
        raise SystemExit('BSM environment JSON is too large')
    values = json.loads(path.read_text())
allowed = {'BSM_CONFIG_DIR', 'BSM_DATA_DIR', 'BSM_DB_URL', 'BSM_LOG_LEVEL',
           'HOST', 'PORT', 'BSM_SUPERVISOR_WEB_HOST', 'BSM_SUPERVISOR_WEB_PORT'}
if not isinstance(values, dict) or values.keys() - allowed:
    raise SystemExit('Unsupported BSM environment variable')
if any(not isinstance(v, str) or not v or len(v) > 16384 or
       any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values.values()):
    raise SystemExit('BSM environment values must be nonempty single-line strings')
if explicit == '1' or 'HOST' not in values:
    values['HOST'] = host
for key in ('HOST', 'BSM_SUPERVISOR_WEB_HOST'):
    if key in values and values[key] not in ('0.0.0.0', '127.0.0.1', '::', '::1'):
        raise SystemExit('Invalid BSM listener host')
values.setdefault('PORT', '11325')
for key in ('PORT', 'BSM_SUPERVISOR_WEB_PORT'):
    if key in values and (not values[key].isascii() or not values[key].isdecimal()
                          or not 1 <= int(values[key]) <= 65535):
        raise SystemExit('Invalid BSM listener port')
values.setdefault('BSM_DATA_DIR', '/srv/bsm')
values.setdefault('BSM_CONFIG_DIR', values['BSM_DATA_DIR'].rstrip('/') + '/config')
for key in ('BSM_DATA_DIR', 'BSM_CONFIG_DIR'):
    path = pathlib.PurePosixPath(values[key])
    if not path.is_absolute() or path == pathlib.PurePosixPath('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]+', values[key]):
        raise SystemExit('BSM directories must be absolute non-root paths')
values.setdefault('BSM_DB_URL', 'sqlite:///' + values['BSM_CONFIG_DIR'].rstrip('/') + '/bedrock-server-manager.db')
level = values.get('BSM_LOG_LEVEL', 'INFO').upper()
level = {'WARN': 'WARNING', 'FATAL': 'CRITICAL'}.get(level, level)
if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'NOTSET'):
    raise SystemExit('Invalid BSM log level')
values['BSM_LOG_LEVEL'] = level
print(json.dumps(values))
PYENV
}
prepare_environment >/dev/null
public_bind=$(prepare_environment | python3 -c 'import json,sys; v=json.load(sys.stdin); print(v.get("BSM_SUPERVISOR_WEB_HOST", v["HOST"]), v.get("BSM_SUPERVISOR_WEB_PORT", v["PORT"]))')
read -r effective_host effective_port <<< "$public_bind"
[[ $onboot == 0 || $onboot == 1 ]] || fail 'onboot must be 0 or 1.'
for value in "$hostname" "$storage" "$template_storage" "$bridge"; do
    [[ $value =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || fail 'Invalid hostname, storage, or bridge name.'
done
[[ $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && $ref != *..* ]] || fail 'Invalid source ref.'
python3 - "$address" "$gateway" "$nameserver" "$cores" "$memory" "$swap" "$disk" "$vlan" <<'PY'
import ipaddress, sys
address, gateway, dns, *numbers = sys.argv[1:]
if address == 'dhcp':
    if gateway:
        raise SystemExit('Do not specify a gateway with DHCP')
else:
    if '/' not in address or not gateway:
        raise SystemExit('Static IPv4 needs a CIDR prefix and --gateway')
    interface = ipaddress.IPv4Interface(address)
    if ipaddress.IPv4Address(gateway) not in interface.network:
        raise SystemExit('Gateway is outside the configured subnet')
if dns:
    ipaddress.ip_address(dns)
for value, minimum, maximum in zip(numbers[:4], (1, 512, 0, 4), (8192, 1048576, 1048576, 1048576)):
    if not value.isascii() or not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise SystemExit('Resource value outside supported range')
if numbers[4] and (not numbers[4].isascii() or not numbers[4].isdecimal() or not 1 <= int(numbers[4]) <= 4094):
    raise SystemExit('VLAN must be 1..4094')
PY
ip link show dev "$bridge" >/dev/null || fail 'Bridge does not exist on this node.'
pvesm status --storage "$storage" --content rootdir | awk 'NR > 1 && $3 == "active" {found=1} END {exit !found}' || fail 'Root disk storage is not active or does not support containers.'
if ! $resume; then
    available_kib=$(pvesm status --storage "$storage" --content rootdir | awk 'NR>1 {print $6; exit}')
    [[ $available_kib =~ ^[0-9]+$ && $available_kib -ge $((disk * 1048576)) ]] || fail 'Selected storage has insufficient free space for the requested disk.'
fi
if [[ -z $ctid ]]; then ctid=$(pvesh get /cluster/nextid); fi
[[ $ctid =~ ^[1-9][0-9]{2,8}$ ]] || fail 'Container ID must be 100..999999999.'
# Cluster-wide check includes QEMU guests and containers on other nodes.
pvesh get /cluster/resources --type vm --output-format json | python3 -c '
import json, sys
wanted=int(sys.argv[1])
resume=sys.argv[2] == "true"
guests = json.load(sys.stdin)
if resume and any(int(item["vmid"]) == wanted and item.get("type") != "lxc" for item in guests):
    raise SystemExit("Resume ID belongs to a different guest type")
if not resume and any(int(item["vmid"]) == wanted for item in guests):
    raise SystemExit("Container ID already belongs to an existing guest")
' "$ctid" "$resume"
if ! $resume; then
if [[ -n $template ]]; then
    [[ $template =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*:vztmpl/[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || fail 'Use a Proxmox storage:vztmpl/template volume.'
    template_path=$(pvesm path "$template")
    [[ -f $template_path ]] || fail 'Template volume is not a local readable file.'
else
    command -v pveam >/dev/null || fail 'pveam is required for automatic template download.'
    [[ $(uname -m) == x86_64 ]] || fail 'Automatic Debian amd64 template selection needs an x86_64 host.'
    if ! $dry_run; then pveam update; fi
    template_versions='12|13'
    template_name=$(pveam available --section system | awk -v versions="$template_versions" '$2 ~ ("^debian-(" versions ")-standard_.*_amd64\\.tar\\.(zst|gz|xz)$") {print $2}' | sort -V | tail -n 1)
    [[ -n $template_name ]] || fail 'No supported Debian template found; run pveam update or pass --template.'
    template="$template_storage:vztmpl/$template_name"
    pvesm status --storage "$template_storage" --content vztmpl | awk 'NR > 1 && $3 == "active" {found=1} END {exit !found}' || fail 'Template storage must be active with vztmpl support.'
fi
fi
network="name=eth0,type=veth,bridge=$bridge,ip=$address,ip6=manual,firewall=1"
[[ -z $gateway ]] || network+=",gw=$gateway"
[[ -z $vlan ]] || network+=",tag=$vlan"
create=(pct create "$ctid" "$template" --hostname "$hostname" --unprivileged 1 --cores "$cores" --memory "$memory" --swap "$swap" --rootfs "$storage:$disk" --net0 "$network" --onboot "$onboot" --env "HOST=$bsm_host")
[[ -z $nameserver ]] || create+=(--nameserver "$nameserver")
printf 'New LXC plan: ID=%s, hostname=%s, template=%s\n' "$ctid" "$hostname" "$template"
printf '%q ' "${create[@]}"; printf '\n'
printf 'Effective BSM listener after native environment setup: %s port %s\n' "$effective_host" "$effective_port"
if $interactive && ! $dry_run; then
    summary="Create unprivileged LXC $ctid ($hostname)?
CPU: $cores | RAM: $memory MB | Swap: $swap MB | Disk: $storage:$disk GB
Template: $template
Network: $bridge | IPv4: $address | VLAN: ${vlan:-untagged}
Supervisor ref: $ref | On boot: $onboot | BSM bind: $effective_host port $effective_port
BSM will remain stopped."
    if command -v whiptail >/dev/null; then
        whiptail --title 'BSM Supervisor LXC' --yesno "$summary" 17 80 || exit 0
    else
        printf '%s\n' "$summary"
        read -r -p 'Create this container? [y/N]: ' answer || exit 0
        [[ $answer == y || $answer == Y ]] || exit 0
    fi
fi
if [[ -n $save_profile ]] && ! $dry_run; then
    settings_payload | python3 -c '
import json, os, pathlib, sys, tempfile
path=pathlib.Path(sys.argv[1])
if path.is_symlink(): raise SystemExit("Profile must not be a symlink")
values=json.load(sys.stdin)
for key in ("address", "gateway"): values.pop(key, None)
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd, name=tempfile.mkstemp(dir=path.parent)
with os.fdopen(fd, "w") as stream: json.dump(values, stream, indent=2)
os.replace(name, path)
' "$save_profile"
fi
if $dry_run; then
    echo 'Dry run: no download, creation, or installation performed.'
    exit 0
fi
exec 9>/run/lock/bsm-supervisor-provision.lock
flock --nonblock 9 || fail 'Another supervisor provisioner is running.'
if [[ $ref != "$initial_ref" ]] && ! $package_version_explicit; then package_version=; fi
work=$(mktemp -d)
prepare_environment > "$work/environment.json"
settings_payload > "$work/settings.json"
cleanup() {
    result=$?
    bsm_finish "$result"
    [[ -z $work ]] || rm -rf -- "$work"
    if ((result != 0)) && $creation_attempted; then
        printf 'Provisioning failed for LXC %s. Any created container is retained; inspect with: pct enter %s\n' "$ctid" "$ctid" >&2
        echo 'Bootstrap files, if copied, are in /root/bsm-supervisor-bootstrap. No container was destroyed.' >&2
    fi
}
trap cleanup EXIT
if $resume; then
    bsm_stage 'Verify recorded deployment'
    phase=$(provision_record phase)
    guest_exists=true
    if pct config "$ctid" > "$work/guest-config"; then
        deployment_id=$(provision_record verify)
    else
        guest_exists=false
        deployment_id=$(provision_record verify-empty)
    fi
    cp -- "$state_root/$ctid/bootstrap.tar.gz" "$work/bootstrap.tar.gz"
else
    bsm_stage 'Acquire pinned deployment bundle'
if [[ -z $source_dir ]]; then
    command -v curl >/dev/null || fail 'Install curl on the host, or pass --source-dir.'
    token=${GH_TOKEN:-}
    : > "$work/auth"
    if [[ -n $token ]]; then
        [[ $token =~ ^[A-Za-z0-9_]+$ ]] || fail 'Invalid token format.'
        printf 'header = "Authorization: Bearer %s"\n' "$token" > "$work/auth"
    fi
    unset token GH_TOKEN

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
[[ -f $source_dir/scripts/install-lxc.sh && -d $source_dir/src/bsm_supervisor ]] || fail 'Invalid supervisor source checkout.'
# Preserve source provenance when the filtered bootstrap has no Git metadata.
python3 - "$source_dir" "$work/supervisor-source.json" "$ref" <<'PYPROVENANCE'
import json, pathlib, re, shutil, subprocess, sys
source, output, requested = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
match = re.search(r'-([0-9a-f]{40})$', source.name)
revision, dirty = match.group(1) if match else None, None
if not match and (source / '.git').exists() and shutil.which('git'):
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
output.write_text(json.dumps(record))
PYPROVENANCE
# Keep version metadata when the bootstrap deliberately omits the Git directory.
version_inputs=()
if [[ -f $source_dir/scripts/archive-version.py ]]; then
    python3 "$source_dir/scripts/archive-version.py" "$source_dir" "$work/.git_archival.txt"
    version_inputs=(.git_archival.txt)
fi
# Copy only installation inputs; credentials, venvs, Git metadata and local data stay on host.
tar --create --gzip --file "$work/bootstrap.tar.gz" --directory "$source_dir" \
    pyproject.toml README.md LICENSE config.example.toml src scripts \
    --directory "$work" supervisor-source.json "${version_inputs[@]}"
    settings_payload > "$work/settings.json"
    deployment_id=$(provision_record prepare)
    phase=prepared
fi
if $resume && [[ $phase == completed ]]; then
    echo "Deployment for LXC $ctid is already complete; nothing changed."
    exit 0
fi
bsm_stage 'Preparing LXC template'
if ! $resume && [[ -n ${template_name:-} ]]; then pveam download "$template_storage" "$template_name"; fi
bsm_stage 'Creating or verifying owned LXC'
creation_attempted=true
if ! $resume || ! $guest_exists; then
    [[ -f $(pvesm path "$template") ]] || fail 'The recorded template is unavailable. Restore it before resuming creation.'
    create+=(--description "BSM Supervisor deployment $deployment_id")
    available_kib=$(pvesm status --storage "$storage" --content rootdir | awk 'NR>1 {print $6; exit}')
    [[ $available_kib =~ ^[0-9]+$ && $available_kib -ge $((disk * 1048576)) ]] || fail 'Selected storage has insufficient free space for the requested disk.'
    "${create[@]}"
    pct config "$ctid" > "$work/guest-config"
    provision_record created >/dev/null
fi
if [[ $phase == prepared || $phase == created ]]; then
# Invoke Proxmox's native CLI handler in-process: POSIX argv cannot carry NULs.
perl -MJSON::PP -MPVE::CLI::pct -e '
    my ($id, $path) = @ARGV;
    open(my $file, "<", $path) or die "Cannot open environment payload";
    local $/; my $values = decode_json(<$file>);
    @ARGV = ("set", $id, "--env", join("\0", map { "$_=$values->{$_}" } sort keys %$values));
    PVE::CLI::pct->run_cli_handler();
' "$ctid" "$work/environment.json"
pct config "$ctid" > "$work/guest-config"
provision_record environment >/dev/null
fi
bsm_stage 'Starting container and waiting for network'
[[ $(pct status "$ctid") == *running* ]] || pct start "$ctid"
ready=false
for ((attempt=0; attempt<60; attempt++)); do
    if pct exec "$ctid" -- test -d /run/systemd/system && \
        pct exec "$ctid" -- getent hosts pypi.org >/dev/null; then ready=true; break; fi
    sleep 2
done
if ! $ready; then
    pct exec "$ctid" -- ip -brief address || true
    pct exec "$ctid" -- ip -4 route || true
    pct exec "$ctid" -- cat /etc/resolv.conf || true
    fail 'Container readiness failed. Inspect systemd, IP, routing and DNS output above; resume with --resume after correcting it.'
fi
bsm_stage 'Install supervisor in owned container'
if [[ $phase != installed ]]; then
pct exec "$ctid" -- mkdir -p /root/bsm-supervisor-bootstrap
pct push "$ctid" "$work/bootstrap.tar.gz" /root/bsm-supervisor-bootstrap/source.tar.gz --perms 0600
pct exec "$ctid" -- tar --extract --gzip --file /root/bsm-supervisor-bootstrap/source.tar.gz \
    --directory /root/bsm-supervisor-bootstrap --no-same-owner --no-same-permissions
echo "[4/4] Installing supervisor and managed BSM unit"
guest_installer=install-lxc.sh
if tar -tzf "$work/bootstrap.tar.gz" | grep -qx 'scripts/install.sh'; then guest_installer=install.sh; fi
flags=(--install-bsm-unit)
if $resume && [[ $(pct exec "$ctid" -- systemctl show bedrock-server-manager-webui.service --property=Description --value) == 'Bedrock Server Manager (BSM Supervisor)' ]]; then flags=(); fi
pct exec "$ctid" -- env BSM_SUPERVISOR_INSTALL_METHOD=proxmox-lxc bash "/root/bsm-supervisor-bootstrap/scripts/$guest_installer" \
    --source-dir /root/bsm-supervisor-bootstrap --ref "$ref" --package-version "$package_version" "${flags[@]}"
pct config "$ctid" > "$work/guest-config"
provision_record installed >/dev/null
fi
pct exec "$ctid" -- rm -rf /root/bsm-supervisor-bootstrap
pct config "$ctid" > "$work/guest-config"
provision_record completed >/dev/null
bsm_stage 'Deployment complete'
printf '\nLXC %s is ready. Enter it with: pct enter %s\n' "$ctid" "$ctid"
pct exec "$ctid" -- hostname -I || true
printf 'Deployment journal: %s/%s/plan.json\n' "$state_root" "$ctid"
echo 'Supervisor is installed. Inspect current service state inside the container.'
echo 'Inside the container, run: bsm-supervisor setup --offline-confirmed'
echo 'See docs/install-lxc.md for version selection and BSM user setup.'
