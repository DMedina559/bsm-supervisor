"""Typed console administration through the supervisor's shared operations."""

import json
import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from packaging.version import Version

from . import (
    checkpoints,
    console_transfers,
    diagnostics,
    events,
    operations,
    restoration,
    retention,
    runtime,
    scheduling,
    self_update,
    transfer,
)
from .activation import selection
from .checkpoint_files import read_json, write_json
from .config import Config
from .environment import overrides
from .integration import require_managed, require_offline
from .service import SystemdService
from .store import Store
from .unit_refresh import publish, refresh

SETTINGS = {
    "bsm_host",
    "bsm_port",
    "bsm_log_level",
    "bsm_db_url",
    "readiness_timeout",
    "service_timeout",
    "web_job_timeout",
    "web_auto_start",
    "web_transfer_limit",
}
RUNTIME_SETTINGS = {"bsm_host", "bsm_port", "bsm_log_level", "bsm_db_url"}
SCHEMAS = {
    "backup-create": set(),
    "backup-restore": {"target", "digest", "restart_after"},
    "checkpoint-export": {"target", "passphrase"},
    "checkpoint-import": {"target", "passphrase"},
    "checkpoint-delete": {"target"},
    "checkpoint-pin": {"target", "enabled"},
    "checkpoint-prune": {"keep"},
    "transfer-delete": {"target"},
    "journal-prune": {"keep_days", "keep_jobs"},
    "settings-save": {"settings"},
    "schedule-save": {"policy"},
    "schedule-remove": {"target"},
    "schedule-run": {"target"},
    "schedule-acknowledge": {"target"},
    "notifications-save": {"url"},
    "notifications-disable": set(),
    "notifications-deliver": set(),
    "supervisor-check": set(),
    "supervisor-update": {"ref", "resume", "github_token"},
    "console-restart": set(),
    "health": set(),
    "doctor": set(),
    "unit-refresh": set(),
    "service-logs": set(),
}
READ_ONLY = {"supervisor-check", "health", "doctor", "service-logs"}


def validate(value: dict) -> dict:
    """Reject unknown fields and malformed values before a job is persisted."""
    action = value.get("action")
    if (
        not isinstance(action, str)
        or action not in SCHEMAS
        or value.keys() - (SCHEMAS[action] | {"action", "confirmed"})
    ):
        raise ValueError("Invalid administration action fields")
    if action not in READ_ONLY and value.get("confirmed") is not True:
        raise ValueError("Explicit confirmation is required")
    for key in ("confirmed", "restart_after", "enabled", "resume"):
        if key in value and type(value[key]) is not bool:
            raise ValueError("Confirmation fields must be booleans")
    if "target" in SCHEMAS[action]:
        pattern = (
            r"[a-z][a-z0-9-]{0,39}"
            if action.startswith("schedule-")
            else r"[0-9a-f]{32}"
        )
        if not isinstance(value.get("target"), str) or not re.fullmatch(
            pattern, value["target"]
        ):
            raise ValueError("Select a valid checkpoint, transfer or schedule")
    for key in ("keep", "keep_days", "keep_jobs"):
        if key in SCHEMAS[action] and (
            type(value.get(key)) is not int or not 1 <= value[key] <= 100000
        ):
            raise ValueError("Retention values must be positive integers up to 100000")
    if "passphrase" in value and (
        not isinstance(value["passphrase"], str)
        or not 16 <= len(value["passphrase"].encode()) <= 4096
        or "\x00" in value["passphrase"]
    ):
        raise ValueError("Encryption passwords must contain 16–4096 bytes")
    if action == "backup-restore" and not re.fullmatch(
        r"[0-9a-f]{64}", str(value.get("digest", ""))
    ):
        raise ValueError("Inspect this checkpoint before confirming restoration")
    if action == "schedule-save":
        if not isinstance(value.get("policy"), dict):
            raise ValueError("Schedule policy must be an object")
        scheduling.policy(value["policy"])
    if action == "settings-save" and (
        not isinstance(value.get("settings"), dict)
        or not value["settings"]
        or value["settings"].keys() - SETTINGS
    ):
        raise ValueError("Choose supported supervisor settings")
    if action == "notifications-save":
        if not isinstance(value.get("url"), str) or len(value["url"]) > 2048:
            raise ValueError("Enter a bounded HTTPS notification URL")
    if action == "supervisor-update":
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", str(value.get("ref", "latest"))
        ) or ".." in value.get("ref", "latest"):
            raise ValueError("Choose a valid supervisor release tag, branch or commit")
        if "github_token" in value and (
            not isinstance(value["github_token"], str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,256}", value["github_token"])
        ):
            raise ValueError("Invalid GitHub token format")
    return value


def checkpoint_summary(config: Config, checkpoint_id: str) -> dict:
    manifest = checkpoints.verify(config, checkpoint_id)
    current = selection(config, Store(config.state_dir))
    saved = manifest.get("installation") or {}
    compatible = bool(
        current
        and saved.get("version")
        and Version(current["version"]) == Version(saved["version"])
        and (
            not any(
                str(item.get("id", "")).startswith("git-") for item in (current, saved)
            )
            or current["id"] == saved["id"]
        )
    )
    return {
        "id": checkpoint_id,
        "created_at": manifest["created_at"],
        "total_bytes": manifest["total_bytes"],
        "database_path": manifest.get("database_path"),
        "schema_revision": manifest.get("schema_revision"),
        "installation": saved,
        "roots": [
            {"path": item["path"], "entries": len(item["entries"])}
            for item in manifest["roots"]
        ],
        "digest": sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        "compatible_version": compatible,
        "scope": "Full BSM data: database, configuration and all configured roots",
        "effect": (
            "Replace saved contents and discard newer data. "
            "Version selection is unchanged."
        ),
    }


def restore_backup(config: Config, store: Store, value: dict) -> dict:
    with store.lock():
        store.require_idle()
        plan = checkpoint_summary(config, value["target"])
        if plan["digest"] != value["digest"] or not plan["compatible_version"]:
            raise ValueError(
                "Checkpoint changed or BSM version differs; inspect and select "
                "the matching version before restoring"
            )
        state = SystemdService(config).status()
        require_managed(config, state)
        if state.get("ActiveState") == "active":
            with store.mutation("service:restore-stop", preserve_on_error=True):
                runtime.shutdown(config)
        require_offline(config)
        safety = checkpoints.create(config, store)
        checkpoints.pin(config, store, safety["id"], True)
        result = restoration.restore(config, store, value["target"])
        result["recovery_checkpoint"] = safety["id"]
        if value.get("restart_after"):
            operations.control(config, store, "start")
        return result


def delete_checkpoint(config: Config, store: Store, checkpoint_id: str) -> dict:
    import shutil

    with store.mutation("checkpoint:delete:" + checkpoint_id):
        checkpoints.verify(config, checkpoint_id)
        items = sorted(
            checkpoints.list_checkpoints(config),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )
        protected = {
            item["id"] for item in items if item.get("pinned") or item.get("invalid")
        }
        seen = set()
        installed = {item["id"] for item in store.installations()}
        for item in items:
            owner = (item.get("installation") or {}).get("id")
            if owner in installed and owner not in seen:
                protected.add(item["id"])
                seen.add(owner)
        for path in config.state_dir.glob("restore-*.json"):
            protected.add(read_json(path).get("checkpoint_id"))
        if checkpoint_id in protected:
            raise ValueError(
                "Checkpoint is pinned, retained for recovery, or the latest "
                "checkpoint for an installed version"
            )
        shutil.rmtree(checkpoints.checkpoint_path(config, checkpoint_id))
        return {"deleted": checkpoint_id}


def settings(config: Config) -> dict:
    environment = overrides()
    names = {
        "HOST": "bsm_host",
        "BSM_SUPERVISOR_WEB_HOST": "bsm_host",
        "PORT": "bsm_port",
        "BSM_SUPERVISOR_WEB_PORT": "bsm_port",
        "BSM_LOG_LEVEL": "bsm_log_level",
        "BSM_DB_URL": "bsm_db_url",
    }
    return {
        "values": {key: getattr(config, key) for key in SETTINGS - {"bsm_db_url"}},
        "database_url_configured": config.bsm_db_url is not None,
        "environment_overrides": sorted(
            {names[name] for name in environment if name in names}
        ),
        "paths": {
            key: str(getattr(config, key))
            for key in ("state_dir", "data_dir", "config_dir", "installations_dir")
        },
        "service_unit": config.service_unit,
        "notifications_enabled": (config.state_dir / "notifications.json").exists(),
    }


def _toml(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    raise ValueError("Unsupported TOML value")


def save_settings(config: Config, config_path: Path, store: Store, value: dict) -> dict:
    updates = dict(value["settings"])
    if set(updates) & set(settings(config)["environment_overrides"]):
        raise ValueError(
            "Change environment-controlled settings in Proxmox "
            "or the service environment"
        )
    new = replace(config, **updates)
    runtime_change = bool(set(updates) & RUNTIME_SETTINGS)
    with store.lock():
        store.require_idle()
        user = "bsm"
        if runtime_change:
            require_offline(config)
            user = SystemdService(config).status().get("User") or "bsm"
        if config_path.is_symlink():
            raise ValueError("Configuration file must not be a symlink")
        original = config_path.read_bytes()
        values = tomllib.loads(original.decode())
        values.update(updates)
        data = (
            "\n".join(
                key + " = " + _toml(item)
                for key, item in values.items()
                if item is not None
            )
            + "\n"
        )
        with store.mutation("settings:save"):
            publish(config.state_dir / "config-before-console.toml", original, 0o600)
            publish(config_path, data.encode(), 0o600)
        if runtime_change:
            refresh(new, store, user)
    return {
        "saved": True,
        "managed_unit_refreshed": runtime_change,
        "bsm_started": False,
    }


def execute(config: Config, config_path: Path, store: Store, value: dict) -> dict:
    """Execute only typed, fixed operations; secrets never enter result summaries."""
    validate(value)
    action = value["action"]
    if action == "backup-restore":
        return restore_backup(config, store, value)
    if action == "backup-create":
        with store.lock():
            store.require_idle()
            state = SystemdService(config).status()
            require_managed(config, state)
            running = state.get("ActiveState") == "active"
            if running:
                with store.mutation("service:backup-stop", preserve_on_error=True):
                    runtime.shutdown(config)
            result = checkpoints.create(config, store)
            if running:
                operations.control(config, store, "start")
            return {key: result[key] for key in ("id", "created_at", "total_bytes")}
    if action in ("checkpoint-export", "checkpoint-import"):
        with tempfile.TemporaryDirectory(
            prefix=".console-secret-", dir=config.state_dir
        ) as temporary:
            password = None
            if value.get("passphrase"):
                password = Path(temporary) / "password"
                publish(password, value["passphrase"].encode(), 0o600)
            if action == "checkpoint-export":
                manifest = checkpoints.load(config, value["target"])
                used = sum(item["bytes"] for item in console_transfers.listing(config))
                if (
                    len(console_transfers.listing(config))
                    >= console_transfers.MAX_TRANSFERS
                    or manifest["total_bytes"] > config.web_transfer_limit
                    or used + manifest["total_bytes"] > 2 * config.web_transfer_limit
                ):
                    raise ValueError(
                        "Archive exceeds staging limits; delete staged files "
                        "or raise the transfer limit"
                    )
                transfer_id = uuid4().hex
                path = console_transfers.archive(config, transfer_id)
                try:
                    transfer.export_checkpoint(
                        config, store, value["target"], path, password
                    )
                    return {
                        **console_transfers.publish(config, transfer_id, "download"),
                        "encrypted": password is not None,
                    }
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
            path = console_transfers.resolve(config, value["target"], "upload")
            imported = transfer.import_checkpoint(
                config, store, path, password, config.web_transfer_limit
            )
            console_transfers.remove(config, value["target"])
            return imported
    if action == "checkpoint-delete":
        return delete_checkpoint(config, store, value["target"])
    if action == "checkpoint-pin":
        checkpoints.pin(config, store, value["target"], value.get("enabled", True))
        return {"pinned": value.get("enabled", True)}
    if action == "checkpoint-prune":
        return {"removed": checkpoints.prune(config, store, value["keep"])}
    if action == "journal-prune":
        return retention.prune(config, store, value["keep_days"], value["keep_jobs"])
    if action == "transfer-delete":
        with store.mutation("transfer:delete"):
            console_transfers.remove(config, value["target"])
        return {"deleted": True}
    if action == "settings-save":
        return save_settings(config, config_path, store, value)
    if action == "schedule-save":
        with tempfile.TemporaryDirectory(dir=config.state_dir) as temporary:
            path = Path(temporary) / "policy.json"
            write_json(path, value["policy"])
            return scheduling.install(
                config, config_path, store, path, replace_existing=True
            )
    if action.startswith("schedule-"):
        if action == "schedule-run":
            return scheduling.run(config, store, value["target"])
        getattr(scheduling, "remove" if action == "schedule-remove" else "acknowledge")(
            config, store, value["target"]
        )
        return {"completed": action}
    if action == "notifications-save":
        with tempfile.TemporaryDirectory(dir=config.state_dir) as temporary:
            path = Path(temporary) / "webhook.json"
            write_json(path, {"url": value["url"]})
            with store.mutation("notifications:configure"):
                events.configure(config.state_dir, path)
        return {"enabled": True}
    if action == "notifications-disable":
        with store.mutation("notifications:disable"):
            (config.state_dir / "notifications.json").unlink(missing_ok=True)
        return {"enabled": False}
    if action == "notifications-deliver":
        return events.deliver(config.state_dir, store)
    if action == "supervisor-check":
        releases = json.loads(self_update.fetch("/releases?per_page=20", "", 1024**2))
        return {
            "installed": self_update.information(),
            "releases": [
                {key: item[key] for key in ("tag_name", "prerelease", "published_at")}
                for item in releases
                if not item["draft"]
            ],
        }
    if action == "supervisor-update":
        if value.get("github_token"):
            os.environ["GH_TOKEN"] = value["github_token"]
        try:
            result = self_update.update(
                config,
                config_path,
                store,
                value.get("ref", "latest"),
                resume=value.get("resume", False),
            )
        finally:
            os.environ.pop("GH_TOKEN", None)
        return {
            "phase": result["phase"],
            "revision": result["revision"],
            "console_restart_required": True,
        }
    if action == "console-restart":
        store.require_idle()
        command_result = subprocess.run(
            [
                "systemctl",
                "show",
                "bsm-supervisor-console.service",
                "-p",
                "Description",
                "--value",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        if command_result.stdout.strip() != "BSM Supervisor independent console":
            raise ValueError(
                "Console restart requires the generated standard console unit"
            )
        subprocess.run(
            [
                "systemd-run",
                "--quiet",
                "--collect",
                "--on-active=5s",
                "/usr/bin/systemctl",
                "restart",
                "bsm-supervisor-console.service",
            ],
            check=True,
            timeout=15,
        )
        return {
            "restart_scheduled": True,
            "reconnect": "Reload and reconnect after the console restarts",
        }
    if action == "unit-refresh":
        user = SystemdService(config).status().get("User") or "bsm"
        refresh(config, store, user)
        return {"refreshed": True, "bsm_started": False}
    if action == "service-logs":
        command_result = subprocess.run(
            [
                "journalctl",
                "-u",
                config.service_unit,
                "-n",
                "200",
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return {"log": command_result.stdout[-65536:]}
    if action == "doctor":
        return diagnostics.doctor(config, False)
    if action == "health":
        current = selection(config, store)
        if current is None:
            raise ValueError("Select a BSM installation first")
        return runtime.ready(config, current["version"])
    raise ValueError("Unsupported administration action")


def execute_request(
    config: Config, config_path: Path, store: Store, request_id: str
) -> dict:
    """Consume a private request once; passwords are never command arguments."""
    import stat

    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("Invalid console request ID")
    path = config.state_dir / "web-jobs" / (request_id + ".request")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size > 65536
            ):
                raise ValueError("Invalid private console request")
            value = json.loads(stream.read())
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(value, dict):
        raise ValueError("Console request must be an object")
    return execute(config, config_path, store, value)
