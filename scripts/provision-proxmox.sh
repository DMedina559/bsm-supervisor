#!/usr/bin/env bash
# Run on a Proxmox VE host; create a new unprivileged LXC and bootstrap supervisor.
set -euo pipefail
umask 077
ctid=
hostname=bsm-supervisor
bsm_host=0.0.0.0
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
ref=dev
source_dir=
dry_run=false
creation_attempted=false
interactive=false
if (($# == 0)) && [[ -t 0 ]]; then interactive=true; fi
work=
usage() {
    cat <<'HELP'
Usage: provision-proxmox.sh [OPTIONS]
  --ctid ID                  Default: next free cluster ID
  --hostname NAME            Default: bsm-supervisor
  --storage NAME             Root disk storage (default: local-lvm)
  --template VOLUME           Existing Debian 12+/Ubuntu 24.04+ template volume
  --template-storage NAME    Download storage (default: local)
                             Without --template, download latest Debian 13/12 amd64
  --bsm-host ADDRESS         0.0.0.0 (default), 127.0.0.1, ::, or ::1
  --bridge NAME              Default: vmbr0
  --ip dhcp|IPV4/CIDR         Default: dhcp
  --gateway IPV4             Required with static --ip
  --nameserver IP            Optional DNS server; otherwise inherit host DNS
  --vlan ID                  Optional VLAN tag
  --cores N --memory MB --swap MB --disk GB
                             Defaults: 2 CPUs, 2048 MB RAM, 512 MB swap, 16 GB disk
  --onboot                   Start container when host boots
  --ref BRANCH_OR_SHA         Supervisor revision (default: dev)
  --source-dir CHECKOUT       Use local source instead of private GitHub download
  --interactive              Guided Default/Advanced setup wizard
  --dry-run                  Read-only preflight and command preview
  --help
The container is always unprivileged. Existing guests are never reused/deleted.
HELP
}
fail() { echo "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --interactive) interactive=true; shift ;;
        --dry-run) dry_run=true; shift ;;
        --onboot) onboot=1; shift ;;
        --help|-h) usage; exit 0 ;;
        --ctid|--bsm-host|--hostname|--storage|--template-storage|--template|--bridge|--ip|--gateway|--nameserver|--vlan|--cores|--memory|--swap|--disk|--ref|--source-dir)
            (($# >= 2)) || fail "Missing value for $1"
            option=${1#--}; option=${option//-/_}
            [[ $option != ip ]] || option=address
            printf -v "$option" '%s' "$2"
            shift 2 ;;
        *) usage >&2; fail "Unknown option: $1" ;;
    esac
done
[[ $EUID == 0 ]] || $dry_run || fail 'Run as root on the Proxmox host.'
for command in pct pvesh pvesm pveversion python3 ip tar flock; do
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
    if [[ -z $ctid ]]; then ctid=$(pvesh get /cluster/nextid); fi
    if [[ $mode == advanced ]]; then
        ask ctid 'Container ID' "$ctid"
        ask hostname 'Container hostname' "$hostname"
        ask cores 'CPU cores' "$cores"
        ask memory 'RAM in MB' "$memory"
        ask swap 'Swap in MB' "$swap"
        ask disk 'Root disk size in GB' "$disk"
        ask bsm_host "BSM bind address" "$bsm_host"
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
    ipaddress.IPv4Interface(address)
    ipaddress.IPv4Address(gateway)
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
if [[ -z $ctid ]]; then ctid=$(pvesh get /cluster/nextid); fi
[[ $ctid =~ ^[1-9][0-9]{2,8}$ ]] || fail 'Container ID must be 100..999999999.'
# Cluster-wide check includes QEMU guests and containers on other nodes.
pvesh get /cluster/resources --type vm --output-format json | python3 -c '
import json, sys
wanted=int(sys.argv[1])
if any(int(item["vmid"]) == wanted for item in json.load(sys.stdin)):
    raise SystemExit("Container ID already belongs to an existing guest")
' "$ctid"
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
network="name=eth0,type=veth,bridge=$bridge,ip=$address,ip6=manual,firewall=1"
[[ -z $gateway ]] || network+=",gw=$gateway"
[[ -z $vlan ]] || network+=",tag=$vlan"
create=(pct create "$ctid" "$template" --hostname "$hostname" --unprivileged 1 --cores "$cores" --memory "$memory" --swap "$swap" --rootfs "$storage:$disk" --net0 "$network" --onboot "$onboot" --env "BSM_SUPERVISOR_WEB_HOST=$bsm_host")
[[ -z $nameserver ]] || create+=(--nameserver "$nameserver")
printf 'New LXC plan: ID=%s, hostname=%s, template=%s\n' "$ctid" "$hostname" "$template"
printf '%q ' "${create[@]}"; printf '\n'
if $interactive && ! $dry_run; then
    summary="Create unprivileged LXC $ctid ($hostname)?
CPU: $cores | RAM: $memory MB | Swap: $swap MB | Disk: $storage:$disk GB
Template: $template
Network: $bridge | IPv4: $address | VLAN: ${vlan:-untagged}
Supervisor ref: $ref | On boot: $onboot | BSM bind: $bsm_host
BSM will remain stopped."
    if command -v whiptail >/dev/null; then
        whiptail --title 'BSM Supervisor LXC' --yesno "$summary" 17 80 || exit 0
    else
        printf '%s\n' "$summary"
        read -r -p 'Create this container? [y/N]: ' answer || exit 0
        [[ $answer == y || $answer == Y ]] || exit 0
    fi
fi
if $dry_run; then
    echo 'Dry run: no download, creation, or installation performed.'
    exit 0
fi
exec 9>/run/lock/bsm-supervisor-provision.lock
flock --nonblock 9 || fail 'Another supervisor provisioner is running.'
work=$(mktemp -d)
cleanup() {
    result=$?
    [[ -z $work ]] || rm -rf -- "$work"
    if ((result != 0)) && $creation_attempted; then
        printf 'Provisioning failed for LXC %s. Any created container is retained; inspect with: pct enter %s\n' "$ctid" "$ctid" >&2
        echo 'Bootstrap files, if copied, are in /root/bsm-supervisor-bootstrap. No container was destroyed.' >&2
    fi
}
trap cleanup EXIT
if [[ -z $source_dir ]]; then
    command -v curl >/dev/null || fail 'Install curl on the host, or pass --source-dir.'
    token=${GH_TOKEN:-}
    if [[ -z $token && -t 0 ]]; then read -r -s -p 'GitHub token: ' token; echo; fi
    : > "$work/auth"
    if [[ -n $token ]]; then
        [[ $token =~ ^[A-Za-z0-9_]+$ ]] || fail 'Invalid token format.'
        printf 'header = "Authorization: Bearer %s"\n' "$token" > "$work/auth"
    fi
    unset token GH_TOKEN
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
if len(roots) != 1:
    raise SystemExit('Invalid source checkout')
(work / 'source-path').write_text(str(roots[0]))
PY
    source_dir=$(cat "$work/source-path")
fi
unset GH_TOKEN
[[ -f $source_dir/scripts/install-lxc.sh && -d $source_dir/src/bsm_supervisor ]] || fail 'Invalid supervisor source checkout.'
# Copy only installation inputs; credentials, venvs, Git metadata and local data stay on host.
tar --create --gzip --file "$work/bootstrap.tar.gz" --directory "$source_dir" \
    pyproject.toml README.md LICENSE config.example.toml src scripts
echo "[1/4] Preparing LXC template"
if [[ -n ${template_name:-} ]]; then pveam download "$template_storage" "$template_name"; fi
echo "[2/4] Creating unprivileged LXC $ctid"
creation_attempted=true
"${create[@]}"
echo "[3/4] Starting container and waiting for network"
pct start "$ctid"
ready=false
for ((attempt=0; attempt<60; attempt++)); do
    if pct exec "$ctid" -- test -d /run/systemd/system && \
        pct exec "$ctid" -- getent hosts pypi.org >/dev/null; then ready=true; break; fi
    sleep 2
done
$ready || fail 'Container did not become ready with DNS within 120 seconds.'
pct exec "$ctid" -- mkdir -p /root/bsm-supervisor-bootstrap
pct push "$ctid" "$work/bootstrap.tar.gz" /root/bsm-supervisor-bootstrap/source.tar.gz --perms 0600
pct exec "$ctid" -- tar --extract --gzip --file /root/bsm-supervisor-bootstrap/source.tar.gz \
    --directory /root/bsm-supervisor-bootstrap --no-same-owner --no-same-permissions
echo "[4/4] Installing supervisor and managed BSM unit"
pct exec "$ctid" -- bash /root/bsm-supervisor-bootstrap/scripts/install-lxc.sh \
    --source-dir /root/bsm-supervisor-bootstrap --install-bsm-unit
pct exec "$ctid" -- rm -rf /root/bsm-supervisor-bootstrap
printf '\nLXC %s is ready. Enter it with: pct enter %s\n' "$ctid" "$ctid"
echo 'Supervisor is installed; BSM has not been installed or started.'
echo 'Continue with docs/install-lxc.md for BSM selection, first startup, and readiness token setup.'
