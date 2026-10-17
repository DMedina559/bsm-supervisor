"""Select an installed environment only while the managed service is stopped."""

import os
import re
import uuid

from .config import Config
from .integration import offline_guard
from .service import managed_command
from .store import Store


def service_unit(config: Config, user: str) -> str:
    """Render a systemd unit for explicit operator installation."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("Invalid service account")
    # Restrict rendering instead of guessing systemd escaping for arbitrary paths.
    for path in (config.installations_dir, config.data_dir, config.config_dir):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)):
            raise ValueError(
                "Unit rendering requires paths without spaces or special characters"
            )
    command = managed_command(config)
    return f"""[Unit]
Description=Bedrock Server Manager (BSM Supervisor)
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User={user}
WorkingDirectory={config.data_dir}
ExecCondition=/usr/bin/test ! -e {config.config_dir}/supervisor-recovery.required
ExecStart={command}
Restart=on-failure
RestartSec=10
TimeoutStopSec=180
KillMode=control-group
UMask=0077
UnsetEnvironment=BSM_DB_URL BSM_DATA_DIR BSM_CONFIG_DIR

[Install]
WantedBy=multi-user.target
"""


def activate(config: Config, store: Store, installation_id: str) -> None:
    """Atomically select a registered installation without starting BSM."""
    with (
        store.mutation("activate:" + installation_id, preserve_on_error=True),
        offline_guard(config),
    ):
        candidates = [
            item for item in store.installations() if item["id"] == installation_id
        ]
        if not candidates:
            raise ValueError("Unknown installation")
        target = config.installations_dir.resolve() / installation_id
        registered = candidates[0]
        if target.resolve(strict=True) != target or str(target) != registered["path"]:
            raise ValueError("Installation path changed or is not a direct child")
        if not (target / "bin/python").is_file():
            raise ValueError("Registered environment has no Python executable")
        current = config.installations_dir / "current"
        if current.exists() and not current.is_symlink():
            raise ValueError("The current path is not a supervisor selection symlink")
        temporary = current.parent / (".selection-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to(installation_id, target_is_directory=True)
            os.replace(temporary, current)
            descriptor = os.open(current.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


def selection(config: Config, store: Store) -> dict | None:
    """Read the selected registered environment without contacting systemd."""
    current = config.installations_dir / "current"
    if not current.is_symlink():
        if current.exists():
            raise ValueError("The current path is not a selection symlink")
        return None
    resolved = current.resolve(strict=True)
    for item in store.installations():
        if item["path"] == str(resolved):
            return item
    raise ValueError("The current symlink points to an unregistered installation")
