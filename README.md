# BSM Supervisor

Installation, version management, updates, and recovery for [Bedrock Server Manager](https://github.com/DMedina559/bedrock-server-manager).

**Status: early development.** The local CLI, validated configuration, durable installation registry, operation journal, and systemd adapter are implemented. Downloading releases, creating environments, activation, checkpoints, recovery automation, and the web interface are not implemented yet.

## Run the foundation

Requires Linux and Python 3.11 or newer. systemd commands require an existing BSM service and permission to control that specific unit. The supervisor does not install units, create service accounts, or grant privileges yet.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml config.toml
# Edit config.toml to use three separate absolute directories.
.venv/bin/bsm-supervisor --config config.toml check
.venv/bin/bsm-supervisor --config config.toml init
.venv/bin/bsm-supervisor --config config.toml installations
.venv/bin/bsm-supervisor --config config.toml history
.venv/bin/bsm-supervisor --config config.toml status
```

Configuration validation does not create directories. Initialization creates the state directory and SQLite database; it does not touch BSM data. Run all commands under the same trusted operator account with exclusive write access to the configured directories.

An existing virtual environment can be inventoried without executing or activating it:

```sh
.venv/bin/bsm-supervisor --config config.toml register release-4.0.0b7 \
  --version 4.0.0b7 --path /opt/bsm/installations/release-4.0.0b7
```

The supplied version is operator-provided metadata, not a verified package version. Registration requires a directory below installations_dir containing pyvenv.cfg and bin/bedrock-server-manager.

The start, stop, and restart commands operate on the configured existing systemd unit. They do not select a registered installation. A successful command means systemd completed its job; application readiness is not checked yet. Stop and restart follow the existing unit's shutdown behavior; coordinated Minecraft maintenance is not implemented. Use these commands only after checking that unit's shutdown behavior.

Mutations use a process lock and persistent journal. A failed service command conservatively leaves an unresolved operation because its final effect may be unknown. After checking the actual service state and ensuring no supervisor command is running, clear an interrupted operation with:

```sh
.venv/bin/bsm-supervisor --config config.toml acknowledge 1 --service-checked
```

Replace 1 with the operation ID from history. Acknowledgement only clears the block; it does not restore data, cancel a systemd job, or retry an operation. Automatic reconciliation is planned.

## Development checks

```sh
python -m pip install .
python -m unittest discover -s tests -v
bsm-supervisor --help
```

CI tests Python 3.11 and 3.14. Service tests use a mocked command boundary; deployment in an actual LXC remains to be validated.

## Scope

BSM Supervisor will manage one BSM installation inside a Linux system, initially targeting an unprivileged Proxmox LXC. It will remain available when BSM is stopped or fails to start.

| Component | Responsibility |
| --- | --- |
| Proxmox | Container lifecycle, resources, networking, and container backups |
| Supervisor | BSM installations, version selection, upgrades, and recovery |
| systemd | Service startup, process tracking, and bounded crash recovery |
| BSM | Minecraft server lifecycle, worlds, plugins, and server operations |

Normal operation will not require Proxmox credentials. Container provisioning is a separate, later feature.

## Planned capabilities

- Select a stable, prerelease, or exact BSM release.
- Install each release into an isolated Python virtual environment.
- Start, stop, and restart the managed BSM service.
- Show installed and available versions, health, logs, and operation history.
- Stage updates before interrupting the running installation.
- Coordinate graceful shutdown and create consistent recovery checkpoints.
- Retain previous installations and support compatibility-aware recovery.
- Provide an independent authenticated web interface and local CLI.
- Offer explicit opt-in Git tag, commit, and branch installations.
- Add optional maintenance windows and scheduled updates after manual updates are reliable.

Release channels select an exact version before installation. Git installations record the resolved commit, including when selected through a branch.

## Installation and persistence boundaries

BSM runtime environments are separate from persistent configuration, databases, plugins, Minecraft server directories, and backups. Updating an environment must not overwrite persistent data.

The supervisor has its own environment, configuration, credentials, and operation journal. It must not import BSM internals or depend on BSM starting successfully. Supervisor updates are separate from managed BSM updates.

BSM runs as an unprivileged service account. Service control uses a narrowly scoped privileged boundary. Web requests must not execute arbitrary shell commands or accept unrestricted package sources. BSM plugins receive no supervisor permissions.

## Update and recovery contract

1. Resolve the requested source to an exact version or commit.
2. Check runtime requirements, free space, source policy, and compatibility.
3. Prepare and validate a separate environment while the existing service runs.
4. Acquire an operation lock and enter maintenance mode.
5. Gracefully stop Minecraft servers and BSM; abort if shutdown cannot be confirmed.
6. Create and validate a recovery checkpoint covering all data the upgrade may change.
7. Atomically select the prepared installation and start BSM.
8. Check readiness within a bounded timeout and record the outcome.

All mutating operations are serialized and journaled durably. After interruption or container reboot, the supervisor reconciles the journal, active installation, and actual service state before accepting another mutation. Failed staging must leave the active installation untouched.

A previous executable alone is not a complete rollback: newer BSM versions may migrate databases or modify other persistent data. Automatic executable rollback is permitted only when compatibility is established. Otherwise recovery requires the matching checkpoint and explicit authorization to restore data. The interface must show the checkpoint time and any progress that restoration would discard.

Checkpoints must account for external server paths and mount points. They must not claim complete recovery when required data was excluded. Retention must protect the active installation and checkpoints needed by retained recovery targets.

## BSM integration

Audit the backend before implementing these contracts; existing capabilities should be reused through supported interfaces.

- Readiness that confirms initialization and migrations have completed.
- Graceful maintenance/shutdown coordination with running Minecraft servers.
- Installed version, Python requirements, and data compatibility metadata.
- Consistent backup/restore or an offline checkpoint strategy.
- A managed-installation indicator to avoid conflicting service/update controls.
- Authenticated communication through the supported API/client where applicable.

Missing compatibility information must be treated as unknown, never assumed safe.

## Implementation order

| Phase | Deliverables | Acceptance criteria |
| --- | --- | --- |
| 1. Foundation | Python package, local CLI, configuration, installation registry, journal, service adapter | Clean installation works; invalid configuration is rejected; state persists across restart; tests run on Python 3.11 and 3.14 only |
| 2. Installation and service control | Exact release discovery/install, isolated environments, systemd units, status/start/stop/restart | Installation failure preserves the current environment; intentional stops remain stopped; crash recovery is bounded; persistent paths remain intact |
| 3. Updates and recovery | Staging, maintenance, checkpoints, atomic activation, readiness, interrupted-operation recovery | Exercise failed downloads, failed startup, incompatible data, concurrent requests, and restart during activation; never run two BSM instances against the same data |
| 4. Independent interface | Authenticated API and recovery UI using BSM frontend styling | Status, logs, update progress, and recovery remain accessible while BSM is down; unauthorized mutations fail; credentials are redacted |
| 5. Optional automation | Git sources, scheduled updates, maintenance windows, separate Proxmox provisioning | Exact commits are recorded; schedules are opt-in; installation inside an existing LXC needs no Proxmox token |

Each phase requires focused automated checks and an integration exercise appropriate to its behavior. Actual systemd and LXC behavior must be validated in a disposable Linux environment; mocked service tests alone do not establish deployment support.

## Initial exclusions

Multi-instance orchestration, Minecraft server management outside BSM, Docker management, and cluster-wide Proxmox administration are outside the first release.

## License

See [LICENSE](LICENSE).

