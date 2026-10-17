"""Bounded systemd commands for one configured BSM service."""

import subprocess

from .config import Config


class ServiceError(RuntimeError):
    """systemd is unavailable or rejected an operation."""


class SystemdService:
    """Use argument lists and never invoke a shell or implicit sudo."""

    def __init__(self, config: Config) -> None:
        self.unit = config.service_unit
        self.timeout = config.service_timeout

    def _run(self, arguments: list[str]) -> str:
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", "--no-pager", "--no-ask-password", *arguments],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(
                "systemctl unavailable or timed out; "
                "inspect service state before retrying"
            ) from exc
        if result.returncode:
            raise ServiceError(result.stderr.strip() or "systemctl failed")
        return result.stdout.strip()

    def status(self) -> dict[str, str]:
        """Read explicit systemd properties, including inactive services."""
        output = self._run(
            [
                "show",
                self.unit,
                "--property=LoadState,ActiveState,SubState,"
                "MainPID,Result,ExecStart,ExecCondition,Job,UnsetEnvironment",
            ]
        )
        values: dict[str, str] = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            # systemctl emits a separate property line for each ExecCondition.
            values[key] = values[key] + "\n" + value if key in values else value
        if values.get("LoadState") == "not-found":
            raise ServiceError(f"Service is not installed: {self.unit}")
        return values

    def reset_failed(self) -> None:
        """Clear a stopped failure state before another managed start."""
        state = self.status()
        if (
            state.get("ActiveState") != "failed"
            or state.get("MainPID") != "0"
            or state.get("Job") != ""
        ):
            raise ServiceError("Only a fully stopped failed service can be reset")
        self._run(["reset-failed", self.unit])

    def control(self, action: str) -> None:
        """Request an explicit service action."""
        if action not in ("start", "stop", "restart"):
            raise ValueError("Unsupported service action")
        self._run([action, self.unit])


def managed_command(config: Config) -> str:
    """Build the exact managed launch command, locking before Python imports BSM."""
    python = config.installations_dir / "current/bin/python"
    maintenance = config.config_dir / "supervisor-maintenance.lock"
    return (
        f"/usr/bin/flock --shared --nonblock --no-fork {maintenance} "
        f"{python} -I -m bedrock_server_manager "
        f"--config-dir {config.config_dir} --data-dir {config.data_dir} "
        f"web start --mode direct --host {config.bsm_host} --port {config.bsm_port}"
    )


def guard_command(config: Config) -> str:
    """Run the independently installed supervisor guard before starting BSM."""
    import sys

    return (
        f"{sys.executable} -I -m bsm_supervisor.launchguard "
        f"--config-dir {config.config_dir}"
    )
