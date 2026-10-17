# Coordinated upgrades

The supervisor can prepare a published BSM release, gracefully shut down the current runtime, capture and pin a checkpoint, activate the release, and require authenticated application readiness.

This workflow targets the audited BSM 4.0 runtime with an initialized SQLite database and fully declared filesystem coverage. It uses existing backend HTTP contracts and does not require backend changes. Automated schema compatibility negotiation is still unavailable; accepting an upgrade authorizes the selected release's forward migrations.

## Configure readiness and startup control

Install this supervisor version into its own Python environment, then regenerate and review the managed systemd unit. Its ExecCondition invokes the independently installed supervisor launchguard module, so the BSM service account needs read/execute access to that Python environment. Restarting or rolling back BSM does not replace the supervisor environment.

The unit uses both a persistent restore marker and an upgrade launch guard. During a controlled trial startup, the guard accepts a short-lived permit tied to the controller's process generation and kernel boot ID. A dead controller, reused PID, stale permit, or different boot cannot authorize startup.

The controller normally runs as root inside the LXC. It creates protected, credential-free permit records below /run/bsm-supervisor. Permission failures are checked before the upgrade stops BSM.

Configure a private file containing an existing BSM bearer token:

    api_token_file = "/var/lib/bsm-supervisor/bsm-api.token"
    readiness_timeout = 60

Set permissions to 0600. The token needs access to GET /api/health; a dedicated ordinary BSM user can supply readiness access. Graceful shutdown uses the private local runtime token, not this user's administrator privileges.

Tokens are never included in workflow journals, CLI output, or request errors. Health and shutdown requests use verified loopback endpoints, disable proxy inheritance, and refuse redirects. Refresh an expired/revoked bearer token before proceeding.

Runtime identity verification requires matching systemd PID, process generation, configuration, command line, and local control record. An older or unverifiable control record fails closed. There is no automatic forced-termination fallback.

## Check readiness or stop gracefully

    bsm-supervisor --config config.toml health
    bsm-supervisor --config config.toml graceful-stop

Readiness requires ready=true, lifecycle=ready, healthy checks, and an exact running-version match. HTTP 200 by itself is insufficient. Graceful stop verifies the shutdown receipt's PID/instance identity and waits for an inactive service.

A startup that fails before publishing a verifiable control record may require an operator to stop the unit with systemctl before recovery. A fully stopped failed service can have its failure state reset; the supervisor does not kill an unverified running process.

## Run an upgrade

Check storage coverage and ensure no unmanaged BSM processes, standalone CLI mutations, Minecraft processes, or other external writers can modify the declared roots during maintenance.

    bsm-supervisor --config config.toml integration
    bsm-supervisor --config config.toml upgrade prerelease --maintenance-confirmed --accept-migrations

Use stable or an exact published version instead of prerelease when appropriate. A lower version is rejected. An intentional repair using the same version requires --allow-same-version.

For an active source installation, readiness/authentication is verified before downtime. Installation and dependency verification happen while the current runtime remains available. An already prepared target is checked before reuse.

After preparation, the workflow:

1. Places a durable upgrade startup block.
2. Requests authenticated graceful shutdown and waits for the service to stop.
3. Creates and verifies a checkpoint, recording its original installation/schema.
4. Pins the checkpoint against retention.
5. Atomically selects the target installation.
6. Authorizes controlled startup and waits for semantic health and version identity.
7. Commits the successful result and removes the startup block.

An upgrade starts and validates the target even when the original service was initially stopped. Cancellation before target startup restores that original running/stopped state.

BSM's existing shutdown lifecycle drains work and stops managed servers. The supervisor does not yet provide a backend maintenance mode that suppresses autostart plugins or prevents Minecraft clients joining during target startup. Plan the maintenance window accordingly. Any world/application writes after the checkpoint would be lost by an explicit checkpoint restoration.

## Inspect progress

    bsm-supervisor --config config.toml upgrades
    bsm-supervisor --config config.toml upgrade-status UPGRADE_ID
    bsm-supervisor --config config.toml history

A durable workflow owns the entire operation, while its individual installation/service/checkpoint steps retain their own journal entries. Other supervisor mutations are blocked until the workflow completes or is recovered. A version-1 supervisor database migrates in place without discarding inventory/history.

The upgrade record includes the source/target, phase, checkpoint ID, whether target startup was ever attempted, and any sanitized failure. Ctrl+C leaves recoverable state and exits with code 130.

## Recover an interrupted or failed upgrade

An unhealthy startup retains the selected target and data for inspection. The controller attempts graceful shutdown, removes its trial permit, and keeps the durable startup block. If graceful shutdown cannot be verified, stop the unit explicitly before recovering.

Retry the prepared target without restoring older data:

    bsm-supervisor --config config.toml upgrade-recover UPGRADE_ID --action resume --maintenance-confirmed

Cancel before target startup was attempted:

    bsm-supervisor --config config.toml upgrade-recover UPGRADE_ID --action cancel --maintenance-confirmed

The recorded startup-intent boundary is conservative: once startup may have run migrations, cancellation without data restoration is refused.

Explicitly restore the checkpoint and its original BSM installation:

    bsm-supervisor --config config.toml upgrade-recover UPGRADE_ID --action restore --maintenance-confirmed --restore-data-confirmed

Restoration replaces newer application/world data with the checkpoint contents. This is never automatic. The supervisor verifies the saved checkpoint and original environment, restores data, selects the original installation, and checks its readiness.

Interrupted child restores are reconciled before further recovery. A completed restore is recorded so a retry does not apply the checkpoint again and discard subsequent writes. Retained pre-restore contents remain available under their restore operation ID. After verification, stop BSM and use restore-cleanup as described in the checkpoint guide.

Generic acknowledge cannot bypass a pending upgrade. Use upgrade-recover for the parent workflow; its child operations are linked in the journal.

Upgrade checkpoints remain pinned after completion or rollback. Unpin them explicitly when they are no longer needed, then apply checkpoint retention.

## Validation and remaining boundaries

Tests exercise successful upgrades, unhealthy startup, retry/cancellation, explicit schema/data rollback, interrupted restoration, stale controller/boot identities, authentication failures, semantic health/version checks, and supervisor database migration.

A disposable BSM 4.0.0b7 same-version repair smoke test exercised the real HTTP authentication, graceful shutdown, checkpoint, version selection, restart, and health sequence. Its service adapter simulated systemd and translated the executor's procfs namespace. Actual Proxmox LXC/systemd behavior still requires deployment validation.

Git sources, scheduled updates/maintenance windows, installation/provisioning automation, backend maintenance-mode integration, and the independent authenticated web interface remain subsequent milestones.
