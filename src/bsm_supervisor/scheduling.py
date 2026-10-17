"""Opt-in systemd schedules with UTC maintenance gates and durable run outcomes."""

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from packaging.version import Version

from . import checkpoints, events, operations, retention, runtime, transfer, upgrades
from .activation import selection
from .checkpoint_files import read_json, write_json
from .config import Config
from .integration import require_managed
from .releases import discover, select
from .service import SystemdService
from .store import Store

ACTIONS = (
    "check-updates",
    "checkpoint",
    "upgrade",
    "export",
    "prune",
    "journal-prune",
    "notify",
)


def policy(value: dict) -> dict:
    """Validate a bounded policy before creating files or stopping any service."""
    allowed = {
        "name",
        "calendar",
        "action",
        "window_start",
        "window_end",
        "days",
        "selector",
        "accept_migrations",
        "maintenance_confirmed",
        "destination",
        "passphrase_file",
        "require_mount",
        "keep",
        "keep_days",
        "keep_jobs",
    }
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError("Unknown schedule policy fields")
    if not isinstance(value.get("name"), str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,39}", value["name"]
    ):
        raise ValueError(
            "Schedule name must contain lowercase letters, digits and hyphens"
        )
    if value.get("action") not in ACTIONS:
        raise ValueError("Unsupported scheduled action")
    calendar = value.get("calendar", "")
    if (
        not isinstance(calendar, str)
        or not calendar.endswith(" UTC")
        or len(calendar) > 128
        or any(c in calendar for c in "\n\r%")
    ):
        raise ValueError(
            "Calendar must be a single systemd calendar expression ending in UTC"
        )
    for key in ("window_start", "window_end"):
        if not isinstance(value.get(key), str) or not re.fullmatch(
            r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value[key]
        ):
            raise ValueError("Maintenance window needs HH:MM UTC start and end")
    if value["window_start"] == value["window_end"]:
        raise ValueError("Maintenance window cannot span zero or all 24 hours")
    days = value.get("days")
    if (
        not isinstance(days, list)
        or not days
        or any(type(d) is not int or d not in range(1, 8) for d in days)
    ):
        raise ValueError("days must contain ISO weekdays 1 through 7")
    if (
        value["action"] in ("checkpoint", "upgrade", "prune", "journal-prune")
        and value.get("maintenance_confirmed") is not True
    ):
        raise ValueError("Scheduled mutations need maintenance_confirmed=true")
    if value["action"] == "upgrade" and value.get("accept_migrations") is not True:
        raise ValueError("Scheduled upgrades need accept_migrations=true")
    if value.get("selector", "stable") not in ("stable", "prerelease"):
        raise ValueError("Scheduled selector must be stable or prerelease")
    if value["action"] == "export":
        destination = value.get("destination", "")
        if not isinstance(destination, str) or not Path(destination).is_absolute():
            raise ValueError("Export schedule needs an absolute destination directory")
    if "require_mount" in value and (
        not isinstance(value["require_mount"], str)
        or not Path(value["require_mount"]).is_absolute()
    ):
        raise ValueError("require_mount must be an absolute path")
    if "passphrase_file" in value and (
        not isinstance(value["passphrase_file"], str)
        or not Path(value["passphrase_file"]).is_absolute()
    ):
        raise ValueError("passphrase_file must be an absolute path")
    if type(value.get("keep", 3)) is not int or value.get("keep", 3) < 1:
        raise ValueError("Scheduled retention must keep at least one checkpoint")
    for field in ("keep_days", "keep_jobs"):
        if type(value.get(field, 1)) is not int or value.get(field, 1) < 1:
            raise ValueError("Journal retention values must be positive integers")
    return value


def in_window(value: dict, now: datetime) -> bool:
    """Evaluate UTC windows, including the preceding day's overnight window."""
    from datetime import timedelta

    now = now.astimezone(timezone.utc)
    clock = now.strftime("%H:%M")
    start, end = value["window_start"], value["window_end"]
    if start < end:
        return now.isoweekday() in value["days"] and start <= clock < end
    day = now if clock >= start else now - timedelta(days=1)
    return day.isoweekday() in value["days"] and (clock >= start or clock < end)


def _quote(value: str) -> str:
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("Unit arguments must be single-line paths")
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        + '"'
    )


def units(config_path: Path, value: dict) -> tuple[str, str]:
    """Render a single bounded CLI job and a nonpersistent timer."""
    policy(value)
    if not config_path.is_absolute():
        raise ValueError("Configuration path must be absolute")
    # Stable CLI link survives supervisor environment updates.
    command = " ".join(
        _quote(arg)
        for arg in (
            "/usr/bin/bsm-supervisor",
            "--config",
            str(config_path),
            "schedule-run",
            value["name"],
        )
    )
    service = f"""[Unit]
Description=BSM Supervisor schedule {value['name']}
After=network-online.target
[Service]
Type=oneshot
ExecStart={command}
UMask=0077
TimeoutStartSec=2h
"""
    timer = f"""[Unit]
Description=BSM Supervisor timer {value['name']}
[Timer]
OnCalendar={value['calendar']}
Persistent=false
AccuracySec=30s
[Install]
WantedBy=timers.target
"""
    return service, timer


def install(config: Config, config_path: Path, store: Store, source: Path) -> dict:
    """Install owned units, preserving existing foreign units and policies."""
    value = policy(read_json(source))
    if os.geteuid() != 0:
        raise ValueError("Schedule installation requires root")
    subprocess.run(
        ["systemd-analyze", "calendar", value["calendar"]],
        check=True,
        capture_output=True,
        timeout=15,
    )
    service, timer = units(config_path.resolve(), value)
    name = "bsm-supervisor-schedule-" + value["name"]
    folder = config.state_dir / "schedules"
    with store.mutation("schedule:install:" + value["name"]):
        folder.mkdir(mode=0o700, exist_ok=True)
        for suffix in ("service", "timer"):
            path = Path("/etc/systemd/system") / (name + "." + suffix)
            if path.is_symlink() or path.exists():
                raise ValueError(
                    "Schedule units already exist; remove the schedule before "
                    "replacing it"
                )
        write_json(folder / (value["name"] + ".json"), value)
        for suffix, content in (("service", service), ("timer", timer)):
            target = Path("/etc/systemd/system") / (name + "." + suffix)
            with target.open("x") as stream:
                stream.write(content)
            target.chmod(0o644)
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        subprocess.run(
            ["systemctl", "enable", "--now", name + ".timer"], check=True, timeout=30
        )
    return {"name": value["name"], "enabled": True}


def listing(config: Config) -> list[dict]:
    """List configured policies and the outcome of their most recent run."""
    folder = config.state_dir / "schedules"
    return [
        {
            "policy": read_json(path),
            "last_run": (
                read_json(path.with_suffix(".run"))
                if path.with_suffix(".run").exists()
                else None
            ),
        }
        for path in sorted(folder.glob("*.json"))
    ]


def run(config: Config, store: Store, name: str) -> dict:
    """Run one policy; never catch up a missed window or clear recovery state."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise ValueError("Invalid schedule name")
    path = config.state_dir / "schedules" / (name + ".json")
    value = policy(read_json(path))
    outcome = path.with_suffix(".run")
    with store.lock():
        if outcome.exists() and read_json(outcome).get("state") in (
            "running",
            "failed",
        ):
            raise ValueError(
                "Previous scheduled run was interrupted; inspect history and "
                "schedule-acknowledge"
            )
        now = datetime.now(timezone.utc)
        if not in_window(value, now):
            report: dict = {"state": "skipped", "reason": "Outside maintenance window"}
            write_json(outcome, report)
            return report
        store.require_idle()
        report = {
            "state": "running",
            "started_at": now.isoformat(),
            "action": value["action"],
        }
        write_json(outcome, report)
        try:
            action = value["action"]
            if action == "check-updates":
                latest = select(discover(), value.get("selector", "stable"))
                current = selection(config, store)

                report["available_version"] = latest.version
                report["update_available"] = current is not None and Version(
                    latest.version
                ) > Version(current["version"])
            elif action == "upgrade":
                current = selection(config, store)
                latest = select(discover(), value.get("selector", "stable"))

                if current is None:
                    raise ValueError(
                        "Select an installation before scheduling upgrades"
                    )
                if Version(latest.version) > Version(current["version"]):
                    report["result"] = upgrades.upgrade(config, store, latest.version)
                else:
                    report["result"] = "No newer version; no change"
            elif action == "checkpoint":
                state = SystemdService(config).status()
                require_managed(config, state)
                was_running = state.get("ActiveState") == "active" and state.get(
                    "MainPID"
                ) not in (None, "", "0")
                if state.get("ActiveState") not in (
                    "active",
                    "inactive",
                    "failed",
                ) or state.get("Job"):
                    raise ValueError("Service state is not settled")
                report["was_running"] = was_running
                write_json(outcome, report)
                if was_running:
                    with store.mutation(
                        "schedule:graceful-stop", preserve_on_error=True
                    ):
                        runtime.shutdown(config)
                report["checkpoint_id"] = checkpoints.create(config, store)["id"]
                if was_running:
                    operations.control(config, store, "start", True)
            elif action == "export":
                saved = [
                    item
                    for item in checkpoints.list_checkpoints(config)
                    if not item.get("invalid")
                ]
                if not saved:
                    raise ValueError("No completed checkpoint to export")
                latest_saved = max(saved, key=lambda item: item["created_at"])
                password = (
                    Path(value["passphrase_file"])
                    if value.get("passphrase_file")
                    else None
                )
                target = Path(value["destination"]) / (
                    latest_saved["id"]
                    + (".bsmcheckpoint.enc" if password else ".bsmcheckpoint")
                )
                if target.exists() or target.is_symlink():
                    raise ValueError(
                        "Export archive already exists; "
                        "inspect or choose another destination"
                    )
                report["result"] = transfer.export_checkpoint(
                    config,
                    store,
                    latest_saved["id"],
                    target,
                    password,
                    (
                        Path(value["require_mount"])
                        if value.get("require_mount")
                        else None
                    ),
                )
            elif action == "prune":
                report["removed"] = checkpoints.prune(
                    config, store, value.get("keep", 3)
                )
            elif action == "journal-prune":
                report["result"] = retention.prune(
                    config,
                    store,
                    value.get("keep_days", 30),
                    value.get("keep_jobs", 100),
                )
            else:
                report["result"] = events.deliver(config.state_dir, store)
            report["state"] = "succeeded"
        except events.DeliveryError:
            report.update(
                state="retry",
                remedy="Notification transport failed; retry at the next permitted run",
            )
            write_json(outcome, report)
            return report
        except BaseException:
            # A killed process leaves 'running'; errors leave an explicit failed run.
            report["state"] = "failed"
            report["remedy"] = (
                "Inspect history, upgrades and service status before retrying"
            )
            write_json(outcome, report)
            raise
        write_json(outcome, report)
        return report


def acknowledge(config: Config, store: Store, name: str) -> None:
    """Acknowledge only schedule bookkeeping after separate service recovery."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise ValueError("Invalid schedule name")
    with store.lock():
        store.require_idle()
        path = config.state_dir / "schedules" / (name + ".run")
        value = read_json(path)
        value["state"] = "acknowledged"
        write_json(path, value)


def remove(config: Config, store: Store, name: str) -> None:
    """Stop and remove only units matching the supervisor's ownership marker."""
    if os.geteuid() != 0 or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise ValueError("Schedule removal requires root and a valid name")
    unit_name = "bsm-supervisor-schedule-" + name
    with store.mutation("schedule:remove:" + name):
        for suffix, marker in (("service", "schedule"), ("timer", "timer")):
            path = Path("/etc/systemd/system") / (unit_name + "." + suffix)
            if path.is_symlink() or (
                path.exists()
                and f"Description=BSM Supervisor {marker} {name}\n"
                not in path.read_text()
            ):
                raise ValueError("Refusing to remove a foreign schedule unit")
        state = subprocess.run(
            [
                "systemctl",
                "show",
                unit_name + ".service",
                "--property=ActiveState",
                "--value",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if state not in ("inactive", "failed", ""):
            raise ValueError("Wait for the scheduled job to finish before removal")
        subprocess.run(
            ["systemctl", "disable", "--now", unit_name + ".timer"],
            check=True,
            timeout=30,
        )
        for suffix in ("service", "timer"):
            (Path("/etc/systemd/system") / (unit_name + "." + suffix)).unlink(
                missing_ok=True
            )
        (config.state_dir / "schedules" / (name + ".json")).unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
