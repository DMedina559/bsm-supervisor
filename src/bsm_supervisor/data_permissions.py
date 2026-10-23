"""Repair adopted runtime data without exposing private files or following links."""

import grp
import os
import pwd
import stat
from pathlib import Path
from typing import Iterator

from .config import Config
from .integration import offline_guard
from .service import service_backend
from .store import Store


def entries(config: Config) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield runtime entries, excluding links, mounts and recovery workspaces."""
    roots = sorted(
        {config.data_dir, config.config_dir}, key=lambda path: len(path.parts)
    )
    selected: list[Path] = []
    for root in roots:
        if any(parent == root or parent in root.parents for parent in selected):
            continue
        if root.resolve() != root or not root.is_dir():
            raise ValueError(f"Runtime root must be an existing real directory: {root}")
        selected.append(root)

        def walk(path: Path) -> Iterator[tuple[Path, os.stat_result]]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                return
            if path != root and path.is_mount():
                raise ValueError(
                    f"Nested runtime mount requires separate repair: {path}"
                )
            if path.name.startswith(".bsm-restore-"):
                raise ValueError(
                    "Resolve and clean up restore workspaces before repair"
                )
            if path.parent == config.config_dir and path.name.startswith("supervisor-"):
                return
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError(
                    f"Hard-linked runtime file requires manual repair: {path}"
                )
            if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                yield path, info
            if stat.S_ISDIR(info.st_mode):
                for child in sorted(path.iterdir()):
                    yield from walk(child)

        yield from walk(root)


def repair(config: Config, store: Store) -> dict:
    """Assign runtime data to the actual service account while BSM is stopped."""
    if os.geteuid() != 0:
        raise ValueError("permissions-repair requires root")
    with store.mutation("permissions:repair"), offline_guard(config):
        state = service_backend(config).status()
        user = state.get("User")
        if not user:
            raise ValueError("Managed service must specify an unprivileged User")
        account = pwd.getpwnam(user)
        group = state.get("Group")
        gid = grp.getgrnam(group).gr_gid if group else account.pw_gid
        if account.pw_uid == 0:
            raise ValueError("Managed service must not run as root")
        # Validate the complete scope before changing any runtime entry.
        targets = list(entries(config))
        changed = 0
        for path, info in targets:
            mode = stat.S_IMODE(info.st_mode)
            required = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
            if (info.st_uid, info.st_gid) != (
                account.pw_uid,
                gid,
            ) or mode & required != required:
                os.chown(path, account.pw_uid, gid, follow_symlinks=False)
                os.chmod(path, mode | required, follow_symlinks=False)
                changed += 1
        return {"user": user, "gid": gid, "changed": changed}
