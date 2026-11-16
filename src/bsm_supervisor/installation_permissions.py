"""Service-readable package environments independent of the operator's umask."""

import os
import stat
from pathlib import Path


def create_root(path: Path) -> None:
    """Create new code directories explicitly without changing existing ancestors."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)


def service_readable(path: Path) -> None:
    """Expose installed code for execution, preserving private metadata and links."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Installation must be a real directory")
    for directory, folders, files in os.walk(path, followlinks=False):
        root = Path(directory)
        root.chmod(stat.S_IMODE(root.stat().st_mode) | 0o555)
        folders[:] = [name for name in folders if not (root / name).is_symlink()]
        for name in files:
            item = root / name
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode) or name in (
                "supervisor-installation.json",
                "verified-version.json",
            ):
                continue
            mode = stat.S_IMODE(info.st_mode)
            item.chmod(mode | 0o444 | (0o111 if mode & 0o111 else 0))
