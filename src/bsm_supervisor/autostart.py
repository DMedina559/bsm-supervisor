"""Recovery-aware, one-shot BSM startup after the console listener becomes ready."""

from . import operations
from .activation import selection
from .checkpoint_files import read_json
from .config import Config
from .integration import require_managed
from .self_update import update_lock
from .service import SystemdService
from .store import Store


def eligible(config: Config, store: Store) -> dict:
    """Inspect startup eligibility while the caller holds update and store locks."""
    store.require_idle()
    if selection(config, store) is None:
        return {"state": "skipped", "reason": "No BSM installation selected; run setup"}
    if any(
        (config.config_dir / name).exists() or (config.config_dir / name).is_symlink()
        for name in ("supervisor-recovery.required", "supervisor-upgrade.required")
    ):
        return {
            "state": "blocked",
            "reason": "BSM recovery requires operator attention",
        }
    plan = config.state_dir / "supervisor-update.json"
    if plan.exists() and read_json(plan).get("phase") != "completed":
        return {"state": "blocked", "reason": "Supervisor update requires recovery"}
    state = SystemdService(config).status()
    require_managed(config, state)
    if state.get("Job") or state.get("ActiveState") not in (
        "inactive",
        "failed",
        "active",
    ):
        return {"state": "skipped", "reason": "BSM service is transitioning"}
    if state.get("ActiveState") == "active" and state.get("MainPID") not in (
        None,
        "",
        "0",
    ):
        return {"state": "running", "reason": "BSM is already running"}
    if state.get("MainPID") != "0" or state.get("ActiveState") == "active":
        return {
            "state": "blocked",
            "reason": "BSM service process state needs inspection",
        }
    return {"state": "eligible"}


def start(config: Config, store: Store) -> dict:
    """Recheck all guards and start BSM under one serialized CLI operation."""
    if not config.web_auto_start:
        return {"state": "disabled", "reason": "Automatic BSM startup is disabled"}
    try:
        with update_lock(config), store.lock():
            report = eligible(config, store)
            if report["state"] != "eligible":
                return report
            operations.control(config, store, "start")
            return {
                "state": "started",
                "reason": "BSM process started; use health to verify readiness",
            }
    except Exception:
        return {
            "state": "blocked",
            "reason": "Startup blocked; inspect status, history and doctor",
        }


def attempt(console) -> dict:
    """Queue one start, leaving final eligibility checks to the serialized child."""
    config = console.config
    if not config.web_auto_start:
        return {"state": "disabled", "reason": "Automatic BSM startup is disabled"}
    try:
        store = Store(config.state_dir)
        with update_lock(config), store.lock():
            report = eligible(config, store)
        if report["state"] != "eligible":
            return report
        job = console.submit({"action": "auto-start"})
        return {
            "state": "queued",
            "job_id": job["id"],
            "reason": "Starting BSM after console readiness",
        }
    except Exception:
        return {
            "state": "blocked",
            "reason": "Startup blocked; inspect status, history and doctor",
        }
