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
                capture_output=True, text=True, timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(
                "systemctl unavailable or timed out; inspect service state before retrying"
            ) from exc
        if result.returncode:
            raise ServiceError(result.stderr.strip() or "systemctl failed")
        return result.stdout.strip()

    def status(self) -> dict[str, str]:
        """Read explicit systemd properties, including inactive services."""
        output = self._run([
            "show", self.unit,
            "--property=LoadState,ActiveState,SubState,MainPID,Result",
        ])
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if values.get("LoadState") == "not-found":
            raise ServiceError(f"Service is not installed: {self.unit}")
        return values

    def control(self, action: str) -> None:
        """Request an explicit service action."""
        if action not in ("start", "stop", "restart"):
            raise ValueError("Unsupported service action")
        self._run([action, self.unit])
