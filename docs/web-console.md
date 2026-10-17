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
start/restart with readiness checks, graceful stop, checkpoint creation,
verification, restore inspection, and explicit data restoration. It also shows
service state, operations, upgrade workflows, scheduled runs, and progress.
Checkpoint creation does not stop BSM automatically; stop it first. Upgrade
handles its own coordinated shutdown. Each console action runs the existing CLI
as a child process, without a shell or arbitrary command execution.

Downtime and data changes require explicit confirmation. Migration acceptance is
separate from confirming maintenance. Review a restore plan before restoring;
restoration replaces saved data and discards newer changes. Binary downgrade is
not an automatic recovery action.

Console jobs have durable IDs and private logs beneath `state_dir/web-jobs`.
Expand a console job to read the last 32 KiB of output, including a restore plan.
The CLI operation journal remains authoritative for interrupted service/data
changes. After inspecting status/history and completing any required upgrade or
restore recovery, acknowledge an interrupted console job:

```bash
bsm-supervisor web-job-acknowledge JOB_ID --service-checked
```

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
