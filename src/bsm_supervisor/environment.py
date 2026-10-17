"""Read explicitly supported overrides from the shell or LXC init environment."""

import os
from pathlib import Path

NAMES = {
    "BSM_SUPERVISOR_WEB_HOST",
    "BSM_SUPERVISOR_WEB_PORT",
    "HOST",
    "PORT",
    "BSM_CONFIG_DIR",
    "BSM_DATA_DIR",
    "BSM_DB_URL",
    "BSM_LOG_LEVEL",
}


def overrides() -> dict[str, str]:
    """Include LXC PID 1 values when pct attach does not inherit its environment."""
    values: dict[str, str] = {}
    try:
        if Path("/run/systemd/container").read_text().strip() == "lxc":
            raw = Path("/proc/1/environ").read_bytes()
            if len(raw) > 1024 * 1024:
                raise ValueError("Container init environment exceeds size limit")
            for entry in raw.split(b"\0"):
                name, separator, value = entry.partition(b"=")
                if separator and name.decode("utf-8", errors="replace") in NAMES:
                    values[name.decode()] = value.decode("utf-8")
    except (FileNotFoundError, PermissionError):
        pass
    values.update({name: os.environ[name] for name in NAMES if name in os.environ})
    return values
