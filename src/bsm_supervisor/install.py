"""Install a published BSM version without touching the running service."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .config import Config
from .installation_permissions import create_root, service_readable
from .releases import INDEX, PACKAGE, Release, ReleaseError
from .store import Store


def run_command(arguments: list[str], log: Path, timeout: int = 900) -> None:
    """Log a command and terminate its process group on timeout or interruption."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON")) and key != "VIRTUAL_ENV"
    }
    environment["PIP_CONFIG_FILE"] = os.devnull
    with log.open("ab") as stream:
        with subprocess.Popen(
            arguments,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
            umask=0o022,
        ) as process:
            try:
                code = process.wait(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if isinstance(sys.exception(), subprocess.TimeoutExpired):
                    raise ReleaseError(f"Installation timed out; inspect {log}")
                raise
    if code:
        raise ReleaseError(f"Installation command failed; inspect {log}")


def install(config: Config, store: Store, release: Release) -> dict:
    """Create and verify an environment at its permanent path.

    Environments are never moved after venv creation because entry point
    shebangs contain absolute paths. Failed directories remain unregistered.
    """
    installation_id = "release-" + release.version
    with store.mutation("install:" + release.version) as operation_id:
        root = config.installations_dir.resolve()
        create_root(root)
        target = root / installation_id
        logs = config.state_dir / "logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        log = logs / (installation_id + ".log")
        if target.exists() or target.is_symlink():
            registered = next(
                (
                    item
                    for item in store.installations()
                    if item["id"] == installation_id
                ),
                None,
            )
            if registered is None:
                raise ReleaseError(
                    "An unregistered installation directory already exists; "
                    "inspect history and installation logs before removing it"
                )
            if (
                target.is_symlink()
                or registered["path"] != str(target)
                or registered["version"] != release.version
            ):
                raise ReleaseError("Registered installation path or version changed")
            manifest = json.loads((target / "supervisor-installation.json").read_text())
            if (
                not isinstance(manifest, dict)
                or manifest.get("source") != "pypi"
                or manifest.get("version") != release.version
                or manifest.get("path") != str(target)
                or manifest.get("index") != INDEX
                or not (target / "pyvenv.cfg").is_file()
                or not (target / "bin/bedrock-server-manager").is_file()
            ):
                raise ReleaseError("Existing installation metadata is invalid")
            service_readable(target)
            python = target / "bin/python"
            run_command([str(python), "-I", "-m", "pip", "check"], log)
            verification = (
                "from importlib.metadata import version; "
                f"assert version('{PACKAGE}') == {release.version!r}, "
                "'Version mismatch'"
            )
            run_command([str(python), "-I", "-c", verification], log)
            return {**manifest, "id": installation_id, "reused": True}
        target.mkdir(mode=0o755)  # Never overwrite an existing environment.
        target.chmod(0o755)
        store.note(operation_id, "Creating virtual environment: " + str(target))
        run_command([sys.executable, "-I", "-m", "venv", str(target)], log)
        python = target / "bin/python"
        store.note(operation_id, "Installing official PyPI release; log: " + str(log))
        run_command(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--index-url",
                INDEX,
                f"{PACKAGE}=={release.version}",
            ],
            log,
        )
        service_readable(target)
        run_command([str(python), "-I", "-m", "pip", "check"], log)
        verification = (
            "from importlib.metadata import version; "
            f"assert version('{PACKAGE}') == {release.version!r}, 'Version mismatch'"
        )
        run_command([str(python), "-I", "-c", verification], log)
        if not (target / "bin/bedrock-server-manager").is_file():
            raise ReleaseError("Installed package has no BSM entry point")
        manifest = {
            "version": release.version,
            "source": "pypi",
            "index": INDEX,
            "python": sys.version,
            "path": str(target),
        }
        with (target / "supervisor-installation.json").open("x") as stream:
            json.dump(manifest, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        store.add_installation(installation_id, release.version, target)
        return {**manifest, "id": installation_id, "reused": False}
