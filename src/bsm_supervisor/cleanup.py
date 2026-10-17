"""Conservative, resumable removal of registered supervisor installations."""

import os
import re
import shutil
from pathlib import Path

from .activation import selection
from .checkpoint_files import inventory, read_json, sync_directory, write_json
from .checkpoints import list_checkpoints
from .config import Config
from .integration import offline_guard
from .store import Store


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
