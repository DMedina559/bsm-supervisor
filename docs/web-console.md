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
It is a trusted operator interface. Filesystem paths, detailed health reports,
schedule editing, archive transfers, and supervisor self-updates remain CLI
operations. Job logs and progress remain local until explicitly accessed; manage
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
