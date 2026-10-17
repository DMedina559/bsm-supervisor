# Supervisor web console

The supervisor console runs independently of BSM. It remains available when the
BSM service is stopped or fails. It manages one configured BSM instance and uses
the same CLI locks, checkpoints, upgrade workflows, and recovery guards.

## Install and start

The LXC installer includes the web and encrypted-transfer dependencies. For a
manual Python environment, install the package with its `all` extra. Update an
older supervisor before enabling the console; see [supervisor updates](update-supervisor.md).

Run inside the LXC as root:

```bash
bsm-supervisor web-token /etc/bsm-supervisor/console-token
bsm-supervisor web-unit \
  --token-file /etc/bsm-supervisor/console-token \
  > /etc/systemd/system/bsm-supervisor-console.service
systemd-analyze verify --man=no /etc/systemd/system/bsm-supervisor-console.service
systemctl daemon-reload
systemctl enable --now bsm-supervisor-console.service
```

The generated unit listens on `127.0.0.1:11326`. Forward that port over SSH and
open `http://127.0.0.1:11326`, or place an HTTPS reverse proxy in front of it.
Read `/etc/bsm-supervisor/console-token` inside the container and enter its contents
in the console. The token grants supervisor administration, including service
control and data restoration. It is separate from BSM accounts and its local
readiness/shutdown credentials.

For an HTTPS reverse proxy, generate the unit with your actual public origin:

```bash
bsm-supervisor web-unit \
  --token-file /etc/bsm-supervisor/console-token \
  --origin https://supervisor.example.com \
  > /etc/systemd/system/bsm-supervisor-console.service
systemctl daemon-reload
systemctl restart bsm-supervisor-console.service
```

The reverse proxy must preserve the original `Host` header and reach the
container's loopback listener. A proxy outside the container needs a separately
configured reachable listener using the `web --host` command and network rules
that restrict access to that proxy. Nonloopback listeners require an HTTPS
origin; the backend connection itself remains HTTP. Proxy headers are ignored.
Origin values have no trailing slash. Subpath hosting is not supported.

The console keeps its access token only in page memory. Refreshing the page or
locking the console requires entering it again. API requests require the bearer
token; no login cookie, browser storage, or cross-origin API access is used.
Host checking and restrictive security headers also apply. Keep the existing
private token file when updating the supervisor. To rotate it, replace the file
atomically with a newly generated private token; subsequent requests using the
old token are rejected without restarting the console.

## Actions and recovery

The console provides version/channel installation, offline selection, upgrades,
start/restart with managed-process checks, graceful stop, checkpoint creation,
verification, restore inspection, and explicit data restoration. It also shows
service state, operations, upgrade workflows, scheduled runs, and progress.
Checkpoint creation does not stop BSM automatically; stop it first. Upgrade
handles its own coordinated shutdown. Busy actions return HTTP 409; validation failures return HTTP 400. The console
disables duplicate submissions while an action is pending or requires recovery.
Each console action runs the existing CLI
as a child process, without a shell or arbitrary command execution.

Downtime and data changes require explicit confirmation. Migration acceptance is
separate from confirming maintenance. Review a restore plan before restoring;
restoration replaces saved data and discards newer changes. Binary downgrade is
not an automatic recovery action.

Console jobs have durable IDs and private logs beneath `state_dir/web-jobs`.
Expand a console job to read the last 32 KiB of captured output, including a
restore plan. Jobs are listed newest first. Captured output is capped at 8 MiB;
use `journal-prune` for completed-log retention.
The CLI operation journal remains authoritative for interrupted service/data
changes. After inspecting status/history and completing any required upgrade or
restore recovery, acknowledge an interrupted console job:

```bash
bsm-supervisor web-job-acknowledge JOB_ID --service-checked
```

Live process leases prevent acknowledgement while a console child is executing.
The `web_job_timeout` configuration value limits each command to 3600 seconds by
default (1–86400). Timeout terminates its process group and records an interrupted
job requiring inspection; it does not restore data or acknowledge CLI operations.

This clears console bookkeeping only. It does not clear a pending CLI operation,
restore data, or restart BSM. Restarting/updating the console can interrupt a
running console child command; finish those jobs before console maintenance.

The console unit runs as root because existing service management requires root.
It is a trusted operator interface. The console provides health reports, service logs, schedule editing, archive
transfers, and supervisor self-updates. Storage relocation and console
listener/origin changes remain host configuration tasks. Job logs and progress remain local until explicitly accessed; manage
log retention according to available storage. All authenticated console users have the same administrative permissions.

The footer links to the project source. If deploying a modified network-facing
version, provide users access to the corresponding source for that version as
required by AGPLv3; update the source link to your fork or source offer.


## HTTPS proxy example

With Caddy running inside the LXC, configure the console origin to match the
hostname and proxy its loopback listener:

```caddyfile
supervisor.example.com {
    reverse_proxy 127.0.0.1:11326
}
```

Keep the console bound to loopback for this arrangement. The proxy preserves the
Host header; forwarded identity headers are ignored by the console.

## Automatic BSM startup

After the console listener is ready, the supervisor attempts to start the selected
BSM installation once. An already running service is left running. Missing
installations, pending operations, interrupted console jobs, recovery markers, and
unfinished supervisor updates prevent automatic startup; the console remains
available for inspection and recovery. Startup never installs or selects a version.

Set `web_auto_start = false` in the supervisor configuration to disable this
behavior, then restart the console service. This is a startup action, not a
watchdog: manually stopping BSM leaves it stopped until another start request or
a console restart.

Console Start and Restart confirm that the managed process is running. They do
not require a BSM API bearer token. Use `bsm-supervisor health` or CLI `start --wait`
for authenticated application readiness. Older BSM releases may require
`api_token_file` for those checks; creating a BSM user remains part of BSM setup.
A successful process start does not guarantee application readiness.

The console bundles the BSM frontend theme and branded icons for offline use.
The pinned source revision and file hashes are recorded in
`assets/frontend/SOURCE.json`; the assets retain their MIT license.

### A previous readiness check blocked further actions

A failed or interrupted start can leave an operation pending even while BSM is
running. Review `bsm-supervisor history`, `bsm-supervisor status`, and the service
journal. If the only failure was a missing readiness credential and the managed
service state is understood, acknowledge that operation explicitly:

```sh
bsm-supervisor acknowledge OPERATION_ID --service-checked
```

This clears the operation guard without restarting BSM or claiming application
readiness. An interrupted console job has a separate acknowledgement described
above. Do not acknowledge migration or restore failures without completing the
corresponding recovery procedure.

## Backups and database restoration

Open **Backups & restore** to create a coordinated BSM checkpoint. The supervisor
gracefully stops a running BSM, captures the database and configured roots, then
starts the process again after successful capture. Failed work leaves the
service stopped for inspection. Checkpoints include a SQLite backup, configuration,
and all configured data roots. External database engines are unsupported.

Use **Prepare download** for a portable `.bsmcheckpoint` archive. Enable encryption
and enter a password of at least 16 bytes to encrypt it. Use **Upload archive**,
then **Verify & import** to add an uploaded checkpoint. For encrypted uploads,
enter the original archive password. Failed password attempts retain the upload
for retry. Import verifies scope, file hashes, SQLite integrity and schema
metadata; it never overwrites an existing checkpoint or restores current data.

Uploads and downloads use authenticated requests and opaque IDs; tokens never
appear in download links. Passwords and optional GitHub credentials travel in
private job requests, not command arguments or job logs, and are removed when
consumed. Staged archives expire after 24 hours. Expired files are cleaned on the
next upload; use **Delete staged file** to remove them immediately. Staging allows
at most 20 archives and uploads enforce a total quota of twice `web_transfer_limit`.
The default per-archive/import payload limit is 10 GiB; change it in Settings.
The server streams transfers. Supported desktop browsers can save directly to a
chosen file; other browsers use their in-memory Blob download fallback.

Use **Inspect contents** or **Restore wizard** to review database location, schema
revision, version, coverage and replacement effects. The wizard requires the
matching BSM version (and exact Git installation for source builds), confirms
downtime/data replacement, creates a pinned pre-restore checkpoint, and retains
originals for recovery. Starting BSM afterward is optional. Full restoration is
the supported selection: database-only or individual-file restoration is not
offered because it can leave the restored state inconsistent. Version selection
is unchanged. A preview digest is rechecked before BSM is stopped.

Pin checkpoints to protect them. Deletion preserves pinned checkpoints, recorded
restore targets and the newest checkpoint for each installed version. Retention
keeps recent checkpoints and protected recovery versions. Clean up retained
originals from a resolved restore in **Supervisor** while BSM is stopped before
creating another checkpoint. Recovery failures require their normal explicit
restore/upgrade recovery procedure.

## Automation, settings and updates

**Automation** creates and edits systemd schedules, runs them within their UTC
maintenance windows, removes owned timers and acknowledges inspected run
bookkeeping. Maintenance and migration permissions are explicit. Edits retain
the previous policy privately and refuse active scheduled jobs or foreign units.
Configure an HTTPS webhook without revealing its saved URL; use a notification
schedule for automatic delivery and retry of completion events.

**Settings** edits supported timeouts, transfer limits, console autostart, lifecycle
coupling and BSM listener, port, log level and database URL. Existing database URLs
are concealed;
a blank replacement leaves them unchanged. Proxmox/environment overrides are
shown and cannot be replaced through the form. Runtime listener, database and
lifecycle changes require BSM to be stopped and refresh the managed unit without starting it. The
previous TOML file is retained privately as `state_dir/config-before-console.toml`.
If unit refresh fails, inspect history and use **Refresh managed unit** after
resolving the failure. Storage paths and service identity are read-only.

**Supervisor** shows the installed version/revision, checks available releases,
updates from a published tag or explicit branch/commit, and resumes interrupted
updates. `latest` selects a stable release; choose a beta tag explicitly for a
prerelease. Public repositories need no GitHub token. Private access accepts a
one-operation token which is not saved in configuration. Updates share CLI
recovery behavior and may stop/restart BSM. Finish the update, then use **Restart
console**, reload and reconnect to load the new console code. Restart requires
the generated `bsm-supervisor-console.service`; custom units need a host restart.

Health checks, service logs and diagnostics appear in **Action output**, with a
downloadable last report. Read-only checks remain available for interrupted
console jobs. Acknowledging a console job checks its process lease and does not
acknowledge underlying operations or perform service/data recovery.

## Couple BSM to the supervisor console

By default, stopping the console leaves BSM running. Enable **Stop BSM when the
supervisor console stops** in Settings to tie BSM to the standard
`bsm-supervisor-console.service`. Stop BSM before saving this setting; saving
refreshes its managed unit and leaves BSM stopped.

With coupling enabled, systemd stops BSM when the console stops or fails. It sends
SIGTERM through the managed service and allows its configured shutdown timeout
before terminating remaining processes. This also applies during a console
restart. **Start BSM after console startup** independently controls whether BSM
starts again; normal recovery and maintenance guards still apply. If shutdown is
still in progress after a console failure, startup waits within `service_timeout`
before checking those guards. A timeout leaves BSM stopped for inspection.

For CLI configuration, set `stop_bsm_with_supervisor = true` in the supervisor
TOML file while BSM is stopped, then run:

```bash
bsm-supervisor unit-refresh --offline-confirmed
bsm-supervisor start
```

Set it to `false` and refresh the unit while stopped to return to independent
operation. Coupling requires the generated standard console service; a console
started manually in a shell or under a custom unit does not satisfy that
systemd dependency. Starting BSM with coupling enabled also starts its console
dependency. It does not grant access to either web interface.
