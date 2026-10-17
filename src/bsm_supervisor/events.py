"""Read durable operation progress and deliver explicitly configured notifications."""

import json
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from .checkpoint_files import read_json, write_json
from .transfer import private_secret


def read(state_dir: Path, after: int = 0, limit: int = 200) -> list[dict]:
    """Read a bounded event page without creating or migrating the database."""
    database = state_dir / "supervisor.sqlite3"
    if not database.exists():
        return []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
            return []
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                (max(0, after), min(1000, max(1, limit))),
            )
        ]


def configure(state_dir: Path, source: Path) -> None:
    """Copy a private HTTPS webhook configuration; never echo its secret URL."""
    value = json.loads(private_secret(source))
    if not isinstance(value, dict) or set(value) != {"url"}:
        raise ValueError("Notification configuration requires only an HTTPS url")
    if not isinstance(value["url"], str) or any(ord(c) < 32 for c in value["url"]):
        raise ValueError("Notification URL must be a single-line string")
    parsed = urlsplit(value["url"])
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Notification URL must be HTTPS without embedded credentials")
    write_json(state_dir / "notifications.json", value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep private webhook URLs and payloads on their configured destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver(state_dir: Path, store=None) -> dict:
    """Deliver ordered completion events; retry failed events on the next call."""
    from .store import Store

    store = store or Store(state_dir)
    with store.lock():
        config_path = state_dir / "notifications.json"
        if not config_path.exists():
            return {"enabled": False, "delivered": 0}
        settings = json.loads(private_secret(config_path))
        cursor_path = state_dir / "notification-cursor.json"
        cursor = read_json(cursor_path) if cursor_path.exists() else {"after": 0}
        delivered = 0
        for event in read(state_dir, cursor["after"], 100):
            if event["state"] in ("succeeded", "failed"):
                # Error text, configuration, tokens and paths are never sent.
                payload = {
                    key: event[key]
                    for key in ("id", "operation_id", "state", "created_at")
                }
                request = urllib.request.Request(
                    settings["url"],
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), NoRedirect()
                )
                try:
                    with opener.open(request, timeout=5) as response:
                        if not 200 <= response.status < 300:
                            raise OSError("Webhook rejected delivery")
                except (OSError, urllib.error.URLError) as exc:
                    raise ValueError(
                        "Notification delivery failed; cursor retained for retry"
                    ) from exc
                delivered += 1
            cursor["after"] = event["id"]
            write_json(cursor_path, cursor)
        return {"enabled": True, "delivered": delivered, "after": cursor["after"]}
