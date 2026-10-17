# Scheduled maintenance and notifications

Schedules are opt-in systemd timers. Each policy defines one action, an explicit
UTC calendar, a UTC maintenance window, and ISO weekdays. Timers are not
persistent: missed runs are not executed immediately after a reboot. Every run
checks its window again before taking action.

## Create a schedule

Write a policy file, then install it inside the LXC as root:

```json
{
  "name": "nightly-check",
  "calendar": "*-*-* 02:00:00 UTC",
  "action": "check-updates",
  "selector": "stable",
  "window_start": "01:00",
  "window_end": "04:00",
  "days": [1, 2, 3, 4, 5, 6, 7]
}
```

```bash
bsm-supervisor schedule-install /root/nightly-check.json
bsm-supervisor schedules
systemctl list-timers 'bsm-supervisor-schedule-*'
```

Policies are copied into private `state_dir/schedules`. Remove a schedule before
installing a replacement. `calendar` uses systemd `OnCalendar` syntax and must end
in ` UTC`. Times use `HH:MM`; weekdays are Monday=1 through Sunday=7. Overnight
windows are associated with their starting weekday. For example, Monday
23:00–02:00 includes Tuesday 01:00. The end time is exclusive. Windows control
when work may **start**; an upgrade already running may finish after the window.

| Action | Behavior and additional fields |
| --- | --- |
| `check-updates` | Record the latest eligible version and whether it is newer; `selector` is `stable` or `prerelease` |
| `upgrade` | Coordinated checkpointed upgrade only when a newer version exists; requires `maintenance_confirmed: true` and `accept_migrations: true` |
| `checkpoint` | Gracefully stop a running managed service, capture, then restart with readiness verification; requires `maintenance_confirmed: true` |
| `export` | Export the most recent completed checkpoint to an existing absolute `destination` directory; optional absolute `passphrase_file` and `require_mount` |
| `prune` | Apply existing protected retention with `keep` of at least 1; requires `maintenance_confirmed: true` |
| `journal-prune` | Remove old completed console logs and progress events; `keep_days` defaults to 30, `keep_jobs` to 100; requires `maintenance_confirmed: true` |
| `notify` | Deliver queued completion notifications to the explicitly configured webhook |

An export never overwrites an existing archive. Re-exporting the same checkpoint
to the same destination fails visibly; create a new checkpoint or choose another
destination. Keep external storage mounted before a scheduled export. Local
checkpoint pruning does not delete exported copies. Automatic exports and
checkpoints should have separate, nonoverlapping windows; concurrent CLI,
console, and timer mutations are serialized or rejected by the existing lock.
A busy job is not silently retried outside its policy window.

A checkpoint schedule restores the prior running state only after successful
capture. If capture or restart fails, inspect the service and journal; it does
not automatically roll back or clear an interruption. Failed or interrupted
scheduled runs block that schedule until acknowledged:

```bash
bsm-supervisor history
bsm-supervisor status
bsm-supervisor schedule-acknowledge nightly-check --service-checked
```

Recover any pending upgrade, restore, or CLI operation first. Schedule
acknowledgement only clears timer bookkeeping. To remove a schedule, wait for
its job to finish, then run:

```bash
bsm-supervisor schedule-remove nightly-check --service-checked
```

## Progress and notifications

`events` reads durable operation starts, progress notes, and completion states.
Cursor IDs let a client resume after reconnecting:

```bash
bsm-supervisor events --after 0 --follow
```

Webhook delivery is disabled by default. Create a mode-0600 JSON file owned by
root containing only `{"url":"https://your-endpoint.example/webhook"}`. Then:

```bash
bsm-supervisor notifications-configure /root/notification-webhook.json
bsm-supervisor notifications-deliver
```

Use a `notify` schedule for recurring delivery. It still needs a calendar,
window, and weekdays. Only operation completion events are sent, as generic JSON
with `id`, `operation_id`, `state`, and `created_at`; error details, data paths,
credentials, and configuration are excluded. This is a generic receiver format,
not a Discord/Slack-specific integration. HTTP proxies and redirects are not
used. HTTPS certificate verification remains enabled.

Delivery is ordered, bounded to 100 events per invocation, and at least once.
The durable cursor advances after success; a timeout or rejected request retains
the failed event for retry. Receivers should deduplicate by event ID. The first
delivery includes retained completion events from the journal. Delivery failure
does not change the outcome of the original operation. A notification transport
failure records `retry` and the next permitted timer run attempts delivery again;
it does not require schedule acknowledgement. Failed destructive maintenance
continues to require inspection and acknowledgement. Disable delivery with:

```bash
bsm-supervisor notifications-disable
```

Remove old progress and completed console logs explicitly, or configure a
`journal-prune` schedule:

```bash
bsm-supervisor journal-prune --keep-days 30 --keep-jobs 100 --delete-confirmed
```

Cleanup preserves recent jobs, interrupted/running jobs, unresolved recovery
records, and events awaiting delivery while notifications are configured.
Operation/workflow history is retained. SQLite reuses deleted event pages;
cleanup does not compact the database file. Console output is capped at 8 MiB
per job. The `history` command displays the latest 100 operations.
