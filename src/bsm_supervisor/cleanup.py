"""Conservative, resumable removal of registered supervisor installations."""

import os
import re
import shutil
import stat
from pathlib import Path
from uuid import uuid4

from .activation import selection
from .checkpoint_files import (
    inventory,
    mount_points,
    read_json,
    sync_directory,
    write_json,
)
from .checkpoints import list_checkpoints
from .config import Config
from .integration import offline_guard
from .store import BusyError, Store


def remove(config: Config, store: Store, installation_id: str) -> None:
    """Delete one owned installation after offline and recovery checks."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", installation_id):
        raise ValueError("Invalid installation ID")
    with store.mutation("installation-remove:" + installation_id):
        with offline_guard(config):
            current = selection(config, store)
            if current and current["id"] == installation_id:
                raise ValueError("The selected installation cannot be removed")
            for checkpoint in list_checkpoints(config):
                if checkpoint.get("invalid"):
                    raise ValueError("Invalid checkpoint metadata blocks cleanup")
                if (checkpoint.get("installation") or {}).get("id") == installation_id:
                    raise ValueError("A checkpoint needs this installation")
            row = next(
                (r for r in store.installations() if r["id"] == installation_id), None
            )
            if row is None:
                raise ValueError("Installation is not registered")
            root = config.installations_dir.resolve()
            target = root / installation_id
            quarantine = root / (".cleanup-" + installation_id)
            record = config.state_dir / ("cleanup-" + installation_id + ".json")
            expected = dict(
                id=installation_id, path=str(target), quarantine=str(quarantine)
            )
            if (
                Path(row["path"]) != target
                or target.is_symlink()
                or quarantine.is_symlink()
            ):
                raise ValueError("Installation path is not a managed direct child")
            if os.path.ismount(target) or os.path.ismount(quarantine):
                raise ValueError("Mounted installation directories cannot be removed")
            if record.exists():
                if read_json(record) != expected:
                    raise ValueError("Cleanup record does not match this installation")
            else:
                manifest = read_json(target / "supervisor-installation.json")
                if manifest.get("path") != str(target) or manifest.get(
                    "source"
                ) not in ("git", "pypi"):
                    raise ValueError(
                        "Installation has no matching supervisor provenance"
                    )
                inventory(
                    target
                )  # Reject nested mounts and special files before deletion.
                if quarantine.exists():
                    raise ValueError("Cleanup destination already exists")
                write_json(record, expected)
            if target.exists():
                if quarantine.exists():
                    raise ValueError("Ambiguous cleanup directories")
                target.rename(quarantine)
                sync_directory(root)
            if quarantine.exists():
                inventory(quarantine)
                shutil.rmtree(quarantine)
                sync_directory(root)
            with store.connect() as db:
                db.execute("DELETE FROM installations WHERE id = ?", (installation_id,))
            record.unlink()
            sync_directory(config.state_dir)


def build_id(installation_id: str) -> str:
    if not re.fullmatch(
        r"(?:release-[A-Za-z0-9][A-Za-z0-9.!_+-]{0,119}|git-[0-9a-f]{40})",
        installation_id,
    ):
        raise ValueError("Unknown managed build ID")
    return installation_id


def failed_build_target(config: Config, store: Store, installation_id: str) -> Path:
    """Identify an unregistered direct child created by a recorded build."""
    root = config.installations_dir.resolve()
    target = root / build_id(installation_id)
    if target.is_symlink() or not target.is_dir():
        raise ValueError("The failed build directory is unavailable")
    if os.path.ismount(target) or any(
        mount == target or target in mount.parents for mount in mount_points()
    ):
        raise ValueError("The failed build contains a mount")
    if any(
        row["id"] == installation_id or row["path"] == str(target)
        for row in store.installations()
    ):
        raise ValueError("This installation is registered")
    selection(config, store)
    for checkpoint in list_checkpoints(config):
        if (
            checkpoint.get("invalid")
            or (checkpoint.get("installation") or {}).get("id") == installation_id
        ):
            raise ValueError("Checkpoint protection prevents moving this directory")
    with store.connect() as db:
        evidence = db.execute(
            "SELECT 1 FROM events "
            "JOIN operations ON operations.id = events.operation_id "
            "WHERE events.detail = ? "
            "AND operations.state IN ('failed', 'acknowledged') "
            "AND (operations.action = ? OR operations.action LIKE 'install-git:%') "
            "LIMIT 1",
            (
                "Creating virtual environment: " + str(target),
                "install:" + installation_id.removeprefix("release-"),
            ),
        ).fetchone()
    if evidence is None:
        raise ValueError(
            "The journal does not identify this directory as a failed build"
        )
    return target


def inspect(config: Config, store: Store, installation_id: str) -> dict:
    installation_id = build_id(installation_id)
    target = config.installations_dir.resolve() / installation_id
    registered = next(
        (item for item in store.installations() if item["id"] == installation_id), None
    )
    blocker = None
    pending_upgrade = None
    if registered is None:
        try:
            failed_build_target(config, store, installation_id)
            store.require_idle()
        except BusyError:
            pending_upgrade = next(
                (row["id"] for row in store.workflows() if row["state"] == "running"),
                None,
            )
            blocker = (
                "Resolve the pending upgrade before moving this build aside"
                if pending_upgrade
                else "Resolve the pending operation before moving this build aside"
            )
        except ValueError as exc:
            blocker = str(exc)
    output = ""
    logs = config.state_dir / "logs"
    if not logs.is_symlink():
        try:
            descriptor = os.open(
                logs / (installation_id + ".log"),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if stat.S_ISREG(info.st_mode):
                    stream.seek(max(0, info.st_size - 65536))
                    output = stream.read(65536).decode("utf-8", errors="replace")
    return {
        "id": installation_id,
        "registered": registered,
        "directory_exists": target.exists(),
        "can_archive": registered is None and blocker is None,
        "blocker": blocker,
        "pending_upgrade": pending_upgrade,
        "output": output,
    }


def archive_failed(config: Config, store: Store, installation_id: str) -> dict:
    """Move a failed build outside the installable ID namespace without deleting it."""
    with store.lock(), offline_guard(config):
        store.require_idle()
        target = failed_build_target(config, store, installation_id)
        archive = target.parent / (".failed-" + installation_id + "-" + uuid4().hex)
        with store.mutation(
            "installation-archive-failed:" + installation_id
        ) as operation_id:
            store.note(operation_id, "Moving failed build to " + str(archive))
            target.rename(archive)
            sync_directory(target.parent)
    return {"id": installation_id, "archived_path": str(archive)}
