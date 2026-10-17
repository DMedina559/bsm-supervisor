"""Rerunnable first installation setup without guessing existing data versions."""

import os
import pwd
import subprocess
import sys

from packaging.version import Version

from . import operations, runtime
from .activation import activate, selection
from .config import Config
from .diagnostics import urls
from .install import install
from .integration import require_managed
from .releases import discover, select
from .service import ServiceError, SystemdService
from .store import Store
from .unit_refresh import refresh


def setup(
    config: Config,
    store: Store,
    selector: str | None,
    installation_id: str | None,
    user: str,
    offline: bool,
    start: bool = True,
    wait: bool = False,
) -> dict:
    """Prepare a new managed deployment or check an existing selected runtime."""
    if os.geteuid() != 0:
        raise ValueError("setup requires root")
    if wait and not start:
        raise ValueError("--wait requires starting BSM; omit --no-start")
    if selector is not None and installation_id is not None:
        raise ValueError("Choose a release selector or --installation, not both")
    with store.lock():
        # Claiming and completing a no-op mutation checks pending recovery blocks.
        with store.mutation("setup:preflight"):
            pass
        current = selection(config, store)
        service = SystemdService(config)
        try:
            state = service.status()
        except ServiceError as error:
            if str(error) != f"Service is not installed: {config.service_unit}":
                raise
            state = {"ActiveState": "inactive", "MainPID": "0", "Job": ""}
        if state.get("ActiveState") == "active":
            if (
                selector
                or current is None
                or installation_id not in (None, current["id"])
            ):
                raise ValueError(
                    "Use upgrade to change versions on an existing running deployment"
                )
            require_managed(config, state)
            if wait:
                runtime.wait_ready(config, current["version"])
            return {
                "installation": current["id"],
                "running": True,
                "urls": urls(config),
            }
        if not offline and sys.stdin.isatty():
            offline = (
                input(
                    "Confirm no unmanaged BSM or Minecraft writers "
                    "use these paths? [y/N] "
                )
                .strip()
                .lower()
                == "y"
            )
        if not offline:
            raise ValueError("Stopped-service setup requires --offline-confirmed")
        if state.get("MainPID") != "0" or state.get("Job"):
            raise ValueError("Wait for BSM to stop completely before setup")
        if current is None and (
            (config.config_dir / "bedrock_server_manager.json").exists()
            or (config.config_dir / "bedrock-server-manager.db").exists()
        ):
            raise ValueError(
                "Existing BSM data has no selected installation; register and activate "
                "its matching version explicitly before setup"
            )
        if current is not None and (
            selector or installation_id not in (None, current["id"])
        ):
            raise ValueError(
                "Use upgrade to change the selected version against existing data"
            )
        try:
            account = pwd.getpwnam(user)
        except KeyError:
            # Validate the account name before passing it to useradd or rendering.
            from .activation import service_unit

            service_unit(config, user)
            subprocess.run(
                [
                    "/usr/sbin/useradd",
                    "--system",
                    "--user-group",
                    "--home-dir",
                    str(config.data_dir),
                    "--shell",
                    "/usr/sbin/nologin",
                    user,
                ],
                check=True,
                timeout=30,
                capture_output=True,
            )
            account = pwd.getpwnam(user)
        for path in (config.data_dir, config.config_dir):
            if not path.exists():
                path.mkdir(mode=0o750, parents=True)
                os.chown(path, account.pw_uid, account.pw_gid)
        if current is None:
            candidates = store.installations()
            if selector is None and installation_id is None and sys.stdin.isatty():
                choices = [(item["id"], "installation") for item in candidates]
                choices.extend([("prerelease", "release"), ("stable", "release")])
                for index, (name, _) in enumerate(choices, 1):
                    print(f"{index}. {name}")
                default = 1 if len(candidates) == 1 else len(candidates) + 1
                choice = input(
                    f"Choose an installation or release channel [{default}]: "
                ).strip()
                try:
                    name, kind = choices[int(choice or str(default)) - 1]
                    if int(choice or str(default)) < 1:
                        raise ValueError
                except (ValueError, IndexError):
                    raise ValueError("Choose a displayed menu number") from None
                if kind == "installation":
                    installation_id = name
                else:
                    selector = name
            if installation_id:
                item = next(
                    (item for item in candidates if item["id"] == installation_id), None
                )
                if item is None:
                    raise ValueError("Unknown installation; inspect installations")
                installation_id = item["id"]
            elif selector is None and len(candidates) == 1:
                installation_id = candidates[0]["id"]
            elif selector is None and candidates:
                raise ValueError(
                    "Multiple installations exist; choose --installation ID"
                )
            else:
                release = select(discover(), selector or "prerelease")
                if Version(release.version) < Version("4.0.0b1"):
                    raise ValueError(
                        "Guided setup requires BSM 4.0; choose a compatible "
                        "prerelease or exact version"
                    )
                prepared = install(config, store, release)
                installation_id = prepared["id"]
        refresh(config, store, user, create=True)
        if current is None:
            assert installation_id is not None
            activate(config, store, installation_id)
            current = selection(config, store)
        if start:
            operations.control(config, store, "start", wait)
        assert current is not None
        return {
            "installation": current["id"],
            "running": start,
            "urls": urls(config),
            "next": "Complete BSM user setup in the browser if prompted",
        }
