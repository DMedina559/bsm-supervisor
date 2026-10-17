"""Inspect recovery state and acknowledge only verified inactive controllers."""

import fcntl
import hashlib
import json
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from . import self_update
from .checkpoint_files import read_json, sync_directory
from .config import Config
from .launchguard import boot_identity, process_generation
from .service import service_backend
from .store import BusyError, Store


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def job_active(config: Config, job: dict) -> bool:
    """Probe a lease and child identity without trusting journal state."""
    path = config.state_dir / "web-jobs" / (job["id"] + ".lock")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        descriptor = None
    if descriptor is not None:
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        finally:
            os.close(descriptor)
    try:
        return bool(
            job.get("boot_id") == boot_identity()
            and job.get("child_pid")
            and job.get("child_generation")
            and process_generation(job["child_pid"]) == job["child_generation"]
        )
    except (OSError, ValueError, IndexError):
        return False


@contextmanager
def idle_jobs(config: Config):
    """Hold all pending job leases while the caller checks the operation lock."""
    with ExitStack() as stack:
        for path in (config.state_dir / "web-jobs").glob("*.json"):
            job = read_json(path)
            if job.get("state") not in ("running", "interrupted"):
                continue
            if job_active(config, job):
                raise BusyError("A console child process is still executing")
            descriptor = os.open(
                path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            stack.callback(os.close, descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BusyError("A console job is still executing") from error
        yield


def service_state(config: Config) -> dict:
    state = service_backend(config).status()
    return {
        key: state.get(key)
        for key in ("ActiveState", "SubState", "MainPID", "Job", "Result")
    }


def inspect_operation(config: Config, store: Store, operation_id: int) -> dict:
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown operation")
        events = [
            dict(item)
            for item in db.execute(
                "SELECT * FROM events WHERE operation_id = ? "
                "ORDER BY id DESC LIMIT 500",
                (operation_id,),
            )
        ]
    value = {"operation": dict(row), "service": service_state(config)}
    return {
        **value,
        "digest": digest(value),
        "events": events,
        "event_limit": 500,
        "effect": (
            "Acknowledgement clears interruption bookkeeping; "
            "it does not restore data or restart BSM."
        ),
    }


def acknowledge_operation(
    config: Config, store: Store, operation_id: int, expected: str
) -> None:
    with idle_jobs(config), store.lock():
        report = inspect_operation(config, store, operation_id)
        if report["digest"] != expected:
            raise ValueError("Operation or service state changed; inspect again")
        state = report["service"]
        if state["ActiveState"] not in ("active", "inactive", "failed") or state["Job"]:
            raise BusyError("Wait for the managed service to finish transitioning")
        if state["ActiveState"] in ("inactive", "failed") and state["MainPID"] != "0":
            raise BusyError("Stopped service still has a process; inspect it first")
        store.acknowledge(operation_id)


def inspect_update(config: Config, config_path: Path) -> dict:
    path = config.state_dir / "supervisor-update.json"
    if not path.exists():
        return {"pending": False}
    plan = read_json(path)
    fields = (
        "phase",
        "ref",
        "revision",
        "previous_environment",
        "environment",
        "was_running",
        "package_version",
    )
    value: dict = {
        "plan": {key: plan.get(key) for key in fields},
        "service": service_state(config),
    }
    scope = [
        str(config.data_dir),
        str(config.config_dir),
        str(config.installations_dir),
        config.service_unit,
    ]
    matching = plan.get("scope") == scope and plan.get("config") == str(
        config_path.resolve()
    )
    try:
        previous = str(self_update.installed_environment()) == plan.get(
            "previous_environment"
        )
    except (OSError, self_update.UpdateError):
        previous = False
    cancellable = matching and previous and plan.get("phase") == "preparing"
    value["matching_scope"] = matching
    return {
        **value,
        "pending": plan.get("phase") != "completed",
        "digest": digest(value),
        "cancellable": cancellable,
        "effect": (
            "Only an uninstalled preparing update can be cancelled. "
            "Installed updates must be resumed."
        ),
    }


def cancel_update(
    config: Config, store: Store, expected: str, config_path: Path
) -> None:
    with self_update.update_lock(config), idle_jobs(config), store.lock():
        store.require_idle()
        report = inspect_update(config, config_path)
        if report.get("digest") != expected or not report.get("cancellable"):
            raise ValueError(
                "Update changed or installation has begun; inspect and resume it"
            )
        if report["service"]["Job"] or report["service"]["ActiveState"] not in (
            "active",
            "inactive",
            "failed",
        ):
            raise BusyError("Wait for the managed service to finish transitioning")
        source = config.state_dir / "supervisor-update.json"
        source.rename(
            config.state_dir / ("supervisor-update-cancelled-" + uuid4().hex + ".json")
        )
        sync_directory(config.state_dir)
