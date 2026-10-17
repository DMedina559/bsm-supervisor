# Update the supervisor in an existing LXC

Run the following **inside the existing container as root**. If starting from the
Proxmox host, use `pct enter` followed by the container ID first.

## Update from the CLI

The CLI update requires an existing supervisor-managed BSM unit.
On versions that provide the update command, run:

```sh
bsm-supervisor supervisor-update --maintenance-confirmed
bsm-supervisor version
bsm-supervisor doctor
```

The command resolves `dev` to an exact commit, downloads the installer from that
same revision, and stages a permanent supervisor environment while BSM remains
available. It then gracefully stops a running BSM, refreshes the managed unit and
private environment with the new interpreter, and restarts BSM only if it was
running before the update. The BSM package and selected installation are preserved.

Use `--ref FULL_COMMIT_SHA` to choose a fixed revision and `--wait` to require
application readiness after restart. A private-repository token is read from
`GH_TOKEN` or a hidden terminal prompt and is never passed as a command argument
or written to the update plan. It needs repository **Contents: read-only** access.
For a local checkout, use `--source-dir /absolute/path/to/bsm-supervisor`.

Progress is saved privately in `state_dir/supervisor-update.json`. If an update is
interrupted, inspect `history`, `doctor`, and service state, resolve any uncertain
service operation explicitly, then run:

```sh
bsm-supervisor supervisor-update --resume --maintenance-confirmed
```

Resume retains the recorded revision and original running/stopped intent. It
retries installation if staging did not finish, or uses the installed environment
when only refresh/start remains. No data restoration or recovery acknowledgement
is automatic. Earlier environments remain available because old units may still
reference their launch guards. A failed post-install refresh can leave the CLI
updated while the previous unit remains installed; use diagnostics before resume.

## Bootstrap or update an older supervisor

If `supervisor-update` is unavailable, use the standalone installer below. It
creates a permanent environment and preserves configuration, BSM selection, data,
and the current service unit. Omit `--install-bsm-unit` for an existing unit.
Download fresh source rather than rerunning a stale bootstrap checkout.

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
