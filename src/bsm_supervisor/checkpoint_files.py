"""Bounded manifest I/O and ordinary POSIX filesystem inventories."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 200_000


def exists(path: Path) -> bool:
    """Include dangling symlinks when testing an entry."""
    return path.exists() or path.is_symlink()


def sync_directory(path: Path) -> None:
    """Persist directory entry changes on Linux filesystems."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, value: dict) -> None:
    """Replace a private JSON record durably."""
    encoded = json.dumps(value, indent=2, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("Checkpoint metadata exceeds the size limit")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    sync_directory(path.parent)


def read_json(path: Path) -> dict:
    """Read a bounded JSON object."""
    if (
        not stat.S_ISREG(path.lstat().st_mode)
        or path.stat().st_size > MAX_MANIFEST_BYTES
    ):
        raise ValueError("Invalid checkpoint metadata file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Checkpoint metadata must be an object")
    return value


def digest(path: Path) -> str:
    """Hash a regular file without following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Checkpoint payload contains a special file")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(value: str) -> PurePosixPath:
    """Validate a manifest path before joining it to any directory."""
    path = PurePosixPath(value)
    if (
        not isinstance(value, str)
        or path.is_absolute()
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise ValueError("Unsafe checkpoint entry path")
    return path


def mount_points() -> set[Path]:
    """Read Linux mountinfo, including bind mounts missed by os.path.ismount."""
    path = Path("/proc/self/mountinfo")
    result = set()
    if path.exists():
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 5:
                decoded = re.sub(
                    r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]
                )
                result.add(Path(decoded))
    return result


def inventory(
    root: Path,
    *,
    skip: set[Path] | None = None,
    overrides: dict[Path, Path] | None = None,
) -> list[dict]:
    """Inventory files/links without traversing symlinks or nested mounts."""
    ignored = skip or set()
    replacements = overrides or {}
    mounts = mount_points()
    entries = []

    def visit(path: Path, name: str) -> None:
        if path in ignored:
            return
        info = path.lstat()
        mode = info.st_mode
        item = {
            "name": name,
            "mode": stat.S_IMODE(mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns,
        }
        if stat.S_ISLNK(mode):
            item.update(kind="symlink", target=os.readlink(path))
        elif stat.S_ISDIR(mode):
            if path != root and (path in mounts or os.path.ismount(path)):
                raise ValueError(
                    "Nested mounts must be reorganized as separate checkpoint roots"
                )
            item["kind"] = "directory"
        elif stat.S_ISREG(mode):
            source = replacements.get(path, path)
            item.update(kind="file", size=source.stat().st_size, sha256=digest(source))
        else:
            raise ValueError(
                f"Unsupported socket, device, or FIFO in checkpoint: {path}"
            )
        entries.append(item)
        if len(entries) > MAX_ENTRIES:
            raise ValueError("Checkpoint has too many filesystem entries")
        if item["kind"] == "directory":
            for child in sorted(path.iterdir()):
                child_name = child.name if name == "." else name + "/" + child.name
                visit(child, child_name)

    visit(root, ".")
    return entries


def validate_entries(entries: list[dict]) -> None:
    """Reject duplicates, traversal, and children beneath non-directories."""
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid checkpoint entry inventory")
    known: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("Invalid checkpoint entry")
        name = item.get("name")
        if not isinstance(name, str):
            raise ValueError("Invalid checkpoint path")
        path = relative(name)
        kind = item.get("kind")
        if name in known or kind not in ("directory", "file", "symlink"):
            raise ValueError("Duplicate or invalid checkpoint entry")
        for field in ("uid", "gid", "mode", "mtime_ns"):
            if type(item.get(field)) is not int or item[field] < 0:
                raise ValueError("Invalid filesystem metadata")
        if item["mode"] > 0o7777:
            raise ValueError("Invalid file permissions")
        if kind == "file" and (
            type(item.get("size")) is not int
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
        ):
            raise ValueError("Invalid checkpoint file record")
        if kind == "symlink" and not isinstance(item.get("target"), str):
            raise ValueError("Invalid symlink record")
        if name != "." and known.get(path.parent.as_posix()) != "directory":
            raise ValueError("Checkpoint entry parent must be a preceding directory")
        known[name] = kind
    if known.get(".") != "directory":
        raise ValueError("Checkpoint roots must be directories")


def verify_payload(root: Path, entries: list[dict]) -> None:
    """Compare payload names, types, links, lengths, and hashes with the manifest."""
    validate_entries(entries)
    actual = inventory(root)
    if {item["name"] for item in actual} != {item["name"] for item in entries}:
        raise ValueError("Checkpoint payload inventory differs from its manifest")
    expected = {item["name"]: item for item in entries}
    for item in actual:
        saved = expected[item["name"]]
        fields = (
            ("kind", "target")
            if item["kind"] == "symlink"
            else (("kind", "size", "sha256") if item["kind"] == "file" else ("kind",))
        )
        if any(item.get(field) != saved.get(field) for field in fields):
            raise ValueError("Checkpoint payload integrity check failed")


def apply_metadata(root: Path, entries: list[dict]) -> None:
    """Restore numeric ownership, modes, and timestamps after staging contents."""
    for item in reversed(entries):
        path = root / item["name"]
        info = path.lstat()
        if (info.st_uid, info.st_gid) != (item["uid"], item["gid"]):
            os.chown(path, item["uid"], item["gid"], follow_symlinks=False)
        if item["kind"] != "symlink":
            os.chmod(path, item["mode"])
        os.utime(path, ns=(item["mtime_ns"], item["mtime_ns"]), follow_symlinks=False)


def sync_payload(root: Path, entries: list[dict]) -> None:
    """Flush staged files and directories before a restore can be committed."""
    for item in entries:
        path = root / item["name"]
        if item["kind"] == "file":
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for item in reversed(entries):
        if item["kind"] == "directory":
            sync_directory(root / item["name"])
