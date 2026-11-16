# Setup and diagnostics

Run administration commands as root inside the container. These examples use the
installer's default configuration. For another file, put
`--config /absolute/path/config.toml` before the command.

## Guided setup

After installing the supervisor, run:

```sh
bsm-supervisor setup
```

On an interactive terminal, setup asks for offline confirmation and offers
registered installations and release channels. Without a terminal, supply the
confirmation and optionally a channel, exact version, or registered ID:

```sh
bsm-supervisor setup prerelease --offline-confirmed
bsm-supervisor setup --installation INSTALLATION_ID --offline-confirmed
```

Choose one command. With no version argument, setup preserves a selection, reuses
a single registered installation, or prepares the prerelease channel on a fresh
installation. Multiple registered environments require an explicit choice in
noninteractive use. Guided release installation requires BSM 4.0 or newer.

Setup creates a missing service account and data/configuration directories,
verifies and installs the managed unit and private environment, selects the
installation, and starts BSM. Existing directory ownership is preserved. Use
`--user ACCOUNT` for a different service account or `--no-start` to prepare the
installation without starting it.

The result includes listener URLs. Complete BSM's first-user setup in the browser
if prompted. No user token is needed for initial startup. Automatic local health
authentication requires a BSM version that supports it; older versions need the
bearer-token configuration described in [coordinated upgrades](upgrades.md).

Rerun `setup` without a selector to check an existing running deployment; it does
not restart it or choose a newer version. Use `setup --wait` to also require
application readiness. Setup refuses to infer the version of existing,
unselected BSM data. Register and select the matching environment explicitly
before adopting that data. Use `upgrade` for version changes against existing data.


Setup starts BSM without enabling boot autostart. To opt in with the default unit,
run `systemctl enable bedrock-server-manager-webui.service`.

Offline confirmation covers unmanaged BSM, Minecraft, and other writers using the
same paths. Setup never clears pending recovery operations or recovery markers.

## Readiness-aware startup

```sh
bsm-supervisor start --wait
bsm-supervisor restart --wait
```

These commands return success only after authenticated health, version, and
runtime identity checks pass within `readiness_timeout`. Without `--wait`, they
check that systemd reports a running process. `restart` follows the unit's normal
shutdown behavior; use `graceful-stop` followed by `start --wait` for planned
application maintenance.

A failed start or readiness check retains an unresolved service operation for
inspection. Check `status`, `history`, and the service journal before deciding
whether acknowledgement is appropriate. A readiness failure does not establish
that BSM is stopped, and the supervisor does not automatically restore data.

## Diagnose a deployment

```sh
bsm-supervisor doctor
bsm-supervisor doctor --no-health
```

Doctor returns a JSON report and exits with status 1 when a check fails. It checks
selection, pending recovery, managed-unit/environment consistency, service state,
service-account permissions, available state-filesystem space, checkpoint
coverage, and authenticated readiness when BSM is running. It also identifies
unfinished supervisor updates and prints listener URLs.

The report contains corrective commands and pending operation/workflow IDs. It
omits database URLs, tokens, environment contents, and raw service error text.
Doctor does not initialize missing state, acknowledge operations, restart
services, or change permissions. Unix permission checks use owner/group/mode bits;
review ACLs separately when they grant additional access. The disk check is a
baseline check; capture and restore perform their own operation-specific checks.

Use `--no-health` to skip HTTP requests while inspecting a stopped or inaccessible
runtime. Checkpoint coverage can remain incomplete until first startup initializes
BSM or external directories have been declared in `checkpoint_paths`.

## Identify the installed supervisor

```sh
bsm-supervisor version
```

The result includes the package version, permanent environment, source reference,
resolved Git revision when known, and dirty status for local Git checkouts. Source
copies without Git or preserved provenance report an unknown revision. This
command does not require a supervisor configuration file.

For updates, see [updating the supervisor](update-supervisor.md). For a complete
container lifecycle exercise, see [Proxmox validation](validation.md).

## BSM and supervisor lifecycle

Console startup can automatically start BSM (`web_auto_start`, default `true`).
Stopping the console leaves BSM running unless `stop_bsm_with_supervisor` is
`true`. The optional coupling uses the generated standard console systemd service
and also stops BSM on console failure or restart. Change it while BSM is stopped
and refresh the managed unit, or use Settings in the web console. See
[console lifecycle settings](web-console.md#couple-bsm-to-the-supervisor-console).

### Repair runtime data permissions

If BSM reports permission denied while backing up or updating a Minecraft server,
run `bsm-supervisor doctor --no-health` to check backup-directory access. Files
copied or created as root can leave individual server backup folders unwritable
although `/srv/bsm` itself has the correct owner.

Stop BSM and any other writers, then run as root:

```sh
bsm-supervisor graceful-stop
bsm-supervisor permissions-repair --offline-confirmed
bsm-supervisor start --wait
```

The repair uses the configured systemd unit's User and Group (or the user's primary
group), assigns BSM data and configuration to that account, and restores owner
read/write permissions plus directory traversal. Existing group/other permissions
are preserved, so private files remain private. It leaves Supervisor state,
installations, startup gates and symlink targets untouched. Nested mounts,
hard-linked files and retained restore workspaces require separate attention;
validation rejects these before changing data. Paths outside the configured BSM
data and configuration roots must be repaired separately. Checkpoint restore
continues to preserve recorded ownership; run repair afterward if that ownership
belongs to a different account or contains previously root-owned runtime data.
