# BSM environment and listener settings

The supervisor supports all public runtime environment settings read by BSM's
bootstrap configuration, plus the `HOST`/`PORT` interface used by BSM's Docker
entrypoint. The table corresponds to BSM `dev`'s
[bootstrap loader](https://github.com/DMedina559/bedrock-server-manager/blob/dev/src/bedrock_server_manager/config/bcm_config.py)
and [Docker entrypoint](https://github.com/DMedina559/bedrock-server-manager/blob/dev/docker-entrypoint.sh).

| Native Proxmox environment variable | Supervisor behavior |
| --- | --- |
| `BSM_CONFIG_DIR` | Override `bsm_config_dir`; service and checkpoint scope use this directory |
| `BSM_DATA_DIR` | Override `data_dir`; service and checkpoint scope use this directory |
| `BSM_DB_URL` | Override `bsm_db_url`; pin the BSM database and inspect that same database |
| `BSM_LOG_LEVEL` | Override `bsm_log_level`; validate and pin the logging level |
| `HOST` | Docker-compatible alias for `bsm_host` |
| `PORT` | Docker-compatible alias for `bsm_port` |
| `BSM_SUPERVISOR_WEB_HOST` | Explicit supervisor host override; takes priority over `HOST` |
| `BSM_SUPERVISOR_WEB_PORT` | Explicit supervisor port override; takes priority over `PORT` |

Accepted log levels are DEBUG, INFO, WARNING, ERROR, CRITICAL and NOTSET; WARN and
FATAL are normalized to WARNING and CRITICAL. Bind addresses are `0.0.0.0`,
`127.0.0.1`, `::`, or `::1`; ports are 1..65535. New LXC installations bind to
`0.0.0.0:11325`. The BSM control channel remains on loopback. Firewall rules and
internet forwarding remain part of the surrounding network configuration.

The internal `BSM_PLUGIN_RECURSION_GUARD` variable is reserved for BSM child
processes. The managed service explicitly clears it so accidental inheritance
cannot disable normal plugin behavior. Docker build values such as `APP_VERSION`
and `SETUPTOOLS_SCM_PRETEND_VERSION`, documentation tokens, and ordinary Python/
OS environment variables are not BSM runtime configuration options.

## Configure through Proxmox

The host provisioner requires Proxmox VE 9.2+ with native `--env` support. It
populates the six public settings above with defaults; config/data/database paths
are derived consistently. Customize them using
[config/proxmox-env.example.json](../config/proxmox-env.example.json):

```sh
bash scripts/provision-proxmox.sh --bsm-env-file /root/bsm-env.json
```

Advanced mode also accepts the JSON file path. All JSON values must be strings;
only the listed public variables and the two supervisor listener names are
accepted. Keep custom files outside the source checkout. Database URLs are not
included in the printed command plan or process arguments. Native environment
entries remain visible to users authorized to inspect the container configuration.

The script calls Proxmox's native CLI handler in-process to pass its NUL-separated
environment list correctly. A shell argument cannot contain those NUL bytes.
This uses Proxmox's configuration locking and validation, rather than editing
`/etc/pve/lxc/ID.conf` directly. New container defaults are:

```json
{
  "BSM_CONFIG_DIR": "/srv/bsm/config",
  "BSM_DATA_DIR": "/srv/bsm",
  "BSM_DB_URL": "sqlite:////srv/bsm/config/bedrock-server-manager.db",
  "BSM_LOG_LEVEL": "INFO",
  "HOST": "0.0.0.0",
  "PORT": "11325"
}
```

For an existing container, add/edit entries using Proxmox's environment editor.
A single `pct set ID --env VALUE` replaces the entire environment list, so preserve
other entries when making changes. Environment changes need a full container
stop/start and a regenerated managed environment/unit before maintenance resumes.
Changing directory variables selects different data; it does not migrate files.
Set them to the existing paths when adopting a running installation.

The supervisor reads supported values from the command environment or, inside
LXC, `/proc/1/environ`. Command values override native values of the same name;
the explicit supervisor listener names take priority over HOST/PORT aliases.
TOML/defaults apply when no environment override exists. Run operator commands as
root so they can read the init environment and private supervisor state.

## Service and checkpoint consistency

BSM configuration/data paths and host/port are rendered as explicit CLI flags.
Database URLs and log levels are written to the root-only
`state_dir/bsm-runtime.env`, which systemd reads before switching to the `bsm`
account. Database credentials are absent from generated unit text and status
arguments. Environment values are pinned rather than depending on systemd manager
inheritance or `PassEnvironment`.

Before capture, restore, activation, or upgrades, the supervisor verifies that the
private environment file matches its effective configuration and that the unit
loads it. Additional environment files or conflicting unset rules are rejected.
Start/restart commands also validate this configuration before controlling BSM.
A changed environment blocks maintenance until the stopped-service
configuration is regenerated. Unconfigured DB/log environment values are cleared
so an unrelated inherited value cannot silently change the application database.

Local file-backed SQLite remains required for supervisor checkpoints and automatic
upgrade recovery. External database URLs can be passed to BSM, but their driver
extras must be installed in the selected BSM environment and checkpoint workflows
refuse them. URI/query SQLite options and uncovered database files are also
rejected by checkpoint inspection. Readiness tokens still use `api_token_file`.

## Apply changed settings

With the latest supervisor installed, edit the native environment or TOML. For
TOML, the equivalent keys are `bsm_config_dir`, `data_dir`, `bsm_db_url`,
`bsm_log_level`, `bsm_host`, and `bsm_port`. Then, inside the container:

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
```

Adapt the service account/unit name if configured differently. `service-environment`
refuses a running service and never prints the private values. Fresh installations
create this environment file before installing the unit. Existing installations
must regenerate both after updating; see [updating the supervisor](update-supervisor.md).
