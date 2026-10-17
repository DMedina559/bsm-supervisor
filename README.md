# BSM Supervisor

Install, manage, upgrade, and recover [Bedrock Server Manager](https://github.com/DMedina559/bedrock-server-manager) inside a Linux system or Proxmox LXC.

**Early development.** The supervisor currently provides a local CLI and managed systemd integration. An independent web interface, scheduled updates, and maintenance windows are planned.

## Get started

Choose the guide for your installation:

| Task | Guide |
| --- | --- |
| Create a new container from the Proxmox host | [Proxmox host setup](docs/proxmox-host.md) |
| Install inside an existing container | [LXC installation](docs/install-lxc.md) |
| Update the supervisor itself | [Supervisor updates](docs/update-supervisor.md) |
| Configure BSM networking, paths, database, and logging | [Environment and listener settings](docs/listener-environment.md) |
| Upgrade BSM with a recovery checkpoint | [Coordinated upgrades](docs/upgrades.md) |
| Capture, restore, and retain checkpoints | [Checkpoint operations](docs/checkpoints.md) |
| Install Git revisions or remove unused environments | [Git sources and cleanup](docs/sources-cleanup.md) |

The container installer supports Debian 12+ and Ubuntu 24.04+ with systemd and Python 3.11+. Host provisioning requires Proxmox VE 9.2+ with native container environment support. Run administration commands as root inside the container; BSM runs as the unprivileged `bsm` account.

Guides use `/etc/bsm-supervisor/config.toml` and `bedrock-server-manager-webui.service`, the installer defaults. For another configuration file, put `--config /absolute/path/config.toml` before the command. Adapt unit names, accounts, storage pools, and network examples to your deployment. Uppercase identifiers such as `INSTALLATION_ID` represent values from command output.

## Manage BSM

After completing the installation guide:

```sh
bsm-supervisor check
bsm-supervisor installations
bsm-supervisor selection
bsm-supervisor status
bsm-supervisor history
```

Discover and prepare a release without changing the active selection:

```sh
bsm-supervisor releases --channel stable
bsm-supervisor install stable
# To select the latest prerelease instead:
bsm-supervisor install prerelease
```

Each release has its own Python environment. Installation verifies dependencies and package metadata without importing BSM or migrating its data. Git sources require explicit permission to execute upstream build code. Failed installation directories and command logs are retained for inspection.

For a new installation with no existing BSM data, select the ID printed by `installations`:

```sh
bsm-supervisor activate INSTALLATION_ID --offline-confirmed
bsm-supervisor start
```

Activation requires a stopped, current managed unit and its private environment file. It selects an environment without starting BSM or restoring data. Use the [upgrade workflow](docs/upgrades.md) when changing versions against existing data.

```sh
bsm-supervisor health
bsm-supervisor graceful-stop
bsm-supervisor start
```

`health` requires a configured BSM bearer token. `graceful-stop` verifies the local BSM runtime identity and requests application shutdown. `start`, `stop`, and `restart` control systemd; a completed systemd job does not establish application readiness. Direct stop/restart follow the unit's shutdown behavior, so use graceful shutdown for planned maintenance.

## Recovery and storage

The supervisor serializes mutations with a process lock and durable journal. An interrupted operation can block further changes. Inspect `history` and the actual service state before recovery. Use the dedicated recovery commands for [upgrades](docs/upgrades.md) and [restores](docs/checkpoints.md). For other interrupted operations, `acknowledge OPERATION_ID --service-checked` clears the journal block after inspection; it does not undo changes or retry an operation.

Checkpoints cover declared configuration/data directories and supported local SQLite databases. Declare external server and plugin paths before capture. Restoring data can discard changes made after the checkpoint; selecting an older executable alone does not undo database migrations. Local checkpoints supplement an independent off-host backup.

| Component | Responsibility |
| --- | --- |
| Proxmox | Container lifecycle, resources, networking, and container backups |
| Supervisor | BSM environments, version selection, upgrades, checkpoints, and recovery |
| systemd | Service startup, process tracking, and bounded crash recovery |
| BSM | Minecraft servers, worlds, plugins, and application operations |

Supervisor environments, state, and credentials are separate from BSM environments and persistent data. Updating the supervisor does not update BSM. Normal container administration requires no Proxmox credentials. Multi-instance orchestration, Docker management, and cluster administration are outside the current scope.

## Development

From a source checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
.venv/bin/python -m black --check src tests
.venv/bin/python -m isort --check-only src tests
.venv/bin/python -m flake8 src tests
.venv/bin/python -m mypy src
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/pre-commit install
```

CI covers Python 3.11 and 3.14, formatting, lint, type checking, and installer shell checks. Automated tests include filesystem/SQLite recovery and simulated service/provisioning boundaries. Validate startup, shutdown, networking, mounts, and recovery in a disposable container before relying on a deployment.

## License

See [LICENSE](LICENSE).
