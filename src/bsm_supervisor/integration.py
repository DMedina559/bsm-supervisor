"""Inspect BSM storage without importing it or triggering migrations."""

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config
from .service import SystemdService, managed_command

BOOTSTRAP = "bedrock_server_manager.json"
DATABASE = "bedrock-server-manager.db"
PATH_KEYS = ("servers", "content", "downloads", "backups", "plugins", "themes")


def roots(config: Config) -> list[Path]:
    """Return canonical, nonoverlapping roots explicitly authorized for capture."""
    config.__post_init__()
    paths = sorted(
        {
            path.resolve()
            for path in (config.data_dir, config.config_dir, *config.checkpoint_paths)
        },
        key=lambda path: (len(path.parts), str(path)),
    )
    result: list[Path] = []
    for path in paths:
        if not any(parent in path.parents for parent in result):
            result.append(path)
    return result


def covered(path: Path, authorized: list[Path]) -> bool:
    """Check coverage after resolving symlinks."""
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in authorized)


@contextmanager
def readonly_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite database without allowing writes or extensions."""
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        yield connection
    finally:
        connection.close()


def inspect(config: Config) -> dict:
    """Describe declared local storage and identify incomplete coverage.

    This inspection assumes BSM uses the explicit config/data paths supplied to
    the generated service. It does not resolve arbitrary service overrides.
    """
    authorized = roots(config)
    blockers = []
    bootstrap_path = config.config_dir / BOOTSTRAP
    bootstrap = {}
    if bootstrap_path.exists():
        if bootstrap_path.stat().st_size > 1024 * 1024:
            raise ValueError("BSM bootstrap configuration exceeds the size limit")
        bootstrap = json.loads(bootstrap_path.read_text())
        if not isinstance(bootstrap, dict):
            raise ValueError("BSM bootstrap configuration must be an object")
    default_database = config.config_dir / DATABASE
    url = bootstrap.get("db_url") or "sqlite:///" + str(default_database)
    database = None
    if not isinstance(url, str) or not url.startswith(
        ("sqlite:///", "sqlite+aiosqlite:///")
    ):
        blockers.append("Only local file-backed SQLite checkpoints are supported")
    else:
        raw_path = url.split(":///", 1)[1]
        if "?" in raw_path or "#" in raw_path or raw_path in ("", ":memory:"):
            blockers.append("SQLite URI/query and in-memory databases are unsupported")
        else:
            database = Path(raw_path)
            if not database.is_absolute():
                database = config.data_dir / database
            database = database.resolve()
            if not covered(database, authorized):
                blockers.append("Database is outside the configured checkpoint paths")
    configured_paths = {
        name: config.data_dir / (".downloads" if name == "downloads" else name)
        for name in PATH_KEYS
    }
    schema_revision = None
    if database is not None and not database.is_file():
        blockers.append("No initialized BSM SQLite database found")
    if any(name in bootstrap for name in ("paths", "settings")):
        blockers.append(
            "Legacy bootstrap settings require explicit adoption before checkpoints"
        )
    if database is not None and database.is_file():
        with readonly_database(database) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "settings" not in tables or "alembic_version" not in tables:
                blockers.append(
                    "Database lacks the audited BSM settings/migration schema"
                )
            if "alembic_version" in tables:
                schema_revision = sorted(
                    row[0]
                    for row in db.execute(
                        "SELECT version_num FROM alembic_version LIMIT 10"
                    )
                )
            if "settings" in tables:
                for key, encoded in db.execute(
                    "SELECT key, value FROM settings WHERE key = 'paths' "
                    "OR key LIKE 'paths.%'"
                ):
                    value = json.loads(encoded) if isinstance(encoded, str) else encoded
                    if key == "paths":
                        if not isinstance(value, dict):
                            raise ValueError("Invalid stored BSM paths settings")
                        for name in PATH_KEYS:
                            if value.get(name):
                                if not isinstance(value[name], str):
                                    raise ValueError("Stored BSM paths must be strings")
                                configured_paths[name] = Path(value[name])
                    elif key[6:] in PATH_KEYS and value:
                        if not isinstance(value, str):
                            raise ValueError("Stored BSM paths must be strings")
                        configured_paths[key[6:]] = Path(value)
    for name, path in configured_paths.items():
        if not path.is_absolute():
            path = config.data_dir / path
            configured_paths[name] = path
        if not covered(path, authorized):
            blockers.append(f"paths.{name} is outside the configured checkpoint paths")
    for path in authorized:
        if not path.is_dir():
            blockers.append(
                "Every configured checkpoint root must be an existing directory"
            )
    return {
        "database_kind": "sqlite" if database is not None else "unsupported",
        "database_path": str(database) if database is not None else None,
        "schema_revision": schema_revision,
        "config_dir": str(config.config_dir.resolve()),
        "roots": [str(path) for path in authorized],
        "paths": {key: str(path.resolve()) for key, path in configured_paths.items()},
        "blockers": blockers,
        "supported": not blockers,
    }


def require_offline(config: Config) -> None:
    """Require an inactive managed unit with no pending job or remaining PID."""
    state = SystemdService(config).status()
    if (
        state.get("ActiveState") != "inactive"
        or state.get("MainPID") != "0"
        or state.get("Job") != ""
    ):
        raise ValueError("BSM service must be inactive with no PID or pending job")
    command = state.get("ExecStart", "")
    expected = managed_command(config)
    if (
        "path=/usr/bin/flock ;" not in command
        or f"argv[]={expected} ;" not in command
        or (
            "argv[]=/usr/bin/test ! -e "
            f"{config.config_dir}/supervisor-recovery.required ;"
        )
        not in state.get("ExecCondition", "")
        or not {"BSM_DB_URL", "BSM_DATA_DIR", "BSM_CONFIG_DIR"}.issubset(
            state.get("UnsetEnvironment", "").split()
        )
    ):
        raise ValueError(
            "Checkpoint operations require the current generated managed service"
        )


@contextmanager
def _file_lock(path: Path, mode: int) -> Iterator[None]:
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
        )
        created = True
    except FileExistsError:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        created = False
    with os.fdopen(descriptor, "r+b") as stream:
        if created:
            os.fchmod(stream.fileno(), mode)
            if path.name == "web_server.lock":
                owner = path.parent.stat()
                if owner.st_uid != os.geteuid():
                    os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another BSM runtime owns this configuration") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def offline_guard(config: Config) -> Iterator[None]:
    """Hold launch and runtime locks while the managed service is stopped."""
    require_offline(config)
    maintenance = config.config_dir / "supervisor-maintenance.lock"
    with _file_lock(maintenance, 0o644):
        with _file_lock(config.config_dir / "web_server.lock", 0o600):
            require_offline(config)
            yield
