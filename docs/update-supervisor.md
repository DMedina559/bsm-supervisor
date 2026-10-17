# Update the supervisor in an existing LXC

Run the following **inside the existing container as root**. If starting from the
Proxmox host, use `pct enter` followed by the container ID first.

The latest installer creates a new permanent supervisor environment, checks the
package/configuration, and switches the command launcher. It preserves existing
configuration, the BSM environment selection, data, and the current service unit.
Omit `--install-bsm-unit` because the unit already exists. Download fresh source;
a retained bootstrap checkout may contain an earlier revision.

```bash
REVISION=dev # Or a full commit SHA for a fixed revision.
read -r -s -p 'GitHub token: ' GH_TOKEN
printf '\n'
export GH_TOKEN
installer=$(mktemp /tmp/bsm-supervisor-update-XXXXXXXX.sh)
if [[ $GH_TOKEN =~ ^[A-Za-z0-9_]+$ ]] && \
    printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw+json"\n' "$GH_TOKEN" | \
    curl --config - --fail --silent --show-error --proto '=https' \
    "https://api.github.com/repos/DMedina559/bsm-supervisor/contents/scripts/install-lxc.sh?ref=$REVISION" \
    -o "$installer"; then
    bash "$installer" --ref "$REVISION"
fi
rm -f "$installer"
unset GH_TOKEN REVISION
```

Only continue if the installer reports success. The same read-only repository
token used for initial installation is sufficient. Set `REVISION` to a full commit SHA to select an immutable revision. Updating the supervisor
does not update the installed BSM package.

## Refresh the managed unit

After updating, refresh the private BSM environment file and generated unit while
BSM is stopped. This keeps the launch guard on the installed supervisor version.
Preserve existing listener, data, configuration, and database settings; a software
update does not require changing them. To change those settings, follow
[environment configuration](listener-environment.md).

The commands below use the default unit name and `bsm` service account. If BSM is
already stopped, skip `graceful-stop`. If graceful shutdown fails, inspect the
service journal and resolve the failure before continuing.

Run each command only after the previous command succeeds:

```sh
bsm-supervisor check
bsm-supervisor graceful-stop
bsm-supervisor unit-refresh --offline-confirmed
bsm-supervisor selection
bsm-supervisor start
bsm-supervisor status
```

`unit-refresh` verifies the candidate, saves the previous unit and private
environment under `state_dir/service-backups/`, installs the current generated
files, and reloads systemd. It accepts a fully stopped failed unit and clears its
failure state. It never starts BSM or acknowledges pending operations. An
interrupted refresh retains its journal entry and backup location for inspection.
Use `--user ACCOUNT` if your service account differs from `bsm`.

If `selection` prints `null`, inspect `installations` and activate the intended
ID with `activate INSTALLATION_ID --offline-confirmed` before starting. A software
update does not choose a different BSM version automatically.

This briefly stops BSM. If graceful shutdown fails, inspect its journal rather
than continuing to change its environment or unit. Generated launch guards point
to a permanent supervisor interpreter; older environments are retained for that
reason. After unit regeneration, it references the newly installed interpreter.
The installer does not perform application migrations or discard checkpoints.
