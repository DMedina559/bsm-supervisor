# Update the supervisor in an existing LXC

Run the following **inside the existing container as root**. If starting from the
Proxmox host, enter it with `pct enter 101` first. Use your container ID.

The latest installer creates a new permanent supervisor environment, checks the
package/configuration, and switches the command launcher. It preserves existing
configuration, the BSM environment selection, data, and the current service unit.
Omit `--install-bsm-unit` because the unit already exists. Download fresh source;
the retained `/root/bsm-supervisor-bootstrap` directory contains the older code.

```bash
read -r -s -p 'GitHub token: ' GH_TOKEN
printf '\n'
export GH_TOKEN
installer=$(mktemp /tmp/bsm-supervisor-update-XXXXXXXX.sh)
if [[ $GH_TOKEN =~ ^[A-Za-z0-9_]+$ ]] && \
    printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw+json"\n' "$GH_TOKEN" | \
    curl --config - --fail --silent --show-error --proto '=https' \
    'https://api.github.com/repos/DMedina559/bsm-supervisor/contents/scripts/install-lxc.sh?ref=dev' \
    -o "$installer"; then
    bash "$installer" --ref dev
fi
rm -f "$installer"
unset GH_TOKEN
```

Only continue if the installer reports success. The same read-only repository
token used for initial installation is sufficient. Use a full commit instead of
`dev` in both references to select an immutable revision. Updating the supervisor
does not update the installed BSM package.

## Migrate the managed unit

This release adds a private, pinned BSM environment file. After updating the
supervisor, generate it and the current unit while BSM is stopped. For container
101's LAN listener, add these keys once to `/etc/bsm-supervisor/config.toml` if they
are absent, or edit their existing values:

```toml
bsm_host = "0.0.0.0"
bsm_port = 11325
bsm_log_level = "INFO"
```

Alternatively use the corresponding Proxmox environment entries described in
[BSM environment settings](listener-environment.md). Preserve the existing data,
config and database paths. Do not change those paths merely to update the software.

```sh
bsm-supervisor check
bsm-supervisor graceful-stop
bsm-supervisor service-environment --offline-confirmed
bsm-supervisor unit --user bsm > /tmp/bedrock-server-manager-webui.service
systemd-analyze verify --man=no /tmp/bedrock-server-manager-webui.service
```

After verification succeeds:

```sh
install -m 0644 /tmp/bedrock-server-manager-webui.service /etc/systemd/system/bedrock-server-manager-webui.service
systemctl daemon-reload
bsm-supervisor start
bsm-supervisor status
ss -ltnp
```

This briefly stops BSM. If graceful shutdown fails, inspect its journal rather
than continuing to change its environment or unit. Generated launch guards point
to a permanent supervisor interpreter; older environments are retained for that
reason. After unit regeneration, it references the newly installed interpreter.
The installer does not perform application migrations or discard checkpoints.
