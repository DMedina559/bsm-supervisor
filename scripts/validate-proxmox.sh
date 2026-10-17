#!/usr/bin/env bash
# Exercise the complete supervisor lifecycle in a newly created disposable LXC.
set -euo pipefail
umask 077
ctid='' bsm_commit='' source_dir='' storage=local-lvm template_storage=local
confirmed=false dry_run=false
usage() {
    echo 'Usage: validate-proxmox.sh --ctid UNUSED_ID --bsm-commit FULL_SHA --source-dir CHECKOUT [--storage POOL] [--template-storage POOL] [--disposable-confirmed] [--dry-run]'
    echo 'Creates and retains a disposable LXC. Requires a BSM commit with local health authentication.'
}
while (($#)); do
    case "$1" in
        --ctid|--bsm-commit|--source-dir|--storage|--template-storage)
            (($# >= 2)) || { usage >&2; exit 2; }
            case "$1" in
                --ctid) ctid=$2 ;; --bsm-commit) bsm_commit=$2 ;;
                --source-dir) source_dir=$2 ;; --storage) storage=$2 ;;
                --template-storage) template_storage=$2 ;;
            esac
            shift 2 ;;
        --disposable-confirmed) confirmed=true; shift ;;
        --dry-run) dry_run=true; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ $ctid =~ ^[1-9][0-9]{2,8}$ && $bsm_commit =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 2; }
[[ -f $source_dir/scripts/provision-proxmox.sh && -f $source_dir/pyproject.toml ]] || { echo 'A supervisor checkout is required.' >&2; exit 2; }
[[ $storage =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && $template_storage =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || exit 2
if $dry_run; then
    printf 'Validation plan: create CT %s; install BSM commit %s; setup/readiness; checkpoint/restore/encrypted transfer; independent console; schedules; failed startup/recovery; supervisor update; reboot/readiness.\n' "$ctid" "$bsm_commit"
    echo 'No host or container changes made.'
    exit 0
fi
$confirmed || { echo 'Pass --disposable-confirmed to authorize the test container lifecycle.' >&2; exit 2; }
[[ $EUID == 0 ]] || { echo 'Run on the Proxmox host as root.' >&2; exit 1; }
command -v pct >/dev/null || { echo 'Proxmox pct is required.' >&2; exit 1; }
# Existing guests are rejected by the provisioner, including cluster-wide VM IDs.
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
pct_call() { (umask 022; pct "$@"); }
bash "$source_dir/scripts/provision-proxmox.sh" --ctid "$ctid" \
    --source-dir "$source_dir" --storage "$storage" --template-storage "$template_storage"
# Copy only package inputs; no checkout credentials, local data or virtualenvs.
tar -C "$source_dir" -czf "$work/source.tar.gz" \
    src scripts pyproject.toml README.md LICENSE config.example.toml
pct_call push "$ctid" "$work/source.tar.gz" /root/bsm-validation-source.tar.gz
pct_call exec "$ctid" -- mkdir -p /root/bsm-validation-source
pct_call exec "$ctid" -- tar -xzf /root/bsm-validation-source.tar.gz -C /root/bsm-validation-source
pct_call exec "$ctid" -- /bin/bash -s -- "$bsm_commit" <<'GUEST'
set -euo pipefail
commit=$1
bsm-supervisor install-git --commit "$commit" --trust-source-build
bsm-supervisor setup --installation "git-$commit" --offline-confirmed --wait
bsm-supervisor setup --wait
bsm-supervisor doctor
bsm-supervisor web-token /etc/bsm-supervisor/validation-console-token
bsm-supervisor web-unit --token-file /etc/bsm-supervisor/validation-console-token > /etc/systemd/system/bsm-supervisor-console.service
systemd-analyze verify --man=no /etc/systemd/system/bsm-supervisor-console.service
systemctl daemon-reload
systemctl enable --now bsm-supervisor-console.service
# urllib keeps the credential out of command arguments and validation logs.
console_ready() {
    /opt/bsm-supervisor/current/bin/python -I - <<'PYCONSOLE'
import json, urllib.request
from pathlib import Path
request = urllib.request.Request('http://127.0.0.1:11326/api/state', headers={
    'Authorization': 'Bearer ' + Path('/etc/bsm-supervisor/validation-console-token').read_text().strip()
})
with urllib.request.urlopen(request, timeout=5) as response:
    assert 'service' in json.load(response)
PYCONSOLE
}
for ((attempt=0; attempt<30; attempt++)); do
    if console_ready >/dev/null 2>&1; then break; fi
    sleep 1
done
console_ready
printf 'before\n' > /srv/bsm/validation-marker.txt
bsm-supervisor graceful-stop
checkpoint=$(bsm-supervisor checkpoint-create --offline-confirmed | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
bsm-supervisor checkpoint-verify "$checkpoint"
# Prove that BSM downtime does not take down the independent control surface.
console_ready
bsm-supervisor web-token /root/validation-backup-passphrase
bsm-supervisor checkpoint-export "$checkpoint" /root/validation.bsmcheckpoint.enc --passphrase-file /root/validation-backup-passphrase
/opt/bsm-supervisor/current/bin/python -I - <<'PYTRANSFER'
from dataclasses import replace
from pathlib import Path
from bsm_supervisor.config import Config
from bsm_supervisor.store import Store
from bsm_supervisor.transfer import import_checkpoint
config = replace(Config.load(Path('/etc/bsm-supervisor/config.toml')), state_dir=Path('/var/lib/bsm-transfer-validation'))
result = import_checkpoint(config, Store(config.state_dir), Path('/root/validation.bsmcheckpoint.enc'), Path('/root/validation-backup-passphrase'))
assert result['restored'] is False
PYTRANSFER
bsm-supervisor start --wait
printf 'after\n' > /srv/bsm/validation-marker.txt
bsm-supervisor graceful-stop
restored=$(bsm-supervisor checkpoint-restore "$checkpoint" --offline-confirmed --restore-data-confirmed | python3 -c 'import json,sys; print(json.load(sys.stdin)["operation_id"])')
[[ $(cat /srv/bsm/validation-marker.txt) == before ]]
bsm-supervisor restore-cleanup "$restored" --offline-confirmed --discard-originals
python3 - <<'PYTIMEOUT'
from pathlib import Path
path = Path('/etc/bsm-supervisor/config.toml')
value = path.read_text().replace('readiness_timeout = 60', 'readiness_timeout = 10')
path.write_text(value)
PYTIMEOUT
# Deliberately remove only this disposable installation's Python symlink.
python=/opt/bsm/installations/current/bin/python
mv "$python" "$python.validation-saved"
trap 'mv -f "$python.validation-saved" "$python" 2>/dev/null || true' EXIT
if bsm-supervisor start --wait; then
    echo 'Expected the disposable runtime startup to fail.' >&2
    exit 1
fi
systemctl stop bedrock-server-manager-webui.service
mv "$python.validation-saved" "$python"
trap - EXIT
systemctl reset-failed bedrock-server-manager-webui.service
sed -i 's/readiness_timeout = 10/readiness_timeout = 60/' /etc/bsm-supervisor/config.toml
operation=$(bsm-supervisor history | python3 -c 'import json,sys; print(next(item["id"] for item in json.load(sys.stdin) if item["state"] == "running"))')
bsm-supervisor acknowledge "$operation" --service-checked
bsm-supervisor start --wait
previous=$(readlink -f /opt/bsm-supervisor/current)
bsm-supervisor supervisor-update --source-dir /root/bsm-validation-source --maintenance-confirmed --wait
[[ $(readlink -f /opt/bsm-supervisor/current) != "$previous" ]]
[[ $(bsm-supervisor selection | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])') == "git-$commit" ]]
[[ $(cat /srv/bsm/validation-marker.txt) == before ]]
bsm-supervisor checkpoint-verify "$checkpoint"
bsm-supervisor doctor
systemctl restart bsm-supervisor-console.service
for ((attempt=0; attempt<30; attempt++)); do
    if console_ready >/dev/null 2>&1; then break; fi
    sleep 1
done
console_ready
/opt/bsm-supervisor/current/bin/python -I - <<'PYPOLICY'
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
now = datetime.now(timezone.utc)
policy = {'name':'validation-check', 'action':'check-updates', 'calendar':'*-*-* 02:00:00 UTC', 'selector':'prerelease', 'window_start':(now-timedelta(minutes=5)).strftime('%H:%M'), 'window_end':(now+timedelta(minutes=5)).strftime('%H:%M'), 'days':[1,2,3,4,5,6,7]}
Path('/root/validation-schedule.json').write_text(json.dumps(policy))
PYPOLICY
bsm-supervisor schedule-install /root/validation-schedule.json
bsm-supervisor schedule-run validation-check
bsm-supervisor schedule-remove validation-check --service-checked
bsm-supervisor events --after 0
systemctl enable bedrock-server-manager-webui.service
GUEST
pct_call reboot "$ctid"
ready=false
for ((attempt=0; attempt<90; attempt++)); do
    if pct_call exec "$ctid" -- bsm-supervisor health >/dev/null 2>&1; then ready=true; break; fi
    sleep 2
done
$ready || { echo "CT $ctid did not recover readiness after reboot; inspect it." >&2; exit 1; }
pct_call exec "$ctid" -- bsm-supervisor doctor
pct_call exec "$ctid" -- systemctl is-active --quiet bsm-supervisor-console.service
printf 'Validation passed for CT %s and BSM %s. Container retained for inspection.\n' "$ctid" "$bsm_commit"
