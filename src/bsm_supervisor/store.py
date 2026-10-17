"""Durable installation inventory and serialized operation journal."""

import fcntl
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class BusyError(RuntimeError):
    """Another mutation or an interrupted operation needs attention."""


class Store:
    """Persist metadata without importing or modifying BSM."""

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = state_dir / "supervisor.sqlite3"
        self._lock_owner: int | None = None
        self._workflow_id: int | None = None
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError(f"Unsupported supervisor database version: {version}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    operation_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE TABLE IF NOT EXISTS installations (
                    id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    registered_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY,
                    action TEXT NOT NULL,
                    state TEXT NOT NULL CHECK
                        (state IN ('running', 'succeeded', 'failed', 'acknowledged')),
                    detail TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_operation
                    ON operations(state) WHERE state = 'running';
                CREATE TABLE IF NOT EXISTS workflows (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL CHECK
                        (state IN ('running', 'succeeded', 'failed')),
                    created_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    finished_at TEXT,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_workflow
                    ON workflows(state) WHERE state = 'running';
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(operations)")}
            if "workflow_id" not in columns:
                db.execute("ALTER TABLE operations ADD COLUMN workflow_id INTEGER")
            db.execute("PRAGMA user_version = 2")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived transaction with full durability."""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous = FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def installations(self) -> list[dict]:
        """Return registered installations in a stable order."""
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM installations ORDER BY id")
            ]

    def operations(self) -> list[dict]:
        """Return the latest operations, including unresolved interruptions."""
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM operations ORDER BY id DESC LIMIT 100"
                )
            ]

    def begin(self, action: str) -> int:
        """Claim the single mutation slot across processes."""
        with self.lock(), self.connect() as db:
            self._check_workflow(db)
            try:
                cursor = db.execute(
                    "INSERT INTO operations(action, state, workflow_id) "
                    "VALUES (?, 'running', ?)",
                    (action, self._workflow_id),
                )
            except sqlite3.IntegrityError as exc:
                raise BusyError(
                    "A running or interrupted operation blocks mutations; "
                    "inspect history before acknowledging an interruption"
                ) from exc
            assert cursor.lastrowid is not None
            db.execute(
                "INSERT INTO events(operation_id, state) VALUES (?, 'running')",
                (cursor.lastrowid,),
            )
            return cursor.lastrowid

    def finish(self, operation_id: int, state: str, detail: str = "") -> None:
        """Record a completed mutation."""
        if state not in ("succeeded", "failed"):
            raise ValueError("Invalid completion state")
        with self.connect() as db:
            result = db.execute(
                """UPDATE operations SET state = ?, detail = ?,
                finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND state = 'running'""",
                (state, detail, operation_id),
            )
            if result.rowcount != 1:
                raise ValueError("Operation is not running")
            db.execute(
                "INSERT INTO events(operation_id, state, detail) VALUES (?, ?, ?)",
                (operation_id, state, detail),
            )

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclude concurrent commands, including interruption acknowledgement."""
        owner = threading.get_ident()
        if self._lock_owner == owner:
            yield
            return
        with (self.path.parent / "operation.lock").open(
            "a", encoding="utf-8"
        ) as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyError("Another supervisor mutation is in progress") from exc
            self._lock_owner = owner
            try:
                yield
            finally:
                self._lock_owner = None
                fcntl.flock(stream, fcntl.LOCK_UN)

    @contextmanager
    def mutation(
        self, action: str, *, preserve_on_error: bool = False
    ) -> Iterator[int]:
        """Journal a mutation, leaving abrupt interruptions unresolved."""
        with self.lock():
            operation_id = self.begin(action)
            try:
                yield operation_id
            except Exception as exc:
                if preserve_on_error:
                    self.note(operation_id, str(exc))
                else:
                    self.finish(operation_id, "failed", str(exc))
                raise
            else:
                self.finish(operation_id, "succeeded")

    def acknowledge(self, operation_id: int) -> None:
        """Clear an interrupted operation after an operator checks service state.

        Call only while no other supervisor command is running.
        """
        with self.lock(), self.connect() as db:
            self._check_workflow(db)
            row = db.execute(
                "SELECT action FROM operations WHERE id = ? AND state = 'running'",
                (operation_id,),
            ).fetchone()
            if row is not None and row["action"].startswith("checkpoint:restore:"):
                raise ValueError("Use restore-recover for an interrupted data restore")
            result = db.execute(
                """UPDATE operations SET state = 'acknowledged',
                detail = 'Operator acknowledged interruption; no rollback performed',
                finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND state = 'running'""",
                (operation_id,),
            )
            if result.rowcount != 1:
                raise ValueError("Operation is not running")

    def register(
        self, installation_id: str, version: str, path: Path, root: Path
    ) -> None:
        """Inventory an existing environment without activating or executing it."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", installation_id):
            raise ValueError("Invalid installation ID")
        if not version.strip() or len(version) > 128:
            raise ValueError("Version must contain 1 to 128 characters")
        resolved = path.resolve(strict=True)
        if resolved == root.resolve() or root.resolve() not in resolved.parents:
            raise ValueError("Installation must be inside installations_dir")
        for name in ("pyvenv.cfg", "bin/bedrock-server-manager"):
            if not (resolved / name).is_file():
                raise ValueError(f"Missing environment file: {name}")
        with self.mutation("register:" + installation_id):
            self.add_installation(installation_id, version, resolved)

    def add_installation(self, installation_id: str, version: str, path: Path) -> None:
        """Record a validated installation inside an already locked mutation."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO installations(id, version, path) VALUES (?, ?, ?)",
                (installation_id, version, str(path)),
            )

    def note(self, operation_id: int, detail: str) -> None:
        """Persist progress or an uncertain outcome without releasing the slot."""
        with self.connect() as db:
            db.execute(
                "UPDATE operations SET detail = ? WHERE id = ? AND state = 'running'",
                (detail, operation_id),
            )

            db.execute(
                "INSERT INTO events(operation_id, state, detail) "
                "VALUES (?, 'progress', ?)",
                (operation_id, detail),
            )

    def require_idle(self) -> None:
        """Reject queued/interrupted work before an automation stops BSM."""
        with self.connect() as db:
            self._check_workflow(db)
            if db.execute("SELECT 1 FROM operations WHERE state='running'").fetchone():
                raise BusyError("Resolve the pending operation before automation")

    def _check_workflow(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT id FROM workflows WHERE state = 'running'").fetchone()
        if row is not None and (
            row["id"] != self._workflow_id or self._lock_owner != threading.get_ident()
        ):
            raise BusyError(
                "An interrupted upgrade blocks mutations; use upgrade-recover"
            )

    def workflows(self) -> list[dict]:
        """Return durable workflow summaries."""
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM workflows ORDER BY id DESC LIMIT 100"
                )
            ]

    def workflow_operations(self, workflow_id: int) -> list[dict]:
        """Return steps associated with one upgrade."""
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM operations WHERE workflow_id = ? ORDER BY id",
                    (workflow_id,),
                )
            ]

    @contextmanager
    def workflow(self, workflow_id: int | None = None) -> Iterator[int]:
        """Own one workflow and its child mutations under a process/thread lock."""
        with self.lock():
            if self._workflow_id is not None:
                raise BusyError("Nested workflows are not supported")
            with self.connect() as db:
                if workflow_id is None:
                    if (
                        db.execute(
                            "SELECT 1 FROM workflows WHERE state = 'running'"
                        ).fetchone()
                        or db.execute(
                            "SELECT 1 FROM operations WHERE state = 'running'"
                        ).fetchone()
                    ):
                        raise BusyError("Resolve the pending operation/workflow first")
                    cursor = db.execute(
                        "INSERT INTO workflows(kind, state) "
                        "VALUES ('upgrade', 'running')"
                    )
                    assert cursor.lastrowid is not None
                    workflow_id = cursor.lastrowid
                else:
                    row = db.execute(
                        "SELECT state, kind FROM workflows WHERE id = ?", (workflow_id,)
                    ).fetchone()
                    if (
                        row is None
                        or row["state"] != "running"
                        or row["kind"] != "upgrade"
                    ):
                        raise ValueError("Upgrade is not pending recovery")
            self._workflow_id = workflow_id
            try:
                yield workflow_id
            finally:
                self._workflow_id = None

    def finish_workflow(self, workflow_id: int, state: str, detail: str) -> None:
        """Complete a workflow after all child operations have resolved."""
        if (
            state not in ("succeeded", "failed")
            or self._workflow_id != workflow_id
            or self._lock_owner != threading.get_ident()
        ):
            raise ValueError("Workflow completion requires its owning context")
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM operations WHERE state = 'running'"
            ).fetchone():
                raise BusyError("A workflow step still requires recovery")
            db.execute(
                "UPDATE workflows SET state = ?, detail = ?, "
                "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND state = 'running'",
                (state, detail, workflow_id),
            )
