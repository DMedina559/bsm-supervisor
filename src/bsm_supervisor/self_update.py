"""Update the supervisor from its fixed repository with durable progress."""

import fcntl
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import runtime
from .checkpoint_files import read_json, write_json
from .config import Config
from .service import SystemdService
from .store import BusyError, Store

PREFIX = Path("/opt/bsm-supervisor")
API = "https://api.github.com/repos/DMedina559/bsm-supervisor"


class UpdateError(ValueError):
    """Supervisor installation or managed-service refresh did not complete."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def information() -> dict:
    """Return package and installed source revision without application state."""
    path = Path(sys.prefix) / "supervisor-installation.json"
    record = (
        read_json(path)
        if path.is_file()
        else {"revision": None, "source": "unrecorded"}
    )
    return {"version": version("bsm-supervisor"), "environment": sys.prefix, **record}


def fetch(path: str, token: str, limit: int) -> bytes:
    """Read only a fixed GitHub API resource without forwarding credentials."""
    headers = {
        "Accept": (
            "application/vnd.github.raw+json"
            if path.startswith("/contents/")
            else "application/vnd.github+json"
        )
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(API + path, headers=headers), timeout=30) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise UpdateError("Supervisor download exceeds the size limit")
        return data
    except HTTPError as error:
        status = error.code
        error.close()
        raise UpdateError(
            f"GitHub download failed (HTTP {status}); check repository read access"
        ) from None
    except (OSError, URLError):
        raise UpdateError(
            "Supervisor download failed; check network and repository access"
        ) from None


def execute(
    arguments: list[str], *, timeout: int, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Stream progress and terminate the entire child group on interruption."""
    with subprocess.Popen(arguments, env=env, start_new_session=True) as process:
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    return subprocess.CompletedProcess(arguments, code)


def installed_environment() -> Path:
    """Verify the published environment before taking BSM down."""
    environment = (PREFIX / "current").resolve(strict=True)
    executable = environment / "bin/bsm-supervisor"
    if (
        (PREFIX / "versions").resolve() not in environment.parents
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise UpdateError("Installed supervisor environment is not usable")
    return environment


def _update(
    config: Config,
    config_path: Path,
    store: Store,
    ref: str,
    source_dir: Path | None = None,
    wait: bool = False,
    resume: bool = False,
) -> dict:
    """Install a new supervisor, gracefully stop BSM, refresh and resume service."""
    if os.geteuid() != 0:
        raise UpdateError("supervisor-update requires root inside the container")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref) or ".." in ref:
        raise UpdateError("Invalid supervisor source reference")
    progress = config.state_dir / "supervisor-update.json"
    token = ""
    with store.lock():
        with store.mutation("supervisor:update-preflight"):
            pass
        if resume:
            plan = read_json(progress)
            if (
                plan.get("phase") == "completed"
                or plan.get("config") != str(config_path.resolve())
                or plan.get("scope")
                != [
                    str(config.data_dir),
                    str(config.config_dir),
                    str(config.installations_dir),
                    config.service_unit,
                ]
            ):
                raise UpdateError("No matching supervisor update needs resuming")
        else:
            if progress.exists() and read_json(progress).get("phase") != "completed":
                raise UpdateError(
                    "An update needs inspection; use supervisor-update --resume"
                )
            state = SystemdService(config).status()
            if state.get("Description") != "Bedrock Server Manager (BSM Supervisor)":
                raise UpdateError(
                    "Supervisor updates require an existing supervisor-managed BSM unit"
                )
            if state.get("ActiveState") not in (
                "active",
                "inactive",
                "failed",
            ) or state.get("Job"):
                raise UpdateError(
                    "Wait for the BSM service job to finish before updating"
                )
            if state.get("ActiveState") != "active" and state.get("MainPID") != "0":
                raise UpdateError("The stopped service still has a process")
            user = state.get("User") or "bsm"
            plan = {
                "phase": "preparing",
                "config": str(config_path.resolve()),
                "previous_environment": str((PREFIX / "current").resolve()),
                "was_running": state["ActiveState"] == "active",
                "user": user,
                "ref": ref,
                "revision": None,
                "source_dir": str(source_dir.resolve()) if source_dir else None,
                "scope": [
                    str(config.data_dir),
                    str(config.config_dir),
                    str(config.installations_dir),
                    config.service_unit,
                ],
            }
            write_json(progress, plan)
        if plan.get("phase") == "preparing":
            ref = plan["ref"]
            source_dir = Path(plan["source_dir"]) if plan.get("source_dir") else None
            if source_dir is None:
                token = os.environ.get("GH_TOKEN", "")
                if not token and sys.stdin.isatty():
                    token = getpass.getpass(
                        "GitHub token with repository read access: "
                    )
                if token and not re.fullmatch(r"[A-Za-z0-9_]+", token):
                    raise UpdateError("Invalid GitHub token format")
                metadata = json.loads(
                    fetch(
                        "/commits?sha="
                        + quote(plan.get("revision") or ref, safe="")
                        + "&per_page=1",
                        token,
                        65536,
                    )
                )
                revision = (
                    metadata[0].get("sha")
                    if isinstance(metadata, list) and metadata
                    else None
                )
                if not isinstance(revision, str) or not re.fullmatch(
                    r"[0-9a-f]{40}", revision
                ):
                    raise UpdateError(
                        "GitHub did not return an exact supervisor revision"
                    )
                plan["revision"] = revision
                write_json(progress, plan)
                installer = fetch(
                    "/contents/scripts/install-lxc.sh?ref=" + revision, token, 262144
                )
            else:
                source_dir = source_dir.resolve(strict=True)
                installer = (source_dir / "scripts/install-lxc.sh").read_bytes()
            with tempfile.TemporaryDirectory(prefix="bsm-update-") as temporary:
                script = Path(temporary) / "install-lxc.sh"
                script.write_bytes(installer)
                arguments = [
                    "/bin/bash",
                    str(script),
                    "--ref",
                    plan["revision"] or ref,
                    "--config",
                    str(config_path.resolve()),
                ]
                if source_dir is not None:
                    arguments.extend(["--source-dir", str(source_dir)])
                environment = dict(os.environ)
                if token:
                    environment["GH_TOKEN"] = token
                with store.mutation("supervisor:install"):
                    result = execute(arguments, env=environment, timeout=1800)
                    if result.returncode:
                        raise UpdateError(
                            "Supervisor installer failed; inspect its output "
                            "before resuming"
                        )
            target = installed_environment()
            if plan["revision"] is not None:
                provenance = read_json(target / "supervisor-installation.json")
                if provenance.get("revision") != plan["revision"]:
                    raise UpdateError(
                        "Installed supervisor revision differs from the pinned source"
                    )
            plan["phase"] = "installed"
            plan["environment"] = str(target)
            write_json(progress, plan)
        if plan["was_running"]:
            with store.mutation("supervisor:graceful-stop", preserve_on_error=True):
                state = SystemdService(config).status()
                if state.get("ActiveState") == "failed" and state.get("MainPID") == "0":
                    SystemdService(config).reset_failed()
                runtime.shutdown(config)
        plan["phase"] = "refreshing"
        write_json(progress, plan)
    # The new interpreter owns the refresh journal and renders its own launch guard.
    executable = Path(plan["environment"]) / "bin/bsm-supervisor"
    if (
        not executable.is_file()
        or (PREFIX / "versions").resolve() not in executable.resolve().parents
    ):
        raise UpdateError(
            "Installed supervisor environment changed; inspect update state"
        )
    commands = [["unit-refresh", "--user", plan["user"], "--offline-confirmed"]]
    if plan["was_running"]:
        commands.append(["start", *(["--wait"] if wait else [])])
    for command in commands:
        result = execute(
            [str(executable), "--config", str(config_path.resolve()), *command],
            timeout=6 * config.service_timeout + config.readiness_timeout + 30,
        )
        if result.returncode:
            raise UpdateError(
                "Supervisor updated but service refresh/start failed; "
                "inspect history and doctor, then resume"
            )
    plan["phase"] = "completed"
    write_json(progress, plan)
    return plan


@contextmanager
def update_lock(config: Config) -> Iterator[None]:
    """Serialize the whole update, including child processes using the new CLI."""
    descriptor = os.open(
        config.state_dir / "supervisor-update.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "rb") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BusyError("Another supervisor update is running") from error
        yield


def update(
    config: Config,
    config_path: Path,
    store: Store,
    ref: str,
    source_dir: Path | None = None,
    wait: bool = False,
    resume: bool = False,
) -> dict:
    """Serialize source installation, service refresh, and optional restart."""
    with update_lock(config):
        return _update(config, config_path, store, ref, source_dir, wait, resume)
