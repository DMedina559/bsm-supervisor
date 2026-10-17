"""Recoverable multi-root restoration without replacing mounted root directories."""

import os
import shutil
import stat
from pathlib import Path

from .checkpoint_files import (
    apply_metadata,
    exists,
    mount_points,
    read_json,
    sync_directory,
    sync_payload,
    verify_payload,
    write_json,
)
from .checkpoints import checkpoint_path, verify
from .config import Config
from .integration import offline_guard, require_offline, roots
from .store import Store


def _plan_path(config: Config, operation_id: int) -> Path:
    if operation_id < 1:
        raise ValueError("Invalid restore operation ID")
    return config.state_dir / f"restore-{operation_id}.json"


def _clear_marker(config: Config, operation_id: int) -> None:
    """Remove only this transaction's durable service startup block."""
    marker = config.config_dir / "supervisor-recovery.required"
    if marker.exists():
        if read_json(marker).get("operation_id") != operation_id:
            raise ValueError("Recovery marker belongs to a different operation")
        marker.unlink()
        sync_directory(marker.parent)


def _directory_metadata(path: Path) -> dict:
    info = path.lstat()
    return {
        "name": ".",
        "kind": "directory",
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mtime_ns": info.st_mtime_ns,
    }


def _move(source: Path, destination: Path) -> None:
    """Move without overwriting an unexpected filesystem entry."""
    if exists(destination):
        raise ValueError("Restore destination unexpectedly exists")
    os.rename(source, destination)
    sync_directory(source.parent)
    sync_directory(destination.parent)


def _validate_plan(config: Config, operation_id: int, plan: dict) -> None:
    """Confine replayable rename actions to configured roots and workspaces."""
    authorized = roots(config)
    records = plan.get("roots", [])
    if plan.get("operation_id") != operation_id or [
        item.get("path") for item in records
    ] != [str(path) for path in authorized]:
        raise ValueError("Restore plan scope differs from current configuration")
    if plan.get("phase") not in ("prepared", "executing", "committed", "rolled_back"):
        raise ValueError("Invalid restore phase")
    actions = plan.get("actions")
    attempted = plan.get("attempted")
    if (
        not isinstance(actions, list)
        or type(attempted) is not int
        or not 0 <= attempted <= len(actions)
    ):
        raise ValueError("Invalid restore action journal")
    for index, record in enumerate(records):
        expected = authorized[index] / f".bsm-restore-{operation_id}-{index}"
        if record.get("workspace") != str(expected) or expected.is_symlink():
            raise ValueError("Invalid restore workspace")
    for action in actions:
        source, destination = Path(action["source"]), Path(action["destination"])
        # Exactly one end is under the recorded staging/originals workspace.
        valid = False
        for root, record in zip(authorized, records):
            workspace = Path(record["workspace"])
            if root not in source.parents or root not in destination.parents:
                continue
            source_internal = workspace in source.parents
            destination_internal = workspace in destination.parents
            if source_internal == destination_internal:
                continue
            for path in (source, destination):
                if path.parent.resolve() != path.parent:
                    raise ValueError("Restore action traverses a symlink")
            valid = True
        if not valid:
            raise ValueError("Restore action escapes its configured root")


def restore(config: Config, store: Store, checkpoint_id: str) -> dict:
    """Stage and verify all roots, then journal each rename before replacing data.

    Replaced originals stay in private workspaces until explicit cleanup.
    Failures leave the operation unresolved for restore-recover.
    """
    with (
        store.mutation(
            "checkpoint:restore:" + checkpoint_id, preserve_on_error=True
        ) as operation_id,
        offline_guard(config),
    ):
        marker = config.config_dir / "supervisor-recovery.required"
        if marker.exists():
            raise ValueError("An existing recovery marker must be reconciled first")
        manifest = verify(config, checkpoint_id)
        authorized = roots(config)
        for root in authorized:
            if not root.is_dir():
                raise ValueError(
                    "Restore requires existing configured root directories"
                )
            if any(root.glob(".bsm-restore-*")):
                raise ValueError(
                    "Clean up or recover existing restore workspaces first"
                )
        mounts = mount_points()
        if any(root in mount.parents for root in authorized for mount in mounts):
            raise ValueError("Restore does not support nested mounts inside a root")
        required: dict[int, tuple[Path, int]] = {}
        for root, record in zip(authorized, manifest["roots"]):
            device = root.stat().st_dev
            count = sum(item.get("size", 0) for item in record["entries"])
            previous = required.get(device, (root, 0))
            required[device] = (root, previous[1] + count)
        for root, count in required.values():
            if shutil.disk_usage(root).free < count + 64 * 1024 * 1024:
                raise ValueError("Insufficient disk space to stage restored data")
        plan: dict = {
            "operation_id": operation_id,
            "checkpoint_id": checkpoint_id,
            "phase": "prepared",
            "attempted": 0,
            "actions": [],
            "roots": [],
        }
        path = _plan_path(config, operation_id)
        plan["roots"] = [
            {
                "path": str(root),
                "workspace": str(root / f".bsm-restore-{operation_id}-{index}"),
                "metadata": [{"path": str(root), "entry": _directory_metadata(root)}],
            }
            for index, root in enumerate(authorized)
        ]
        write_json(path, plan)
        for index, (root, record) in enumerate(zip(authorized, manifest["roots"])):
            workspace = root / f".bsm-restore-{operation_id}-{index}"
            metadata = plan["roots"][index]["metadata"]
            workspace.mkdir(mode=0o700)
            staged = workspace / "new"
            originals = workspace / "old"
            originals.mkdir()
            copied = checkpoint_path(config, checkpoint_id) / "payload" / record["id"]
            shutil.copytree(copied, staged, symlinks=True)
            verify_payload(staged, record["entries"])
            apply_metadata(staged, record["entries"])
            sync_payload(staged, record["entries"])
            # Keep config ancestors and the held runtime-lock inode in place.
            locks = {
                config.config_dir.resolve() / name
                for name in (
                    "web_server.lock",
                    "supervisor-maintenance.lock",
                    "supervisor-recovery.required",
                )
            }

            def actions(directory: Path, new: Path, backup: Path) -> None:
                names = sorted(
                    {item.name for item in directory.iterdir()}
                    | {item.name for item in new.iterdir()}
                )
                for name in names:
                    current, replacement, old = (
                        directory / name,
                        new / name,
                        backup / name,
                    )
                    if current == workspace or current in locks:
                        continue
                    if any(current in lock.parents for lock in locks):
                        if not current.is_dir() or not replacement.is_dir():
                            raise ValueError(
                                "Checkpoint must preserve runtime-lock ancestors"
                            )
                        metadata.append(
                            {
                                "path": str(current),
                                "entry": _directory_metadata(current),
                            }
                        )
                        old.mkdir(parents=True, exist_ok=True)
                        actions(current, replacement, old)
                        continue
                    if exists(current):
                        plan["actions"].append(
                            {"source": str(current), "destination": str(old)}
                        )
                    if exists(replacement):
                        plan["actions"].append(
                            {"source": str(replacement), "destination": str(current)}
                        )

            actions(root, staged, originals)
            # Persist staged paths even if preparation of the next root fails.
            write_json(path, plan)
        _validate_plan(config, operation_id, plan)
        require_offline(config)
        plan["phase"] = "executing"
        write_json(path, plan)
        write_json(
            marker,
            {
                "operation_id": operation_id,
                "checkpoint_id": checkpoint_id,
            },
        )
        store.note(
            operation_id,
            f"Restoring {checkpoint_id}; "
            f"on interruption use restore-recover {operation_id}",
        )
        for index, action in enumerate(plan["actions"]):
            plan["attempted"] = index + 1
            write_json(path, plan)
            _move(Path(action["source"]), Path(action["destination"]))
        for root, record in zip(authorized, manifest["roots"]):
            apply_metadata(root, record["entries"])
            sync_payload(root, record["entries"])
        plan["phase"] = "committed"
        write_json(path, plan)
        _clear_marker(config, operation_id)
        return {
            "operation_id": operation_id,
            "checkpoint_id": checkpoint_id,
            "state": "committed",
            "originals_retained": True,
        }


def recover(config: Config, store: Store, operation_id: int) -> str:
    """Reconcile a committed restore or reverse attempted renames."""
    with store.lock(), offline_guard(config):
        with store.connect() as db:
            row = db.execute(
                "SELECT action, state FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        if row is None or not row["action"].startswith("checkpoint:restore:"):
            raise ValueError("Operation is not a checkpoint restore")
        if row["state"] != "running":
            raise ValueError("Restore recovery requires its unresolved operation")
        path = _plan_path(config, operation_id)
        if not path.exists():
            store.finish(operation_id, "failed", "Restore never reached file staging")
            return "not_started"
        plan = read_json(path)
        _validate_plan(config, operation_id, plan)
        if plan["phase"] == "committed":
            _clear_marker(config, operation_id)
            store.finish(operation_id, "succeeded", "Committed restore reconciled")
            return "committed"
        for index in reversed(range(plan["attempted"])):
            action = plan["actions"][index]
            source, destination = Path(action["source"]), Path(action["destination"])
            source_exists, destination_exists = exists(source), exists(destination)
            if destination_exists and not source_exists:
                _move(destination, source)
            elif not source_exists or destination_exists:
                raise ValueError(
                    "Ambiguous restore state; " "originals retained for inspection"
                )
            plan["attempted"] = index
            write_json(path, plan)
        for record in plan["roots"]:
            for metadata in reversed(record["metadata"]):
                apply_metadata(Path(metadata["path"]), [metadata["entry"]])
        plan["phase"] = "rolled_back"
        write_json(path, plan)
        _clear_marker(config, operation_id)
        store.finish(operation_id, "failed", "Interrupted restore rolled back")
        return "rolled_back"


def cleanup(config: Config, store: Store, operation_id: int) -> None:
    """Discard retained originals only after a resolved transaction."""
    with (
        store.mutation("checkpoint:cleanup:" + str(operation_id)),
        offline_guard(config),
    ):
        plan = read_json(_plan_path(config, operation_id))
        _validate_plan(config, operation_id, plan)
        if plan["phase"] not in ("committed", "rolled_back"):
            raise ValueError("Recover this restore before discarding its workspace")
        with store.connect() as db:
            row = db.execute(
                "SELECT state FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        if row is None or row["state"] == "running":
            raise ValueError("Restore operation is unresolved")
        for record in plan["roots"]:
            workspace = Path(record["workspace"])
            if workspace.exists():
                shutil.rmtree(workspace)
                sync_directory(workspace.parent)
