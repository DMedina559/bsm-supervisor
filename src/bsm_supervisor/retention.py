"""Explicit retention of completed console logs and delivered progress events."""

import fcntl
import os
from datetime import datetime, timedelta, timezone

from .checkpoint_files import read_json
from .config import Config
from .store import Store


def prune(
    config: Config, store: Store, keep_days: int = 30, keep_jobs: int = 100
) -> dict:
    """Preserve pending recovery, recent jobs and undelivered notifications."""
    if (
        type(keep_days) is not int
        or keep_days < 1
        or type(keep_jobs) is not int
        or keep_jobs < 1
    ):
        raise ValueError("Retention must keep at least one day and one job")
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed_jobs = 0
    with store.mutation("journal:prune"):
        cursor = None
        if (config.state_dir / "notifications.json").exists():
            path = config.state_dir / "notification-cursor.json"
            cursor = read_json(path).get("after", 0) if path.exists() else 0
        with store.connect() as db:
            result = db.execute(
                "DELETE FROM events WHERE created_at < ? "
                "AND (? IS NULL OR id <= ?) AND operation_id IN "
                "(SELECT id FROM operations WHERE state != 'running' AND "
                "(workflow_id IS NULL OR workflow_id NOT IN "
                "(SELECT id FROM workflows WHERE state = 'running')))",
                (cutoff.strftime("%Y-%m-%dT%H:%M:%fZ"), cursor, cursor),
            )
            removed_events = result.rowcount
        folder = config.state_dir / "web-jobs"
        jobs = sorted(
            ((read_json(path), path) for path in folder.glob("*.json")),
            key=lambda item: item[0].get("created_at", ""),
            reverse=True,
        )
        for job, path in jobs[keep_jobs:]:
            if job.get("state") not in ("succeeded", "failed", "acknowledged"):
                continue
            if datetime.fromisoformat(job["created_at"]) >= cutoff:
                continue
            lease = os.open(
                path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            try:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                path.with_suffix(".log").unlink(missing_ok=True)
                path.unlink()
                path.with_suffix(".lock").unlink(missing_ok=True)
                removed_jobs += 1
            finally:
                os.close(lease)
    return {"removed_events": removed_events, "removed_jobs": removed_jobs}
