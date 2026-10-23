"""Read-only operator diagnostics with no credentials in reports."""

import ipaddress
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import runtime
from .checkpoint_files import read_json
from .config import Config
from .integration import inspect, require_managed
from .service import ServiceError, service_backend


def urls(config: Config) -> list[str]:
    """Describe reachable listener addresses without changing networking."""
    hosts = [config.bsm_host]
    if config.bsm_host in ("0.0.0.0", "::"):
        hosts = []
        try:
            result = subprocess.run(
                [shutil.which("ip") or "/usr/sbin/ip", "-j", "address", "show"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for interface in json.loads(result.stdout):
                for address in interface.get("addr_info", []):
                    ip = ipaddress.ip_address(address["local"])
                    if (
                        not ip.is_loopback
                        and not ip.is_link_local
                        and (ip.version == (4 if config.bsm_host == "0.0.0.0" else 6))
                    ):
                        hosts.append(str(ip))
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            pass
        if not hosts:
            hosts = ["CONTAINER-IP"]
    return [
        f"http://{'[' + host + ']' if ':' in host else host}:{config.bsm_port}"
        for host in sorted(set(hosts))
    ]


def doctor(config: Config, check_health: bool = True) -> dict:
    """Inspect service, credentials, recovery blocks, coverage, and disk space."""
    checks: list[dict] = []

    def add(code: str, ok: bool, message: str, remedy: str = "") -> None:
        checks.append({"code": code, "ok": ok, "message": message, "remedy": remedy})

    current = None
    pending = []
    workflows = []
    if (config.state_dir / "supervisor.sqlite3").is_file():
        try:
            database = sqlite3.connect(
                (config.state_dir / "supervisor.sqlite3").as_uri() + "?mode=ro",
                uri=True,
            )
            try:
                database.row_factory = sqlite3.Row
                installations = [
                    dict(row) for row in database.execute("SELECT * FROM installations")
                ]
                pending = [
                    row[0]
                    for row in database.execute(
                        "SELECT id FROM operations WHERE state = 'running'"
                    )
                ]
                workflows = [
                    row[0]
                    for row in database.execute(
                        "SELECT id FROM workflows WHERE state = 'running'"
                    )
                ]
            finally:
                database.close()
            link = config.installations_dir / "current"
            if link.is_symlink():
                resolved = str(link.resolve(strict=True))
                current = next(
                    (item for item in installations if item["path"] == resolved), None
                )
            elif link.exists():
                raise ValueError("Invalid selection")
            add(
                "journal",
                not (pending or workflows),
                (
                    "No unresolved operations"
                    if not (pending or workflows)
                    else "Recovery is pending"
                ),
                "Inspect history and upgrades; use the dedicated recovery command",
            )
        except (OSError, ValueError, sqlite3.Error):
            add(
                "journal",
                False,
                "Supervisor state cannot be read",
                "Inspect state permissions and selection",
            )
    else:
        add(
            "journal", False, "Supervisor state is not initialized", "Run setup or init"
        )
    add(
        "selection",
        current is not None,
        "Installation selected" if current else "No installation selected",
        "Run setup for a new installation; inspect installations "
        "for an existing deployment",
    )
    for name, path in (
        ("state", config.state_dir),
        ("installations", config.installations_dir),
        ("data", config.data_dir),
        ("configuration", config.config_dir),
    ):
        add(
            "directory:" + name,
            path.is_dir(),
            (
                name + " directory exists"
                if path.is_dir()
                else name + " directory is missing"
            ),
            "Check configuration paths and permissions",
        )
        if name == "state" and path.exists():
            add(
                "state-permissions",
                not bool(path.stat().st_mode & 0o077),
                "Supervisor state must be private",
                "Use owner-only state directory permissions",
            )
    nearest = config.state_dir
    while not nearest.exists():
        nearest = nearest.parent
    free = shutil.disk_usage(nearest).free
    add(
        "disk",
        free >= 512 * 1024 * 1024,
        "Free bytes on supervisor state filesystem: " + str(free),
        "Free space before installing or creating checkpoints",
    )
    state = None
    try:
        state = service_backend(config).status()
        add("service", True, "Service state: " + state.get("ActiveState", "unknown"))
        try:
            require_managed(config, state)
            add("managed-unit", True, "Managed unit and environment match")
        except (OSError, ValueError, sqlite3.Error):
            add(
                "managed-unit",
                False,
                "Managed unit or environment needs refresh",
                "Stop BSM gracefully, then run unit-refresh --offline-confirmed",
            )
        if state.get("ActiveState") == "failed":
            add(
                "service-failure",
                False,
                "The BSM service failed",
                f"Inspect journalctl -u {config.service_unit} -n 60 --no-pager",
            )
    except ServiceError:
        add(
            "service",
            False,
            "systemd service is missing or unavailable",
            "Check systemd; run setup for a new unit",
        )
    if state:
        user = state.get("User") or "bsm"
        try:
            account = pwd.getpwnam(user)
            groups = os.getgrouplist(user, account.pw_gid)
            add(
                "service-account",
                account.pw_uid != 0,
                "BSM service account: " + user,
                "Use a dedicated unprivileged service account",
            )
            for label, path, required in (
                ("data", config.data_dir, 7),
                ("configuration", config.config_dir, 7),
                ("python", config.installations_dir / "current/bin/python", 5),
            ):
                add(
                    "service-access:" + label,
                    accessible(path, account.pw_uid, groups, required),
                    "Service account access to " + label,
                    "Check ownership, traversal permissions, "
                    "and interpreter accessibility",
                )
            backups = Path(inspect(config)["paths"]["backups"])
            if backups.is_dir() and not backups.is_symlink():
                for path in (backups, *sorted(backups.iterdir())):
                    if path.is_dir() and not path.is_symlink():
                        add(
                            "service-access:backup:" + path.name,
                            accessible(path, account.pw_uid, groups, 7),
                            "Service account access to backup directory: " + str(path),
                            "Stop BSM and run permissions-repair --offline-confirmed; "
                            "repair external backup paths separately",
                        )
        except KeyError:
            add(
                "service-account",
                False,
                "BSM service account is missing",
                "Run setup or create the configured service account",
            )
    update_plan = config.state_dir / "supervisor-update.json"
    if update_plan.exists():
        try:
            complete = read_json(update_plan).get("phase") == "completed"
        except (OSError, ValueError):
            complete = False
        add(
            "supervisor-update",
            complete,
            (
                "Supervisor update completed"
                if complete
                else "Supervisor update needs recovery"
            ),
            "Inspect history; run supervisor-update --resume --maintenance-confirmed",
        )
    for marker in ("supervisor-recovery.required", "supervisor-upgrade.required"):
        if (config.config_dir / marker).exists():
            add(
                "recovery-marker",
                False,
                "Startup is blocked by recovery state",
                "Inspect history and upgrades; do not delete recovery markers",
            )
    if check_health and state and state.get("ActiveState") == "active" and current:
        try:
            runtime.ready(config, current["version"])
            add("readiness", True, "Authenticated BSM readiness verified")
        except runtime.AuthenticationError:
            add(
                "readiness",
                False,
                "Readiness authentication is unavailable or rejected",
                "Use BSM with local readiness support or configure "
                "api_token_file for an older version",
            )
        except (OSError, ValueError, runtime.RuntimeErrorBase):
            add(
                "readiness",
                False,
                "BSM is running but readiness could not be verified",
                "Inspect service logs, runtime identity, and selected version",
            )
    try:
        coverage = inspect(config)
        add(
            "checkpoint-coverage",
            coverage["supported"],
            (
                "Checkpoint coverage is complete"
                if coverage["supported"]
                else "Checkpoint coverage is not ready"
            ),
            "Run integration; initialize BSM and declare external checkpoint_paths",
        )
    except (OSError, ValueError, sqlite3.Error):
        add(
            "checkpoint-coverage",
            False,
            "Checkpoint coverage cannot be inspected",
            "Inspect BSM configuration and database",
        )
    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "urls": urls(config),
        "pending_operations": pending,
        "pending_upgrades": workflows,
    }


def accessible(path, uid: int, groups: list[int], required: int) -> bool:
    """Check Unix mode traversal and access for the service account."""

    def grants(candidate, bits: int) -> bool:
        info = candidate.stat()
        permissions = (
            info.st_mode >> 6
            if info.st_uid == uid
            else info.st_mode >> 3 if info.st_gid in groups else info.st_mode
        ) & 7
        return uid == 0 or permissions & bits == bits

    try:
        for candidate in (path, path.resolve(strict=True)):
            if not grants(candidate, required):
                return False
            if any(not grants(parent, 1) for parent in candidate.parents):
                return False
        return True
    except OSError:
        return False
