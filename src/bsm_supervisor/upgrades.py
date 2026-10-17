"""Durable, serialized BSM upgrades with explicit checkpoint-based recovery."""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from packaging.version import Version

from . import checkpoints, restoration, runtime
from .activation import activate, selection
from .checkpoint_files import read_json, sync_directory, write_json
from .config import Config
from .install import install, run_command
from .integration import inspect, require_managed
from .launchguard import boot_identity, controller_pid, permit_path, process_generation
from .releases import Release, discover, select
from .service import SystemdService
from .store import Store


def _path(config: Config, workflow_id: int) -> Path:
    if workflow_id < 1:
        raise ValueError("Invalid upgrade ID")
    return config.state_dir / f"upgrade-{workflow_id}.json"


def status(config: Config, workflow_id: int) -> dict:
    """Read an upgrade without returning runtime credentials."""
    plan = read_json(_path(config, workflow_id))
    expected = {
        "config_dir": str(config.config_dir.resolve()),
        "data_dir": str(config.data_dir.resolve()),
        "installations_dir": str(config.installations_dir.resolve()),
    }
    if plan.get("id") != workflow_id or plan.get("scope") != expected:
        raise ValueError("Upgrade scope differs from current configuration")
    return plan


def _save(config: Config, plan: dict, phase: str | None = None) -> None:
    if phase is not None:
        plan["phase"] = phase
    write_json(_path(config, plan["id"]), plan)


def _public_json(path: Path, value: dict) -> None:
    # Contains only workflow/process identities; the BSM service guard must read it.
    write_json(path, value)
    path.chmod(0o644)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _mark(config: Config, workflow_id: int, workflow_key: str) -> None:
    marker = config.config_dir / "supervisor-upgrade.required"
    if marker.exists() and (
        read_json(marker).get("workflow_id") != workflow_id
        or read_json(marker).get("workflow_key") != workflow_key
    ):
        raise ValueError("A different upgrade owns the startup block")
    _public_json(marker, {"workflow_id": workflow_id, "workflow_key": workflow_key})


def _unmark(config: Config, workflow_id: int, workflow_key: str) -> None:
    marker = config.config_dir / "supervisor-upgrade.required"
    if marker.exists():
        if (
            read_json(marker).get("workflow_id") != workflow_id
            or read_json(marker).get("workflow_key") != workflow_key
        ):
            raise ValueError("A different upgrade owns the startup block")
        marker.unlink()
        sync_directory(marker.parent)


def _runtime_directory(config: Config) -> Path:
    path = permit_path(config.config_dir)
    base = path.parent
    if not base.exists():
        base.mkdir(mode=0o755)
        base.chmod(0o755)
    if (
        base.is_symlink()
        or base.stat().st_uid != os.geteuid()
        or base.stat().st_mode & 0o022
    ):
        raise ValueError(
            "Upgrade runtime directory must be owned and protected by the operator"
        )
    return path


@contextmanager
def _trial(config: Config, workflow_id: int, workflow_key: str) -> Iterator[None]:
    """Authorize startup only during this live controller generation and boot."""
    path = _runtime_directory(config)
    base = path.parent
    _public_json(
        path,
        {
            "workflow_id": workflow_id,
            "workflow_key": workflow_key,
            "pid": controller_pid(),
            "generation": process_generation(controller_pid()),
            "boot_id": boot_identity(),
        },
    )
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        sync_directory(base)


def _verify_environment(config: Config, item: dict) -> None:
    target = config.installations_dir.resolve() / item["id"]
    if target.resolve(strict=True) != target or str(target) != item["path"]:
        raise ValueError("Upgrade environment path differs from its registry")
    log = config.state_dir / "logs"
    log.mkdir(mode=0o700, exist_ok=True)
    code = (
        "from importlib.metadata import version; "
        f"assert version('bedrock-server-manager') == {item['version']!r}"
    )
    run_command(
        [str(target / "bin/python"), "-I", "-c", code],
        log / ("verify-" + item["id"] + ".log"),
    )


def _selected(config: Config, store: Store, plan: dict) -> dict:
    current = selection(config, store)
    if current is None or current["id"] not in (
        plan["original"]["id"],
        plan["target_id"],
    ):
        raise ValueError("Selected installation changed outside this upgrade")
    return current


def _quiesce(config: Config) -> None:
    service = SystemdService(config)
    state = service.status()
    require_managed(config, state)
    if state.get("ActiveState") == "failed" and state.get("MainPID") == "0":
        service.reset_failed()
    runtime.shutdown(config)


def _stop(config: Config, store: Store) -> None:
    with store.mutation("upgrade:graceful-stop", preserve_on_error=True):
        _quiesce(config)


def _start(config: Config, store: Store, plan: dict, version: str) -> dict:
    with _trial(config, plan["id"], plan["key"]):
        with store.mutation("upgrade:start", preserve_on_error=True):
            SystemdService(config).control("start")
            return runtime.wait_ready(config, version)


def _target(config: Config, store: Store, plan: dict) -> dict:
    target = next(
        (item for item in store.installations() if item["id"] == plan["target_id"]),
        None,
    )
    if target is None:
        install(config, store, Release(plan["target_version"]))
        target = next(
            item for item in store.installations() if item["id"] == plan["target_id"]
        )
    _verify_environment(config, target)
    return target


def _complete(config: Config, store: Store, plan: dict, result: str) -> dict:
    _save(config, plan, result)
    _unmark(config, plan["id"], plan["key"])
    store.finish_workflow(
        plan["id"], "succeeded" if result == "completed" else "failed", result
    )
    return plan


def _continue(config: Config, store: Store, plan: dict) -> dict:
    target = _target(config, store, plan)
    _selected(config, store, plan)
    _verify_environment(config, plan["original"])
    _mark(config, plan["id"], plan["key"])
    _save(config, plan, "stopping")
    _stop(config, store)
    if plan.get("checkpoint_id") is None:
        if plan["ever_started"]:
            raise ValueError("Started upgrade has no recoverable checkpoint")
        _save(config, plan, "checkpointing")
        snapshot = checkpoints.create(config, store)
        plan["checkpoint_id"] = snapshot["id"]
        _save(config, plan, "checkpointed")
    snapshot = checkpoints.verify(config, plan["checkpoint_id"])
    if (snapshot.get("installation") or {}).get("id") != plan["original"]["id"]:
        raise ValueError("Upgrade checkpoint belongs to a different installation")
    checkpoints.pin(config, store, plan["checkpoint_id"], True)
    _save(config, plan, "activating")
    activate(config, store, target["id"])
    plan["ever_started"] = True
    _save(config, plan, "starting")
    plan["readiness"] = _start(config, store, plan, target["version"])
    return _complete(config, store, plan, "completed")


def _failure(config: Config, store: Store, plan: dict, error: Exception) -> None:
    # Runtime clients and command runners emit sanitized errors only.
    plan["last_error"] = str(error)
    _save(config, plan)
    if plan.get("ever_started"):
        try:
            _quiesce(config)
        except Exception:
            plan["shutdown_required"] = True
            _save(config, plan)
    # Keep marker, checkpoint, and workflow unresolved. Data restoration is explicit.


def upgrade(
    config: Config, store: Store, selector: str, *, allow_same: bool = False
) -> dict:
    """Prepare a release, checkpoint stopped data, and require healthy startup."""
    release = select(discover(), selector)
    original = selection(config, store)
    if original is None:
        raise ValueError("A registered selected installation is required")
    current_version = Version(original["version"])
    target_version = Version(release.version)
    if current_version < Version("4.0.0b1"):
        raise ValueError("Coordinated upgrades require the audited BSM 4.0 runtime")
    if target_version < current_version or (
        target_version == current_version and not allow_same
    ):
        raise ValueError(
            "Choose a newer release or explicitly allow same-version repair"
        )
    runtime.bearer(config)
    _runtime_directory(config)
    state = SystemdService(config).status()
    require_managed(config, state)
    if state.get("ActiveState") not in ("active", "inactive"):
        raise ValueError("Upgrade requires an active or cleanly inactive service")
    with store.workflow() as workflow_id:
        plan = {
            "id": workflow_id,
            "key": uuid4().hex,
            "phase": "preparing",
            "original": original,
            "target_id": "release-" + release.version,
            "target_version": release.version,
            "checkpoint_id": None,
            "was_running": state["ActiveState"] == "active",
            "ever_started": False,
            "scope": {
                "config_dir": str(config.config_dir.resolve()),
                "data_dir": str(config.data_dir.resolve()),
                "installations_dir": str(config.installations_dir.resolve()),
            },
        }
        _save(config, plan)
        try:
            if plan["was_running"]:
                runtime.ready(config, original["version"])
            report = inspect(config)
            if report["blockers"]:
                raise ValueError("; ".join(report["blockers"]))
            return _continue(config, store, plan)
        except Exception as error:
            _failure(config, store, plan, error)
            raise


def _settle_steps(config: Config, store: Store, plan: dict) -> None:
    for step in store.workflow_operations(plan["id"]):
        if step["state"] != "running":
            continue
        if step["action"].startswith("checkpoint:restore:"):
            _quiesce(config)
            restoration.recover(config, store, step["id"])
        else:
            store.finish(
                step["id"], "failed", "Interrupted step reconciled by upgrade recovery"
            )
    # A checkpoint may have been published before the controller recorded its ID.
    if plan.get("checkpoint_id") is None:
        captures = {
            step["id"]
            for step in store.workflow_operations(plan["id"])
            if step["action"] == "checkpoint:create"
        }
        for item in checkpoints.list_checkpoints(config):
            if item.get("invalid"):
                continue
            saved = checkpoints.load(config, item["id"])
            if saved.get("operation_id") in captures:
                plan["checkpoint_id"] = saved["id"]
                _save(config, plan)
    # Retain committed restore originals; discard only staging after a reversed attempt.
    for step in store.workflow_operations(plan["id"]):
        if step["action"].startswith("checkpoint:restore:"):
            path = config.state_dir / f"restore-{step['id']}.json"
            if path.exists():
                transaction = read_json(path)
                if transaction["phase"] == "committed":
                    plan["restore_operation_id"] = step["id"]
                    _save(config, plan)
                elif transaction["phase"] == "rolled_back":
                    restoration.cleanup(config, store, step["id"])


def recover(config: Config, store: Store, workflow_id: int, action: str) -> dict:
    """Resume a target or explicitly restore a checkpoint and the original version."""
    if action not in ("resume", "restore", "cancel"):
        raise ValueError("Unknown upgrade recovery action")
    runtime.bearer(config)
    with store.workflow(workflow_id):
        if not _path(config, workflow_id).exists():
            if (
                store.workflow_operations(workflow_id)
                or (config.config_dir / "supervisor-upgrade.required").exists()
            ):
                raise ValueError("Upgrade journal is missing; inspect before recovery")
            store.finish_workflow(workflow_id, "failed", "Cancelled before preparation")
            return {"id": workflow_id, "phase": "cancelled"}
        plan = status(config, workflow_id)
        try:
            _settle_steps(config, store, plan)
            _selected(config, store, plan)
            if action == "cancel":
                if plan["ever_started"]:
                    raise ValueError(
                        "Started upgrades require resume or explicit data restoration"
                    )
                # No new-version startup was attempted; selecting the original is safe.
                state = SystemdService(config).status()
                if state.get("ActiveState") == "active":
                    runtime.ready(config, plan["original"]["version"])
                else:
                    _mark(config, workflow_id, plan["key"])
                    _stop(config, store)
                    activate(config, store, plan["original"]["id"])
                    if plan["was_running"]:
                        plan["readiness"] = _start(
                            config, store, plan, plan["original"]["version"]
                        )
                return _complete(config, store, plan, "cancelled")
            if action == "resume":
                if plan.get("restore_operation_id"):
                    raise ValueError(
                        "Data rollback has begun; finish with action restore"
                    )
                return _continue(config, store, plan)
            if plan.get("checkpoint_id") is None:
                raise ValueError("Upgrade has no checkpoint to restore")
            _mark(config, workflow_id, plan["key"])
            _stop(config, store)
            _verify_environment(config, plan["original"])
            checkpoints.verify(config, plan["checkpoint_id"])
            if plan.get("restore_operation_id") is None:
                _save(config, plan, "restoring")
                result = restoration.restore(config, store, plan["checkpoint_id"])
                plan["restore_operation_id"] = result["operation_id"]
                _save(config, plan, "restored")
            activate(config, store, plan["original"]["id"])
            plan["readiness"] = _start(config, store, plan, plan["original"]["version"])
            return _complete(config, store, plan, "rolled_back")
        except Exception as error:
            _failure(config, store, plan, error)
            raise
