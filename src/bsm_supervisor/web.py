"""Independent authenticated ASGI console using the existing operator CLI."""

import asyncio
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from . import checkpoints, events, scheduling
from .activation import selection
from .checkpoint_files import read_json, write_json
from .config import Config
from .service import SystemdService
from .store import Store
from .transfer import private_secret

ASSETS = Path(__file__).parent / "assets"


def token_create(path: Path) -> None:
    """Create a private console credential without printing it or replacing files."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        stream.write(secrets.token_urlsafe(48) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def arguments(value: dict) -> list[str]:
    """Translate typed, allowlisted requests into CLI arguments without a shell."""
    action = value.get("action")
    fields = {"action", "target", "confirmed", "accept_migrations", "restore_data"}
    if value.keys() - fields or not isinstance(action, str):
        raise ValueError("Invalid action fields")
    for flag in ("confirmed", "accept_migrations", "restore_data"):
        if flag in value and type(value[flag]) is not bool:
            raise ValueError("Confirmation fields must be booleans")
    if action == "start":
        return [action, "--wait"]
    if action == "restart":
        if value.get("confirmed") is not True:
            raise ValueError("Confirm service downtime")
        return [action, "--wait"]
    if action in ("stop", "graceful-stop"):
        if value.get("confirmed") is not True:
            raise ValueError("Confirm service downtime")
        return [action]
    if action == "checkpoint-create":
        if value.get("confirmed") is not True:
            raise ValueError("Confirm BSM is stopped, including outside systemd")
        return [action, "--offline-confirmed"]
    target = value.get("target", "")
    if not isinstance(target, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target
    ):
        raise ValueError(
            "Select a valid version, installation, checkpoint or operation"
        )
    if action in ("install", "checkpoint-verify", "restore-plan"):
        return [action, target]
    if value.get("confirmed") is not True:
        raise ValueError("Explicit confirmation is required")
    if action == "activate":
        return [action, target, "--offline-confirmed"]
    if action == "upgrade" and value.get("accept_migrations") is True:
        return [action, target, "--maintenance-confirmed", "--accept-migrations"]
    if action == "checkpoint-restore" and value.get("restore_data") is True:
        return [action, target, "--offline-confirmed", "--restore-data-confirmed"]
    if action == "acknowledge" and target.isdecimal():
        return [action, target, "--service-checked"]
    if action == "upgrade-resume" and target.isdecimal():
        return [
            "upgrade-recover",
            target,
            "--maintenance-confirmed",
            "--action",
            "resume",
        ]
    if action == "restore-recover" and target.isdecimal():
        return [action, target, "--offline-confirmed"]
    raise ValueError("Unsupported action or missing migration/data confirmation")


class Console:
    """A single-worker control surface that remains available while BSM is down."""

    def __init__(
        self, config: Config, config_path: Path, token_file: Path, origin: str
    ):
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("Origin must be an exact HTTP(S) origin without a path")
        private_secret(token_file)
        self.config = config
        self.config_path = config_path.resolve()
        self.token_file = token_file
        self.origin = origin
        self.lock = threading.Lock()
        self.jobs = config.state_dir / "web-jobs"
        self.jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        Store(config.state_dir)

    def snapshot(self) -> dict:
        store = Store(self.config.state_dir)
        try:
            status: dict = SystemdService(self.config).status()
            status = {
                key: status.get(key)
                for key in ("ActiveState", "SubState", "MainPID", "Result")
            }
        except (OSError, RuntimeError) as exc:
            status = {"error": type(exc).__name__}
        return {
            "service": status,
            "selection": selection(self.config, store),
            "installations": store.installations(),
            "operations": store.operations(),
            "workflows": store.workflows(),
            "checkpoints": checkpoints.list_checkpoints(self.config),
            "schedules": scheduling.listing(self.config),
            "jobs": [
                read_json(path) for path in sorted(self.jobs.glob("*.json"))[-50:]
            ],
        }

    def submit(self, value: dict) -> dict:
        args = arguments(value)
        if not self.lock.acquire(blocking=False):
            raise ValueError("Another console action is running")
        try:
            if any(
                read_json(path).get("state") == "running"
                for path in self.jobs.glob("*.json")
            ):
                raise ValueError(
                    "An interrupted console job requires web-job-acknowledge after "
                    "checking history and service state"
                )
            job = {
                "id": uuid4().hex,
                "action": value["action"],
                "state": "running",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            write_json(self.jobs / (job["id"] + ".json"), job)
            thread = threading.Thread(
                target=self.execute, args=(dict(job), args), daemon=False
            )
            thread.start()
            return job
        except BaseException:
            self.lock.release()
            raise

    def execute(self, job: dict, args: list[str]) -> None:
        """Keep output private and durable; surface exit status without "
        "error secrets."""
        try:
            log = self.jobs / (job["id"] + ".log")
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "bsm_supervisor",
                        "--config",
                        str(self.config_path),
                        *args,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            job.update(
                state="succeeded" if result.returncode == 0 else "failed",
                exit_code=result.returncode,
            )
        except Exception:
            job.update(
                state="failed",
                remedy="Inspect the private job log and operation history",
            )
        finally:
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            try:
                write_json(self.jobs / (job["id"] + ".json"), job)
            finally:
                self.lock.release()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        headers = {
            key.decode().lower(): value.decode() for key, value in scope["headers"]
        }
        path, method = scope["path"], scope["method"]
        status, media, body = 200, "application/json", b"{}"
        security = [
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (
                b"content-security-policy",
                b"default-src 'self'; frame-ancestors 'none'; "
                b"base-uri 'none'; form-action 'self'",
            ),
        ]
        try:
            if headers.get("host", "").lower() != urlsplit(self.origin).netloc.lower():
                status = 400
            elif path in ("/", "/app.js", "/style.css") and method == "GET":
                name, media = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/style.css": ("style.css", "text/css"),
                }[path]
                body = (ASSETS / name).read_bytes()
            elif path.startswith("/api/"):
                credential = headers.get("authorization", "").removeprefix("Bearer ")
                if not hmac.compare_digest(
                    credential.encode(), private_secret(self.token_file)
                ):
                    status = 401
                elif headers.get("origin") not in (None, self.origin):
                    status = 403
                elif path == "/api/state" and method == "GET":
                    body = json.dumps(await asyncio.to_thread(self.snapshot)).encode()
                elif path == "/api/events" and method == "GET":
                    query = scope.get("query_string", b"").decode()
                    if query and not re.fullmatch(r"after=[0-9]{1,15}", query):
                        raise ValueError("Invalid event cursor")
                    after = int(query[6:]) if query else 0
                    body = json.dumps(
                        await asyncio.to_thread(
                            events.read, self.config.state_dir, after
                        )
                    ).encode()
                elif re.fullmatch(r"/api/jobs/[0-9a-f]{32}", path) and method == "GET":
                    log = self.jobs / (path.rsplit("/", 1)[1] + ".log")
                    with log.open("rb") as stream:
                        stream.seek(max(0, log.stat().st_size - 32768))
                        body = json.dumps(
                            {
                                "output": stream.read(32768).decode(
                                    "utf-8", errors="replace"
                                )
                            }
                        ).encode()
                elif path == "/api/actions" and method == "POST":
                    if headers.get("content-type") != "application/json":
                        status = 415
                    else:
                        data = bytearray()
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            data.extend(message.get("body", b""))
                            if len(data) > 8192:
                                raise ValueError("Action request exceeds 8192 bytes")
                            if not message.get("more_body"):
                                break
                        value = json.loads(data)
                        if not isinstance(value, dict):
                            raise ValueError("Action request must be an object")
                        body = json.dumps(
                            await asyncio.to_thread(self.submit, value)
                        ).encode()
                        status = 202
                else:
                    status = 404
            else:
                status = 404
        except (ValueError, KeyError):
            status, body = (
                400,
                b'{"error":"Action blocked; inspect operation history"}',
            )
        except Exception:
            status, body = (
                503,
                b'{"error":"Console unavailable; inspect the journal"}',
            )
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", media.encode()), *security],
            }
        )
        await send({"type": "http.response.body", "body": body})


def serve(
    config: Config,
    config_path: Path,
    token_file: Path,
    host: str,
    port: int,
    origin: str,
) -> None:
    """Serve through Uvicorn, never trusting forwarded identity headers."""
    try:
        import uvicorn
    except ImportError as exc:
        raise ValueError("The web console requires the web extra") from exc
    if host not in ("127.0.0.1", "::1") and not origin.startswith("https://"):
        raise ValueError(
            "A remote listener requires an explicit HTTPS reverse-proxy origin"
        )
    uvicorn.run(
        Console(config, config_path, token_file, origin),
        host=host,
        port=port,
        workers=1,
        proxy_headers=False,
        access_log=False,
        server_header=False,
        lifespan="off",
        limit_concurrency=32,
    )


def acknowledge(config: Config, store: Store, job_id: str) -> None:
    """Clear console bookkeeping only after the operator separately recovers BSM."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid console job ID")
    with store.lock():
        store.require_idle()
        path = config.state_dir / "web-jobs" / (job_id + ".json")
        job = read_json(path)
        if job["state"] != "running":
            raise ValueError("Console job is not interrupted")
        job["state"] = "acknowledged"
        write_json(path, job)


def unit(config_path: Path, token_file: Path, origin: str) -> str:
    """Render a separate console unit; BSM downtime does not stop the console."""
    if not config_path.is_absolute() or not token_file.is_absolute():
        raise ValueError("Console configuration and token paths must be absolute")
    from .scheduling import _quote

    command = " ".join(
        _quote(arg)
        for arg in (
            "/usr/bin/bsm-supervisor",
            "--config",
            str(config_path),
            "web",
            "--token-file",
            str(token_file),
            "--origin",
            origin,
        )
    )
    return f"""[Unit]
Description=BSM Supervisor independent console
After=network.target
[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=5
UMask=0077
KillMode=control-group
TimeoutStopSec=240
[Install]
WantedBy=multi-user.target
"""
