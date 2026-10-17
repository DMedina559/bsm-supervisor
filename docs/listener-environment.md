# BSM listener and Proxmox environment

The supervisor controls BSM's bind address and port through explicit CLI flags.
New LXC installations use `0.0.0.0:11325`, making BSM reachable at the container's
IPv4 address. The Python configuration defaults to loopback when neither a TOML
setting nor an environment override is supplied.

```toml
bsm_host = "0.0.0.0"
bsm_port = 11325
```

Supported bind addresses are `0.0.0.0`, `127.0.0.1`, `::`, and `::1`. Wildcards
support LAN access; loopback addresses support a proxy running in the same
container. IPv6 wildcard behavior also depends on the OS socket configuration.
Ports must be integers from 1 to 65535. The runtime control channel continues to
use loopback, including when the public listener uses a wildcard address.
Opening a listener does not open Proxmox/OPNsense firewall ports or configure
internet forwarding.

## Native Proxmox container environment

The host provisioner targets **Proxmox VE 9.2+** and checks for the native `--env`
option before making changes. Older host versions can use the existing-container
installer instead. New containers receive:

```text
BSM_SUPERVISOR_WEB_HOST=0.0.0.0
```

The supervisor supports these public listener overrides:

| Variable | Effect |
| --- | --- |
| `BSM_SUPERVISOR_WEB_HOST` | Override TOML `bsm_host` |
| `BSM_SUPERVISOR_WEB_PORT` | Override TOML `bsm_port` |

Precedence is the current command's environment, the LXC PID 1 environment, then
TOML/defaults. The PID 1 fallback is only used when `/run/systemd/container`
identifies LXC and the caller has permission to read its environment. Normal
operator commands should run as root inside the container. Only the two variables
above are imported into supervisor configuration; credentials are not returned.
This covers `pct enter` shells that do not inherit the container runtime values.

Set these in Proxmox's container environment editor. For a container with no other
native environment entries, this host command sets the bind address:

```sh
pct set 101 --env 'BSM_SUPERVISOR_WEB_HOST=0.0.0.0'
```

`--env` replaces the whole native environment list. If other entries already
exist, preserve them using the environment editor instead of replacing the list.
Apply changes with a full container stop/start, then regenerate the managed unit
as described below. A supervisor shell override applies to that command alone;
prefer TOML or the container environment for persistent operation.

The audited BSM code reads `BSM_LOG_LEVEL` directly. The generated unit passes
that variable from systemd's manager environment using `PassEnvironment`.
Native container environment values are supplied to the container init process;
check manager propagation with `systemctl show-environment` if changing logging.
If necessary, import only this public setting with
`systemctl import-environment BSM_LOG_LEVEL` from a shell containing it.

The audited BSM web runtime reads host/port from CLI flags or database settings,
so the supervisor translates its variables into `--host` and `--port`. It does
not assume a `BSM_WEB_HOST` variable exists. BSM path/database overrides remain
excluded from the managed service: checkpoint coverage relies on the explicit
configuration/data paths. Configure those paths through supervisor TOML instead.
Readiness credentials continue to use the private `api_token_file`.

## Update an existing installation

Update the supervisor using the latest [LXC installer](install-lxc.md), **without**
`--install-bsm-unit`, so it preserves your existing unit and configuration.
If using retained bootstrap files, download fresh source first: that checkout
contains the earlier supervisor code. The latest installer includes all previous
status-parser and PATH fixes.

For the existing container 101, you can keep using TOML rather than changing its
Proxmox environment. Inside the container:

1. Set `bsm_host = "0.0.0.0"` and `bsm_port = 11325` in
   `/etc/bsm-supervisor/config.toml`, once the new supervisor is installed.
2. Gracefully stop BSM and render/verify the updated unit.
3. Install the verified unit and restart BSM.

```sh
bsm-supervisor check
bsm-supervisor graceful-stop
bsm-supervisor unit --user bsm > /tmp/bedrock-server-manager-webui.service
systemd-analyze verify --man=no /tmp/bedrock-server-manager-webui.service
install -m 0644 /tmp/bedrock-server-manager-webui.service /etc/systemd/system/bedrock-server-manager-webui.service
systemctl daemon-reload
bsm-supervisor start
ss -ltnp
```

Only proceed to install the candidate unit after verification succeeds. Adapt the
service name/user if your configuration differs. Expect a listener on
`0.0.0.0:11325` with the example settings. Unit validation expects the newly
rendered host/port flags; a unit from before this feature must be regenerated.
BSM remains responsible for account authentication and application setup.
