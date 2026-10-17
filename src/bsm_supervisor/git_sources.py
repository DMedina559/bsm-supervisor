"""Explicit builds from the fixed, trusted BSM upstream repository."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.version import Version

from .checkpoint_files import write_json
from .config import Config
from .install import run_command
from .releases import INDEX, PACKAGE, ReleaseError
from .store import Store

UPSTREAM = "https://github.com/DMedina559/bedrock-server-manager.git"


def git(arguments: list[str], cwd: Path | None = None) -> str:
    """Run Git without user configuration, prompts, hooks, or local transports."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="https",
    )
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError("Git source operation failed") from exc
    return result.stdout.strip()


def validate_ref(kind: str, reference: str) -> str:
    """Accept an explicit branch, tag, or full immutable commit ID."""
    if kind == "commit":
        if not re.fullmatch(r"[0-9a-fA-F]{40}", reference):
            raise ValueError("Use a full 40-character Git commit ID")
        return reference.lower()
    if (
        kind not in ("branch", "tag")
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", reference)
        or any(part in reference for part in ("..", "//", "@{"))
    ):
        raise ValueError("Invalid Git reference")
    if reference.endswith(("/", ".", ".lock")):
        raise ValueError("Invalid Git reference")
    return "refs/" + ("heads/" if kind == "branch" else "tags/") + reference


def install_git(config: Config, store: Store, kind: str, reference: str) -> dict:
    """Resolve once, build that commit, and register a permanent environment."""
    requested = validate_ref(kind, reference)
    with store.mutation("install-git:" + kind + ":" + reference):
        builds = config.state_dir / "builds"
        builds.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=builds) as temporary:
            checkout = Path(temporary)
            git(["init", str(checkout)])
            git(["fetch", "--no-recurse-submodules", UPSTREAM, requested], checkout)
            commit = git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], checkout)
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ReleaseError("Git returned an invalid commit")
            if kind == "commit" and commit != requested:
                raise ReleaseError("Git commit did not match the requested revision")
            git(["checkout", "--detach", commit], checkout)
            # setuptools-scm needs release tags to compute the package version.
            git(["fetch", "--tags", "--no-recurse-submodules", UPSTREAM], checkout)
            target = config.installations_dir.resolve() / ("git-" + commit)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir(mode=0o755)
            logs = config.state_dir / "logs"
            logs.mkdir(mode=0o700, exist_ok=True)
            log = logs / (target.name + ".log")
            run_command([sys.executable, "-I", "-m", "venv", str(target)], log)
            python = str(target / "bin/python")
            run_command(
                [
                    python,
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--no-input",
                    "--no-cache-dir",
                    "--index-url",
                    INDEX,
                    str(checkout),
                ],
                log,
            )
            run_command([python, "-I", "-m", "pip", "check"], log)
            metadata = target / "verified-version.json"
            script = (
                "import json; from importlib.metadata import version; "
                f"open({str(metadata)!r}, 'x').write(json.dumps(version({PACKAGE!r})))"
            )
            run_command([python, "-I", "-c", script], log)
            version = str(Version(json.loads(metadata.read_text())))
            metadata.unlink()
            if not (target / "bin/bedrock-server-manager").is_file():
                raise ReleaseError("Installed package has no BSM entry point")
            manifest = dict(
                source="git",
                upstream=UPSTREAM,
                ref_kind=kind,
                reference=reference,
                commit=commit,
                version=version,
                path=str(target),
                python=sys.version,
            )
            write_json(target / "supervisor-installation.json", manifest)
            store.add_installation(target.name, version, target)
            return manifest
