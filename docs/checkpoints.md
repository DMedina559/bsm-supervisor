# Checkpoint operations

Checkpoints preserve BSM data independently of its Python environments. They are stored beneath state_dir/checkpoints in private directories. The versioned manifest records the selected installation, SQLite migration revision, declared roots, byte counts, SHA-256 file hashes, symlink targets, numeric ownership, modes, and modification times.

The implementation currently targets the audited BSM 4.0 SQLite configuration/settings layout. It does not claim coverage for legacy layouts or arbitrary plugin storage outside the declared roots.

## Inspect coverage

Configure bsm_config_dir when adopting a configuration directory outside data_dir. Its default is data_dir/config.

Add external server, content, plugin, theme, download, backup, or database-parent directories to checkpoint_paths in the supervisor TOML file:

    bsm_config_dir = "/etc/bedrock-server-manager"
    checkpoint_paths = ["/mnt/bedrock-servers", "/mnt/bsm-content"]

Every checkpoint root must already exist as a directory. Contained roots are deduplicated, and supervisor state/runtime directories cannot be included.

    bsm-supervisor --config config.toml integration

The inspection reads bedrock_server_manager.json and SQLite settings without importing BSM or invoking database migrations. It reports configured roots, discovered paths, schema revision, and coverage blockers. It never returns the database URL or credentials.

The generated service supplies explicit config/data paths and removes BSM_DB_URL, BSM_CONFIG_DIR, and BSM_DATA_DIR environment overrides. Custom commands or other effective path/database overrides require adoption before these operations can establish their scope.

PostgreSQL/MySQL/MariaDB, in-memory SQLite, SQLite URI options, and unknown or uninitialized database layouts are rejected. Backend-specific dump/restore providers remain future work; unsupported databases are never silently omitted.

Plugin-owned files outside the known BSM path settings cannot be inferred automatically. Declare their directories explicitly. Root scopes must remain unchanged when restoring a checkpoint; restore is not a relocation/import command.

## Capture and verify

Stop Minecraft servers gracefully, then stop BSM. Confirm no unmanaged BSM instances, standalone BSM CLI mutations, Minecraft server processes, or other writers are using the same data.

    bsm-supervisor --config config.toml checkpoint-create --offline-confirmed
    bsm-supervisor --config config.toml checkpoints
    bsm-supervisor --config config.toml checkpoint-verify CHECKPOINT_ID

The service must be inactive with no PID or pending job. The supervisor takes an exclusive maintenance lock and BSM's runtime lock. The generated systemd command takes the maintenance lock before Python starts, preventing managed startup from loading or migrating the database during maintenance.

SQLite's backup API folds committed WAL data into one validated database snapshot. Transient runtime PID/control/lock files and SQLite WAL/SHM sidecars are excluded; stale runtime credentials are not restored. The live lock inodes and the recovery marker remain in place throughout restoration.

Payloads are copied and checked against their inventories, flushed to disk, and published by renaming a private partial directory. Interrupted captures never appear as completed checkpoints. Failed partial directories remain for inspection; remove them only after inspecting history and resolving any interrupted capture. These are local recovery copies, not off-host disaster-recovery backups.

Disk-space checks reserve space for payloads, SQLite snapshot overhead, and restore staging. Failure to allocate or flush data aborts the operation. A power-loss guarantee still depends on the underlying filesystem and storage honoring fsync.

## Restore data

Review the saved timestamp, installation, schema revision, root list, and byte count first:

    bsm-supervisor --config config.toml restore-plan CHECKPOINT_ID

Restoration replaces the saved roots' contents, including removing newer files absent from the snapshot. It does not change the selected BSM version or start the service. Establish version/schema compatibility yourself; select the matching saved BSM version when appropriate.

With BSM and all other writers stopped:

    bsm-supervisor --config config.toml checkpoint-restore CHECKPOINT_ID --offline-confirmed --restore-data-confirmed

All payloads must verify before data replacement begins. New contents are staged on each destination filesystem; originals are retained in private .bsm-restore-OPERATION_ID-ROOT workspaces. Each atomic rename is journaled before execution. A durable supervisor-recovery.required marker is written before replacement begins. The generated systemd ExecCondition checks this marker before startup, keeping BSM stopped after a supervisor crash or reboot until recovery clears it.

Restore works inside existing root directories, preserving their inodes rather than replacing a mounted root. It is a recoverable multi-root transaction, not a single atomic swap across filesystems.

After verifying the restored contents, discard retained originals while the service is still stopped:

    bsm-supervisor --config config.toml restore-cleanup OPERATION_ID --offline-confirmed --discard-originals

Discarding originals is explicit because it removes the immediate path back to the pre-restore contents. The checkpoint itself remains available. Existing workspaces must be recovered/cleaned before another restore or capture.

## Recover an interrupted restore

A failed or interrupted restore blocks further supervisor mutations. Use its operation ID from history:

    bsm-supervisor --config config.toml history
    bsm-supervisor --config config.toml restore-recover OPERATION_ID --offline-confirmed

Recovery reverses attempted renames to return to the pre-restore contents. A transaction already marked committed is reconciled as successful instead. A failure before staging is reconciled without replacing files. Recovery itself is journaled so it can be retried after another interruption.

Do not move retained originals or edit target files before recovery. Ambiguous filesystem states fail closed and retain the originals for inspection. Generic acknowledge cannot bypass an unresolved data restore.

After recovery, inspect the contents and use restore-cleanup to remove its workspace.

## Retention

Pin important checkpoints and retain a minimum recent history:

    bsm-supervisor --config config.toml checkpoint-pin CHECKPOINT_ID
    bsm-supervisor --config config.toml checkpoint-pin CHECKPOINT_ID --unpin
    bsm-supervisor --config config.toml checkpoint-prune --keep 3 --delete-confirmed

Retention protects pinned checkpoints, recent checkpoints, and the latest verified recovery checkpoint for every registered installation that has one. Invalid or differently scoped checkpoints are retained for inspection. At least one recent checkpoint must be retained.

## Filesystem and deployment boundaries

Regular files, directories, symbolic links within the declared coverage, numeric UID/GID, modes, and modification times are supported. Sockets, devices, FIFOs, and nested mounts inside a checkpoint root are rejected. A checkpoint root itself may be a mounted directory; nested mounts must be reorganized into separate, nonoverlapping roots.

Hard-link identity, sparse allocation, ACLs, extended attributes, filesystem flags, and arbitrary plugin-managed external databases are not guaranteed by the manifest format. Do not treat this as a filesystem-image backup. Checkpoints may contain application credentials and world data; keep their private parent permissions intact.

The supervisor operator needs access to all captured data and permission to restore numeric owners, normally root inside the LXC. The BSM service account needs access to its configuration directory, runtime environments, base Python interpreter, and launch lock.

Automated tests exercise real filesystem restoration, SQLite WAL snapshots, maintenance-lock exclusion, and recovery after a subprocess exits during a rename. systemd service-state responses are mocked in those tests. A disposable unprivileged Proxmox LXC must still validate actual service startup/shutdown, account mapping, mount layout, reboot recovery, and storage behavior before production deployment.

## Existing BSM integration contracts

The backend already exposes authenticated GET /api/health and POST /api/runtime/shutdown. The health route can return HTTP 200 while degraded, so future readiness checks must inspect its ready/lifecycle/check fields. Runtime shutdown supports an administrator credential or the private local shutdown token and returns instance/PID identity.

The BSM CLI loads application state before its web runtime lock, which can run migrations. Recovery tooling therefore must not launch that CLI merely to inspect or stop an unknown/broken installation. Future coordinated upgrades should use the independent service/API controls and explicit runtime identity checks.

No backend changes were made for these checkpoint commands. Coordinated shutdown/readiness, automatic schema compatibility, update orchestration, Git installations, scheduling, provisioning, and the independent UI remain subsequent milestones.
