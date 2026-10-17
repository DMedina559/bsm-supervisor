"""Portable verified checkpoints with optional streaming authenticated encryption."""

import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import cast

from . import checkpoints
from .checkpoint_files import (
    MAX_ENTRIES,
    MAX_MANIFEST_BYTES,
    sync_directory,
    sync_payload,
    validate_entries,
)
from .config import Config
from .store import Store

MAGIC = b"BSMENC01"
CHUNK = 1024 * 1024
DEFAULT_LIMIT = 100 * 1024**3


def private_secret(path: Path) -> bytes:
    """Read a bounded private regular credential without following a symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.geteuid()
            or not 16 <= info.st_size <= 4096
        ):
            raise ValueError(
                "Credential must be owned by this user, mode 0600, 16–4096 bytes"
            )
        value = stream.read().rstrip(b"\r\n")
        if len(value) < 16:
            raise ValueError("Credential must contain at least 16 bytes")
        return value


def _crypt(source: Path, target: Path, password: bytes, decrypt: bool) -> None:
    """Authenticate the entire stream before decrypted data is consumed."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers import (
            AEADDecryptionContext,
            AEADEncryptionContext,
            Cipher,
            algorithms,
            modes,
        )
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise ValueError("Encryption requires the transfer extra") from exc
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        if decrypt:
            header = incoming.read(36)
            if len(header) != 36 or not header.startswith(MAGIC):
                raise ValueError("Invalid encrypted checkpoint header")
            size = source.stat().st_size - 36 - 16
            if size < 0:
                raise ValueError("Truncated encrypted checkpoint")
            salt, nonce = header[8:24], header[24:36]
        else:
            salt, nonce = os.urandom(16), os.urandom(12)
            header = MAGIC + salt + nonce
            size = source.stat().st_size
            outgoing.write(header)
        key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password)
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce))
        context = cipher.decryptor() if decrypt else cipher.encryptor()
        context.authenticate_additional_data(header)
        while size:
            block = incoming.read(min(CHUNK, size))
            if not block:
                raise ValueError("Truncated checkpoint archive")
            outgoing.write(context.update(block))
            size -= len(block)
        try:
            if decrypt:
                outgoing.write(
                    cast(AEADDecryptionContext, context).finalize_with_tag(
                        incoming.read(16)
                    )
                )
            else:
                outgoing.write(context.finalize())
                outgoing.write(cast(AEADEncryptionContext, context).tag)
        except InvalidTag as exc:
            raise ValueError("Checkpoint authentication failed") from exc
        outgoing.flush()
        os.fsync(outgoing.fileno())


def export_checkpoint(
    config: Config,
    store: Store,
    checkpoint_id: str,
    destination: Path,
    passphrase_file: Path | None = None,
) -> dict:
    """Export an immutable checkpoint; never overwrite an existing destination."""
    if not destination.is_absolute():
        raise ValueError("Export destination must be absolute")
    password = private_secret(passphrase_file) if passphrase_file else None
    with store.mutation("checkpoint:export:" + checkpoint_id):
        manifest = checkpoints.verify(config, checkpoint_id)
        root = checkpoints.checkpoint_path(config, checkpoint_id)
        with tempfile.TemporaryDirectory(
            prefix=".bsm-export-", dir=destination.parent
        ) as tmp:
            staging = Path(tmp)
            archive = staging / "checkpoint.zip"
            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_STORED
            ) as bundle:
                bundle.writestr("manifest.json", json.dumps(manifest))
                for record in manifest["roots"]:
                    for item in record["entries"]:
                        if item["kind"] == "file":
                            name = "payload/" + record["id"] + "/" + item["name"]
                            bundle.write(root / name, name)
            archive.chmod(0o600)
            if password is not None:
                encrypted = staging / "checkpoint.enc"
                _crypt(archive, encrypted, password, False)
                archive = encrypted
            archive.chmod(0o600)
            with archive.open("rb") as stream:
                os.fsync(stream.fileno())
            # Hard link publication is atomic and refuses files and dangling links.
            os.link(archive, destination)
            sync_directory(destination.parent)
        return {
            "id": checkpoint_id,
            "path": str(destination),
            "encrypted": password is not None,
        }


def import_checkpoint(
    config: Config,
    store: Store,
    source: Path,
    passphrase_file: Path | None = None,
    max_bytes: int = DEFAULT_LIMIT,
) -> dict:
    """Stage, authenticate, check scope and hashes, then publish without restoring."""
    if max_bytes < 1 or not source.is_absolute() or source.is_symlink():
        raise ValueError(
            "Import needs an absolute regular archive and positive byte limit"
        )
    if (
        not source.is_file()
        or source.stat().st_size > max_bytes + MAX_MANIFEST_BYTES + 1024**2
    ):
        raise ValueError("Archive exceeds the configured import limit")
    password = private_secret(passphrase_file) if passphrase_file else None
    base = config.state_dir / "checkpoints"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (
        store.mutation("checkpoint:import"),
        tempfile.TemporaryDirectory(prefix=".import-", dir=base) as tmp,
    ):
        staging = Path(tmp)
        archive = source
        with source.open("rb") as stream:
            encrypted = stream.read(8) == MAGIC
        if encrypted:
            if password is None:
                raise ValueError("Encrypted archive requires --passphrase-file")
            archive = staging / "decrypted.zip"
            _crypt(source, archive, password, True)
        elif password is not None:
            raise ValueError("Passphrase supplied for an unencrypted archive")
        sandbox = replace(config, state_dir=staging)
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ENTRIES + 1 or len(
                {m.filename for m in members}
            ) != len(members):
                raise ValueError("Duplicate or excessive archive members")
            meta = bundle.getinfo("manifest.json")
            if meta.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("Manifest exceeds its size limit")
            manifest = json.loads(bundle.read(meta))
            if not isinstance(manifest, dict):
                raise ValueError("Invalid checkpoint manifest")
            checkpoint_id = manifest.get("id", "")
            if not isinstance(checkpoint_id, str):
                raise ValueError("Invalid checkpoint ID")
            candidate = checkpoints.checkpoint_path(sandbox, checkpoint_id)
            candidate.mkdir(mode=0o700, parents=True)
            (candidate / "manifest.json").write_text(json.dumps(manifest))
            checkpoints.load(sandbox, checkpoint_id)
            expected = {"manifest.json"}
            total = 0
            for record in manifest["roots"]:
                validate_entries(record.get("entries"))
                payload = candidate / "payload" / record["id"]
                payload.mkdir(parents=True)
                for item in record["entries"]:
                    path = payload / item["name"]
                    if item["kind"] == "directory":
                        path.mkdir(exist_ok=True)
                    elif item["kind"] == "symlink":
                        # Children under symlinks are rejected by validate_entries.
                        path.symlink_to(item["target"])
                    else:
                        name = "payload/" + record["id"] + "/" + item["name"]
                        expected.add(name)
                        info = bundle.getinfo(name)
                        total += info.file_size
                        if info.file_size != item["size"] or total > max_bytes:
                            raise ValueError(
                                "Archive payload exceeds its declared or allowed size"
                            )
                        with bundle.open(info) as incoming, path.open("xb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing, CHUNK)
            if expected != {m.filename for m in members}:
                raise ValueError("Unexpected archive members")
        checkpoints.verify(sandbox, checkpoint_id)
        final = checkpoints.checkpoint_path(config, checkpoint_id)
        if final.exists():
            raise ValueError("Checkpoint already exists; imports never overwrite")

        for record in manifest["roots"]:
            sync_payload(candidate / "payload" / record["id"], record["entries"])
        with (candidate / "manifest.json").open("rb") as stream:
            os.fsync(stream.fileno())
        sync_directory(candidate / "payload")
        sync_directory(candidate)
        os.rename(candidate, final)
        sync_directory(staging / "checkpoints")
        sync_directory(base)
        return {"id": checkpoint_id, "total_bytes": total, "restored": False}
