"""Authenticated local runtime control with verified service/process identity."""

import ipaddress
import json
import math
import os
import stat
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from packaging.version import Version

from .config import Config
from .launchguard import process_generation
from .service import SystemdService


class RuntimeErrorBase(RuntimeError):
    """A bounded runtime operation failed without exposing credentials."""


class AuthenticationError(RuntimeErrorBase):
    """The configured readiness credential is missing or rejected."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bearer(config: Config) -> str:
    """Read a private operator-provided BSM token without logging it."""
    path = config.api_token_file
    if path is None:
        raise AuthenticationError(
            "Configure api_token_file for authenticated readiness"
        )
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
        raise AuthenticationError(
            "api_token_file must be a private regular file (0600)"
        )
    token = path.read_text().strip()
    if not token or any(character.isspace() for character in token):
        raise AuthenticationError("api_token_file must contain one BSM bearer token")
    return token


def control_record(config: Config, status: dict[str, str]) -> dict:
    """Bind local credentials to the current systemd PID and its process generation."""
    path = config.config_dir / "web_server.control.json"
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != config.config_dir.stat().st_uid
        or info.st_size > 65536
    ):
        raise RuntimeErrorBase("Invalid BSM runtime control record permissions")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("config_dir"), str):
        raise RuntimeErrorBase("Invalid BSM runtime control identity")
    pid = record.get("pid")
    if type(pid) is not int or str(pid) != status.get("MainPID"):
        raise RuntimeErrorBase("Runtime record does not match the managed service PID")
    ticks = int(process_generation(pid))
    boot = next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    created = boot + ticks / os.sysconf("SC_CLK_TCK")
    if (
        type(record.get("created_at")) not in (int, float)
        or not math.isfinite(record["created_at"])
        or abs(created - record["created_at"]) > 0.01
        or Path(record.get("config_dir", "")).resolve() != config.config_dir.resolve()
    ):
        raise RuntimeErrorBase("Runtime process generation or configuration differs")
    arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    pairs = list(zip(arguments, arguments[1:]))
    if (b"-m", b"bedrock_server_manager") not in pairs or (
        b"--config-dir",
        str(config.config_dir.resolve()).encode(),
    ) not in pairs:
        raise RuntimeErrorBase("Managed process is not the expected BSM host")
    host = record.get("host")
    try:
        if not isinstance(host, str) or not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise RuntimeErrorBase("Runtime control must use a loopback address") from exc
    if type(record.get("port")) is not int or not 1 <= record["port"] <= 65535:
        raise RuntimeErrorBase("Invalid runtime control port")
    if not isinstance(record.get("token"), str) or not isinstance(
        record.get("instance_id"), str
    ):
        raise RuntimeErrorBase("Runtime record lacks its control identity")
    if "readiness_token" in record:
        credential = record["readiness_token"]
        if (
            not isinstance(credential, str)
            or not credential
            or len(credential) > 16384
            or any(character.isspace() for character in credential)
        ):
            raise AuthenticationError("Invalid local readiness credential")
    return record


def request(
    record: dict,
    path: str,
    *,
    token: str | None = None,
    shutdown: bool = False,
    readiness: bool = False,
) -> dict:
    """Send one bounded loopback request without proxy or redirect forwarding."""
    host = record["host"]
    if ":" in host:
        host = "[" + host + "]"
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if readiness:
        headers["X-BSM-Readiness-Token"] = record["readiness_token"]
    if shutdown:
        headers["X-BSM-Shutdown-Token"] = record["token"]
        headers["Content-Type"] = "application/json"
        data = b"{}"
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(
            Request(
                f"http://{host}:{record['port']}{path}", data=data, headers=headers
            ),
            timeout=5,
        ) as response:
            if response.status != (202 if shutdown else 200):
                raise RuntimeErrorBase("Unexpected BSM runtime response status")
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeErrorBase("BSM runtime response exceeds the size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            raise AuthenticationError(
                "BSM readiness/shutdown authentication rejected"
            ) from None
        raise RuntimeErrorBase(f"BSM runtime returned HTTP {code}") from None
    except (OSError, URLError, ValueError):
        raise RuntimeErrorBase("BSM runtime request failed") from None


def ready(config: Config, expected_version: str) -> dict:
    """Require semantic health and the exact selected running BSM version."""
    status = SystemdService(config).status()
    if status.get("ActiveState") != "active":
        raise RuntimeErrorBase("BSM service is not active")
    record = control_record(config, status)
    if "readiness_token" in record:
        payload = request(record, "/api/runtime/readiness", readiness=True)
        identity = payload.get("runtime")
        if (
            not isinstance(identity, dict)
            or identity.get("instance_id") != record["instance_id"]
            or identity.get("pid") != record["pid"]
            or identity.get("phase") != "ready"
            or identity.get("shutdown_requested") is not False
        ):
            raise RuntimeErrorBase("Readiness belongs to a different or stopped runtime")
        health = payload.get("health")
        observed_version = payload.get("version")
    else:
        health = request(record, "/api/health", token=bearer(config))
        observed_version = None
    if not isinstance(health, dict):
        raise RuntimeErrorBase("BSM readiness response lacks application health")
    checks = health.get("checks")
    if (
        health.get("ready") is not True
        or health.get("lifecycle") != "ready"
        or health.get("health") != "healthy"
        or not isinstance(checks, dict)
        or not {"lifecycle", "database", "state"}.issubset(checks)
        or not all(
            isinstance(item, dict) and item.get("healthy") is True
            for item in checks.values()
        )
    ):
        raise RuntimeErrorBase("BSM has not reached healthy application readiness")
    if "readiness_token" not in record:
        info = request(record, "/api/info").get("info")
        observed_version = info.get("app_version") if isinstance(info, dict) else None
    try:
        matches = Version(observed_version or "") == Version(expected_version)
    except (ValueError, TypeError):
        matches = False
    if not matches:
        raise RuntimeErrorBase("Running BSM version differs from selected installation")
    confirmed = control_record(config, SystemdService(config).status())
    if confirmed["instance_id"] != record["instance_id"]:
        raise RuntimeErrorBase("BSM runtime changed during readiness verification")

    return {
        "ready": True,
        "version": expected_version,
        "instance_id": record["instance_id"],
        "pid": record["pid"],
    }


def wait_ready(config: Config, expected_version: str) -> dict:
    """Wait within a fixed startup deadline; authentication errors fail immediately."""
    deadline = time.monotonic() + config.readiness_timeout
    last_reason = "Runtime identity record is unavailable"
    while True:
        try:
            return ready(config, expected_version)
        except AuthenticationError:
            raise
        except (OSError, ValueError, RuntimeErrorBase) as error:
            if isinstance(error, RuntimeErrorBase):
                last_reason = str(error)
            if time.monotonic() >= deadline:
                raise RuntimeErrorBase(
                    "BSM did not become healthy before the startup deadline: "
                    + last_reason
                ) from None
            time.sleep(0.2)


def shutdown(config: Config) -> None:
    """Request authenticated graceful exit and await an inactive service."""
    status = SystemdService(config).status()
    if status.get("ActiveState") == "inactive" and status.get("MainPID") == "0":
        return
    if status.get("ActiveState") != "active":
        raise RuntimeErrorBase("Cannot gracefully stop an unverified service state")
    record = control_record(config, status)
    receipt = request(record, "/api/runtime/shutdown", shutdown=True)
    if (
        receipt.get("status") != "accepted"
        or receipt.get("phase") != "stopping"
        or receipt.get("pid") != record["pid"]
        or receipt.get("instance_id") != record["instance_id"]
    ):
        raise RuntimeErrorBase("Shutdown receipt belongs to a different BSM runtime")
    deadline = time.monotonic() + config.service_timeout
    while True:
        state = SystemdService(config).status()
        if (
            state.get("ActiveState") == "inactive"
            and state.get("MainPID") == "0"
            and state.get("Job") == ""
        ):
            return
        if time.monotonic() >= deadline:
            raise RuntimeErrorBase("Graceful BSM shutdown exceeded its deadline")
        time.sleep(0.2)
