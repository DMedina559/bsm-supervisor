"""Private, bounded browser archive staging and streaming downloads."""

import asyncio
import os
import re
import shutil
import stat
import time
from pathlib import Path
from uuid import uuid4

from .checkpoint_files import read_json, sync_directory, write_json
from .config import Config
from .transfer import CHUNK

TTL = 24 * 3600
MAX_TRANSFERS = 20


class UploadError(ValueError):
    """A browser upload failed validation or exceeded a resource limit."""


def folder(config: Config) -> Path:
    path = config.state_dir / "web-transfers"
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Transfer directory cannot be a symlink")
    return path


def archive(config: Config, transfer_id: str) -> Path:
    if not isinstance(transfer_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", transfer_id
    ):
        raise ValueError("Invalid transfer ID")
    return folder(config) / (transfer_id + ".bsmcheckpoint")


def publish(config: Config, transfer_id: str, direction: str) -> dict:
    path = archive(config, transfer_id)
    report = {
        "id": transfer_id,
        "direction": direction,
        "bytes": path.stat().st_size,
        "created_at": time.time(),
        "expires_at": time.time() + TTL,
    }
    write_json(path.with_suffix(".json"), report)
    return report


def resolve(config: Config, transfer_id: str, direction: str) -> Path:
    path = archive(config, transfer_id)
    report = read_json(path.with_suffix(".json"))
    info = path.lstat()
    if (
        report.get("direction") != direction
        or report.get("expires_at", 0) <= time.time()
        or not stat.S_ISREG(info.st_mode)
    ):
        raise ValueError("Transfer is unavailable or expired")
    return path


def listing(config: Config) -> list[dict]:
    return sorted(
        (
            read_json(path)
            for path in folder(config).glob("*.json")
            if re.fullmatch(r"[0-9a-f]{32}.json", path.name)
        ),
        key=lambda item: item["created_at"],
        reverse=True,
    )


def remove(config: Config, transfer_id: str) -> None:
    path = archive(config, transfer_id)
    path.unlink(missing_ok=True)
    path.with_suffix(".json").unlink(missing_ok=True)
    sync_directory(path.parent)


async def upload(config: Config, headers: dict, receive) -> dict:
    """Receive an archive incrementally; no filename or path comes from the client."""
    if headers.get("content-type") != "application/octet-stream":
        raise UploadError("Upload a checkpoint archive as application/octet-stream")
    length = headers.get("content-length")
    if length is not None and (
        not length.isascii()
        or not length.isdecimal()
        or int(length) > config.web_transfer_limit
    ):
        raise UploadError("Upload exceeds the configured byte limit")
    base = folder(config)
    for path in base.iterdir():
        if (
            path.is_file()
            and not path.is_symlink()
            and time.time() - path.stat().st_mtime > TTL
        ):
            path.unlink()
    used = sum(path.stat().st_size for path in base.glob("*.bsmcheckpoint"))
    if (
        len(list(base.glob("*.bsmcheckpoint"))) >= MAX_TRANSFERS
        or used >= config.web_transfer_limit * 2
    ):
        raise UploadError("Delete staged transfers before uploading more archives")
    transfer_id = uuid4().hex
    path = archive(config, transfer_id)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    count = 0
    deadline = time.monotonic() + 900
    try:
        with os.fdopen(descriptor, "wb") as stream:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UploadError("Upload deadline exceeded")
                message = await asyncio.wait_for(receive(), min(30, remaining))
                if message["type"] == "http.disconnect":
                    raise UploadError("Upload disconnected")
                block = message.get("body", b"")
                count += len(block)
                if (
                    count > config.web_transfer_limit
                    or used + count > config.web_transfer_limit * 2
                ):
                    raise UploadError("Upload exceeds the configured byte limit")
                if shutil.disk_usage(base).free < len(block) + 64 * 1024**2:
                    raise UploadError("Insufficient free space for upload")
                await asyncio.to_thread(stream.write, block)
                if not message.get("more_body"):
                    break
            if not count or (length is not None and count != int(length)):
                raise UploadError("Empty or incomplete archive upload")
            stream.flush()
            os.fsync(stream.fileno())
        return publish(config, transfer_id, "upload")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def download(config: Config, transfer_id: str, send, security: list) -> None:
    path = resolve(config, transfer_id, "download")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    *security,
                    (b"content-type", b"application/octet-stream"),
                    (
                        b"content-length",
                        str(os.fstat(stream.fileno()).st_size).encode(),
                    ),
                    (
                        b"content-disposition",
                        (
                            'attachment; filename="' + transfer_id + '.bsmcheckpoint"'
                        ).encode(),
                    ),
                ],
            }
        )
        while block := await asyncio.to_thread(stream.read, CHUNK):
            await send({"type": "http.response.body", "body": block, "more_body": True})
        await send({"type": "http.response.body", "body": b""})
