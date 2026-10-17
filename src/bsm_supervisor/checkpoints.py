"""Offline, verified BSM checkpoints with explicit storage coverage."""

import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .activation import selection
from .checkpoint_files import (
    inventory,
    read_json,
    sync_directory,
    verify_payload,
    write_json,
)
from .config import Config
from .integration import (
    covered,
    inspect,
    offline_guard,
    readonly_database,
    require_offline,
    roots,
)
from .store import Store

FORMAT_VERSION = 1


def checkpoint_path(config: Config, checkpoint_id: str) -> Path:
    """Resolve a checkpoint ID beneath the private checkpoint directory."""
    if not re.fullmatch(r"[0-9a-f]{32}", checkpoint_id):
        raise ValueError("Invalid checkpoint ID")
    path = config.state_dir / "checkpoints" / checkpoint_id
    if path.is_symlink():
        raise ValueError("Checkpoint directories cannot be symlinks")
    return path


def load(config: Config, checkpoint_id: str) -> dict:
    """Load and validate checkpoint scope against current operator configuration."""
    path = checkpoint_path(config, checkpoint_id)
    manifest = read_json(path / "manifest.json")
    if (
        manifest.get("format_version") != FORMAT_VERSION
        or manifest.get("id") != checkpoint_id
    ):
        raise ValueError("Unsupported checkpoint format or mismatched identity")
    if (
        not isinstance(manifest.get("created_at"), str)
        or type(manifest.get("total_bytes")) is not int
        or manifest["total_bytes"] < 0
        or not isinstance(manifest.get("installation"), (dict, type(None)))
    ):
        raise ValueError("Invalid checkpoint summary")
    records = manifest.get("roots")
    if (
        not isinstance(records, list)
        or not records
        or any(not isinstance(record, dict) for record in records)
    ):
        raise ValueError("Invalid checkpoint roots")
    authorized = [str(root) for root in roots(config)]
    if [record.get("path") for record in records] != authorized:
        raise ValueError("Checkpoint roots differ from current configured coverage")
    if [record.get("id") for record in records] != [
        str(i) for i in range(len(records))
    ]:
        raise ValueError("Invalid checkpoint payload root IDs")
    return manifest


def verify(config: Config, checkpoint_id: str) -> dict:
    """Verify every payload byte before a checkpoint is eligible for restoration."""
    manifest = load(config, checkpoint_id)
    path = checkpoint_path(config, checkpoint_id)
    payload = path / "payload"
    if payload.is_symlink() or not payload.is_dir():
        raise ValueError("Invalid checkpoint payload directory")
    if set(child.name for child in payload.iterdir()) != {
        record["id"] for record in manifest["roots"]
    }:
        raise ValueError("Unexpected checkpoint payload roots")
    authorized = roots(config)
    for record in manifest["roots"]:
        root = payload / record["id"]
        if root.is_symlink():
            raise ValueError("Checkpoint payload root cannot be a symlink")
        verify_payload(root, record.get("entries"))
        for item in record["entries"]:
            if item["kind"] == "symlink":
                source = Path(record["path"]) / item["name"]
                if not covered(source.parent / item["target"], authorized):
                    raise ValueError(
                        "Checkpoint symlink target is outside its coverage"
                    )
    if manifest["total_bytes"] != sum(
        item.get("size", 0)
        for record in manifest["roots"]
        for item in record["entries"]
    ):
        raise ValueError("Checkpoint byte count does not match its inventory")
    database = manifest.get("database_path")
    if not isinstance(database, str) or not Path(database).is_absolute():
        raise ValueError("Checkpoint has no valid SQLite database path")
    copied_database = None
    for record in manifest["roots"]:
        if Path(database).is_relative_to(Path(record["path"])):
            copied_database = (
                payload / record["id"] / Path(database).relative_to(record["path"])
            )
    if (
        copied_database is None
        or copied_database.is_symlink()
        or not copied_database.is_file()
    ):
        raise ValueError("Checkpoint database is outside the saved payload")
    try:
        with readonly_database(copied_database) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Checkpoint SQLite integrity check failed")
            revisions = sorted(
                row[0]
                for row in db.execute(
                    "SELECT version_num FROM alembic_version LIMIT 10"
                )
            )
            if revisions != manifest.get("schema_revision"):
                raise ValueError("Checkpoint database schema differs from its manifest")
    except sqlite3.Error as error:
        raise ValueError("Checkpoint SQLite database is invalid") from error
    return manifest


def list_checkpoints(config: Config) -> list[dict]:
    """List completed checkpoints without treating unfinished directories as valid."""
    base = config.state_dir / "checkpoints"
    if not base.exists():
        return []
    result = []
    for path in sorted(base.iterdir()):
        if re.fullmatch(r"[0-9a-f]{32}", path.name) and not path.is_symlink():
            try:
                manifest = read_json(path / "manifest.json")
                result.append(
                    {
                        "id": path.name,
                        "created_at": manifest["created_at"],
                        "installation": manifest.get("installation"),
                        "total_bytes": manifest["total_bytes"],
                        "pinned": (path / "pinned").exists(),
                    }
                )
            except (OSError, ValueError, KeyError):
                result.append({"id": path.name, "invalid": True})
    return result


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    """Use SQLite's backup API so WAL commits are included in one portable file."""
    with (
        readonly_database(source) as current,
        closing(sqlite3.connect(destination)) as copied,
    ):
        current.backup(copied)
        copied.execute("PRAGMA journal_mode=DELETE")
        if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite checkpoint integrity check failed")


def create(config: Config, store: Store) -> dict:
    """Capture all declared roots while BSM is offline; publish only after verify."""
    with store.mutation("checkpoint:create") as operation_id, offline_guard(config):
        require_offline(config)
        if (config.config_dir / "supervisor-recovery.required").exists():
            raise ValueError("An interrupted restore requires recovery before capture")
        report = inspect(config)
        if report["blockers"]:
            raise ValueError("; ".join(report["blockers"]))
        authorized = roots(config)
        for root in authorized:
            if not root.is_dir():
                raise ValueError("Every checkpoint root must be an existing directory")
        checkpoint_id = uuid4().hex
        final = checkpoint_path(config, checkpoint_id)
        final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = final.parent / (".partial-" + checkpoint_id)
        staging.mkdir(mode=0o700)
        store.note(operation_id, "Capturing checkpoint " + checkpoint_id)
        database = Path(report["database_path"]) if report["database_path"] else None
        skip = {
            config.config_dir.resolve() / name
            for name in (
                "web_server.lock",
                "web_server.pid",
                "web_server.control.json",
                "supervisor-maintenance.lock",
                "supervisor-recovery.required",
                "supervisor-upgrade.required",
            )
        }
        if database is not None:
            skip.update(Path(str(database) + suffix) for suffix in ("-wal", "-shm"))
        if any(any(root.glob(".bsm-restore-*")) for root in authorized):
            raise ValueError("Recover or clean up previous restore workspaces first")
        estimates = [inventory(root, skip=skip) for root in authorized]
        total = sum(item.get("size", 0) for entries in estimates for item in entries)
        wal = Path(str(database) + "-wal") if database is not None else None
        wal_size = wal.stat().st_size if wal is not None and wal.exists() else 0
        if shutil.disk_usage(staging).free < (total + wal_size) * 2 + 64 * 1024 * 1024:
            raise ValueError("Insufficient checkpoint disk space")
        overrides = {}
        snapshot = staging / "database.sqlite3"
        if database is not None and database.exists():
            _sqlite_snapshot(database, snapshot)
            overrides[database] = snapshot
        payload = staging / "payload"
        payload.mkdir()
        records: list[dict] = []
        for index, root in enumerate(authorized):
            entries = inventory(root, skip=skip, overrides=overrides)
            for item in entries:
                if item["kind"] == "symlink":
                    source = root / item["name"]
                    if not covered(source.parent / item["target"], authorized):
                        raise ValueError(
                            "Symlink targets must be included in checkpoint_paths"
                        )
            copied = payload / str(index)
            shutil.copytree(
                root,
                copied,
                symlinks=True,
                ignore=lambda directory, names: [
                    name for name in names if Path(directory) / name in skip
                ],
            )
            if (
                database is not None
                and database in overrides
                and covered(database, [root])
            ):
                destination = copied / database.relative_to(root)
                shutil.copyfile(snapshot, destination)
            verify_payload(copied, entries)
            records.append({"id": str(index), "path": str(root), "entries": entries})
        snapshot.unlink(missing_ok=True)
        manifest: dict = {
            "format_version": FORMAT_VERSION,
            "operation_id": operation_id,
            "id": checkpoint_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "installation": selection(config, store),
            "schema_revision": report["schema_revision"],
            "database_path": report["database_path"],
            "total_bytes": sum(
                item.get("size", 0) for record in records for item in record["entries"]
            ),
            "roots": records,
        }
        write_json(staging / "manifest.json", manifest)
        # Flush copied files before publishing the complete checkpoint.
        for record in records:
            for item in record["entries"]:
                path = payload / record["id"] / item["name"]
                if item["kind"] == "file":
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            for item in reversed(record["entries"]):
                if item["kind"] == "directory":
                    sync_directory(payload / record["id"] / item["name"])
        sync_directory(payload)
        require_offline(config)
        os.replace(staging, final)
        sync_directory(final.parent)
        return manifest


def pin(config: Config, store: Store, checkpoint_id: str, enabled: bool) -> None:
    """Protect a completed checkpoint from automated retention."""
    with store.mutation("checkpoint:pin:" + checkpoint_id):
        load(config, checkpoint_id)
        path = checkpoint_path(config, checkpoint_id) / "pinned"
        if enabled:
            path.touch(mode=0o600, exist_ok=True)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        else:
            path.unlink(missing_ok=True)
        sync_directory(path.parent)


def prune(config: Config, store: Store, keep: int) -> list[str]:
    """Retain pinned checkpoints and a recovery target for each installed version."""
    if keep < 1:
        raise ValueError("At least one recent checkpoint must be retained")
    with store.mutation("checkpoint:prune"):
        if not (config.state_dir / "checkpoints").exists():
            return []
        checkpoints = sorted(
            list_checkpoints(config),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )
        protected = {item["id"] for item in checkpoints[:keep]}
        for path in config.state_dir.glob("restore-*.json"):
            target = read_json(path).get("checkpoint_id")
            if isinstance(target, str):
                protected.add(target)
        installed = {item["id"] for item in store.installations()}
        seen = set()
        for item in checkpoints:
            try:
                verify(config, item["id"])
            except (OSError, ValueError, KeyError):
                item["invalid"] = True
            installation = item.get("installation") or {}
            owner = installation.get("id")
            if item.get("pinned") or item.get("invalid"):
                protected.add(item["id"])
            if owner in installed and owner not in seen and not item.get("invalid"):
                protected.add(item["id"])
                seen.add(owner)
        removed = []
        for item in checkpoints:
            if item["id"] not in protected:
                shutil.rmtree(checkpoint_path(config, item["id"]))
                removed.append(item["id"])
        sync_directory(config.state_dir / "checkpoints")
        return removed
