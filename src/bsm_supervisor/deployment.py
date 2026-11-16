"""Describe installation ownership and supported systemd deployment operations."""

import argparse
import json
import os
import platform
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from .checkpoint_files import read_json, write_json
from .config import Config
from .diagnostics import urls
from .service import ServiceError, service_backend

CAPABILITIES = {
    "service_control": True,
    "managed_units": True,
    "systemd_schedules": True,
    "supervisor_update": True,
    "checkpoint_restore": True,
}


def information(config: Config) -> dict:
    """Return bounded deployment metadata; credentials are never included."""
    path = config.state_dir / "deployment.json"
    installed = None
    if path.is_file():
        value = read_json(path)
        allowed = {"schema", "method", "backend", "recorded_at", "uid", "version"}
        if value.keys() - allowed or value.get("schema") != 1:
            raise ValueError("Invalid deployment metadata")
        installed = value
    return {
        "backend": "systemd",
        "platform": platform.system(),
        "python": platform.python_version(),
        "capabilities": dict(CAPABILITIES),
        "installation": installed,
        "service": config.service_unit,
        "paths": {
            key: str(getattr(config, key))
            for key in ("state_dir", "installations_dir", "data_dir", "config_dir")
        },
    }


def record(config: Config, method: str) -> dict:
    """Record the installation mechanism after successful publication."""
    if method not in ("linux", "proxmox-lxc"):
        raise ValueError("Unknown installation mechanism")
    value = {
        "schema": 1,
        "method": method,
        "backend": "systemd",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "uid": os.geteuid(),
        "version": version("bsm-supervisor"),
    }
    write_json(config.state_dir / "deployment.json", value)
    return value


def summary(config: Config) -> str:
    """Report configured listeners and observed state without claiming readiness."""
    try:
        state = service_backend(config).status().get("ActiveState", "unknown")
    except ServiceError:
        state = "not installed or unavailable"
    return "\n".join(
        [
            "Installation complete (systemd)",
            f"BSM service: {config.service_unit} — {state}",
            "Configured BSM URLs: " + ", ".join(urls(config)),
            "URLs describe configuration; application readiness is checked separately.",
            "Inspect: bsm-supervisor status; bsm-supervisor history; "
            "bsm-supervisor doctor",
            "New installation: bsm-supervisor setup --offline-confirmed",
            "Console setup/access: docs/web-console.md "
            "(no listener is exposed automatically)",
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--record", choices=("linux", "proxmox-lxc"))
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.record:
        record(config, args.record)
    print(
        summary(config) if args.summary else json.dumps(information(config), indent=2)
    )
