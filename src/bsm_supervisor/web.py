"""Independent authenticated ASGI console using the existing operator CLI."""

import asyncio
import fcntl
import hmac
import json
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from packaging.version import InvalidVersion, Version

from . import (
    autostart,
    checkpoints,
    console_admin,
    console_transfers,
    events,
    scheduling,
    self_update,
)
from .activation import selection
from .checkpoint_files import read_json, write_json
from .config import Config
from .launchguard import boot_identity, controller_pid, process_generation
from .service import SystemdService
from .store import BusyError, Store
from .transfer import private_secret

ASSETS = Path(__file__).parent / "assets"
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript"),
    "/admin.js": ("admin.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/frontend/tokens.css": ("frontend/tokens.css", "text/css"),
    "/frontend/base.css": ("frontend/base.css", "text/css"),
    "/frontend/buttons.css": ("frontend/buttons.css", "text/css"),
    "/frontend/forms.css": ("frontend/forms.css", "text/css"),
    "/frontend/icon-192.png": ("frontend/icon-192.png", "image/png"),
    "/frontend/favicon-96.png": ("frontend/favicon-96.png", "image/png"),
}


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
    if isinstance(action, str) and action in console_admin.SCHEMAS:
        console_admin.validate(value)
        return ["console-task"]
    fields = {"action", "target", "confirmed", "accept_migrations", "restore_data"}
    if value.keys() - fields or not isinstance(action, str):
        raise ValueError("Invalid action fields")
    for flag in ("confirmed", "accept_migrations", "restore_data"):
        if flag in value and type(value[flag]) is not bool:
            raise ValueError("Confirmation fields must be booleans")
    if action == "auto-start":
        return [action]
    if action == "start":
        return [action]
    if action == "restart":
        if value.get("confirmed") is not True:
            raise ValueError("Confirm service downtime")
        return [action]
    if action == "installation-remove":
        target = value.get("target", "")
        if (
            not isinstance(target, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target)
            or value.get("confirmed") is not True
        ):
            raise ValueError("Confirm deletion of a registered installation")
        return [action, target, "--offline-confirmed", "--delete-confirmed"]
    if action == "restore-cleanup":
        target = value.get("target", "")
        if (
            not isinstance(target, str)
            or not target.isascii()
            or not target.isdecimal()
            or value.get("confirmed") is not True
        ):
            raise ValueError("Confirm deletion of retained restore originals")
        return [action, target, "--offline-confirmed", "--discard-originals"]
    if action in ("stop", "graceful-stop"):
        if value.get("confirmed") is not True:
            raise ValueError("Confirm service downtime")
        return [action]
    if action == "checkpoint-create":
        if value.get("confirmed") is not True:
            raise ValueError("Confirm BSM is stopped, including outside systemd")
        return [action, "--offline-confirmed"]
    target = value.get("target", "")
    if isinstance(target, str):
        target = target.strip()
    if not isinstance(target, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target
    ):
        raise ValueError(
            "Select a valid version, installation, checkpoint or operation"
        )
    if action in ("install", "upgrade"):
        if target.lower() in ("stable", "prerelease"):
            target = target.lower()
        else:
            try:
                target = str(Version(target))
            except InvalidVersion as exc:
                raise ValueError(
                    "Choose stable, prerelease, or an exact version such as 4.0.0b7. "
                    "Use Select while stopped for registered installation IDs."
                ) from exc
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
    if action in ("upgrade-cancel", "upgrade-restore") and target.isdecimal():
        if action == "upgrade-restore" and value.get("restore_data") is not True:
            raise ValueError("Confirm data replacement before upgrade restoration")
        return [
            "upgrade-recover",
            target,
            "--maintenance-confirmed",
            "--action",
            "cancel" if action == "upgrade-cancel" else "restore",
            *(["--restore-data-confirmed"] if action == "upgrade-restore" else []),
        ]
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
        self.transfer_lock = threading.Lock()
        self.auto_start = {
            "state": "waiting",
            "reason": "Waiting for console listener readiness",
        }
        self.lock = threading.Lock()
        self.jobs = config.state_dir / "web-jobs"
        self.jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        Store(config.state_dir)

    def snapshot(self) -> dict:
        if self.config_path.is_file():
            self.config = Config.load(self.config_path)
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
            "autostart": self.auto_start,
            "settings": console_admin.settings(self.config),
            "supervisor": self_update.information(),
            "transfers": console_transfers.listing(self.config),
            "selection": selection(self.config, store),
            "installations": store.installations(),
            "operations": store.operations(),
            "workflows": store.workflows(),
            "checkpoints": checkpoints.list_checkpoints(self.config),
            "schedules": scheduling.listing(self.config),
            "jobs": sorted(
                (read_json(path) for path in self.jobs.glob("*.json")),
                key=lambda job: job.get("created_at", ""),
                reverse=True,
            )[:50],
        }

    def submit(self, value: dict) -> dict:
        args = arguments(value)
        if not self.lock.acquire(blocking=False):
            raise BusyError("Another console action is running")
        try:
            if value["action"] not in console_admin.READ_ONLY and any(
                read_json(path).get("state") in ("running", "interrupted")
                for path in self.jobs.glob("*.json")
            ):
                raise BusyError(
                    "An interrupted console job requires web-job-acknowledge after "
                    "checking history and service state"
                )
            job = {
                "id": uuid4().hex,
                "action": value["action"],
                "state": "running",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            lease = os.open(
                self.jobs / (job["id"] + ".lock"),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                job.update(
                    controller_pid=controller_pid(),
                    controller_generation=process_generation(controller_pid()),
                    boot_id=boot_identity(),
                )
                if args == ["console-task"]:
                    write_json(self.jobs / (job["id"] + ".request"), value)
                    args = [*args, job["id"]]
                write_json(self.jobs / (job["id"] + ".json"), job)
                thread = threading.Thread(
                    target=self.execute, args=(dict(job), args, lease), daemon=False
                )
                thread.start()
            except BaseException:
                os.close(lease)
                raise
            return job
        except BaseException:
            self.lock.release()
            raise

    def execute(self, job: dict, args: list[str], lease: int) -> None:
        """Bound execution and retain a process lease until the child has exited."""
        process = None
        try:
            log = self.jobs / (job["id"] + ".log")
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                process = subprocess.Popen(
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
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(lease,),
                )
                job["child_pid"] = process.pid
                try:
                    job["child_generation"] = process_generation(process.pid)
                except (OSError, ValueError, IndexError):
                    job["child_generation"] = None
                write_json(self.jobs / (job["id"] + ".json"), job)
                assert process.stdout is not None
                remaining = 8 * 1024 * 1024
                deadline = time.monotonic() + self.config.web_job_timeout
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            if time.monotonic() >= deadline:
                                raise subprocess.TimeoutExpired(
                                    args, self.config.web_job_timeout
                                )
                            readable = selector.select(timeout=0.2)
                            if readable:
                                data = os.read(process.stdout.fileno(), 65536)
                                if not data:
                                    break
                                if remaining:
                                    stream.write(data[:remaining])
                                    remaining = max(0, remaining - len(data))
                            elif process.poll() is not None:
                                # Descendants holding stdout share the deadline.
                                continue
                    code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                finally:
                    process.stdout.close()
                if remaining == 0:
                    stream.write(b"\n[Console output truncated at 8 MiB]\n")
                stream.flush()
                os.fsync(stream.fileno())
            if job["action"] == "auto-start":
                report = read_json(log)
                self.auto_start = {"state": report["state"], "reason": report["reason"]}
            if code == 0 and log.stat().st_size <= 65536:
                try:
                    report = read_json(log)
                    if isinstance(report, dict):
                        job["result"] = report
                except (ValueError, OSError):
                    pass
            job.update(state="succeeded" if code == 0 else "failed", exit_code=code)
        except BaseException:
            if process is not None:
                try:
                    if job["action"] == "supervisor-update":
                        # Let the updater terminate its separately grouped installer.
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            job.update(
                state="interrupted",
                remedy="Inspect service status and operation history, "
                "recover, then acknowledge this job",
            )
        finally:
            (self.jobs / (job["id"] + ".request")).unlink(missing_ok=True)
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            try:
                write_json(self.jobs / (job["id"] + ".json"), job)
            finally:
                os.close(lease)
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
            elif method == "GET" and path in STATIC_ASSETS:
                name, media = STATIC_ASSETS[path]
                body = (ASSETS / name).read_bytes()
            elif path.startswith("/api/"):
                credential = headers.get("authorization", "").removeprefix("Bearer ")
                if not hmac.compare_digest(
                    credential.encode(), private_secret(self.token_file)
                ):
                    status = 401
                elif headers.get("origin") not in (None, self.origin):
                    status = 403
                elif path == "/api/transfers" and method == "POST":
                    if not self.transfer_lock.acquire(blocking=False):
                        raise BusyError("Another upload is running")
                    try:
                        body = json.dumps(
                            await console_transfers.upload(
                                self.config, headers, receive
                            )
                        ).encode()
                        status = 201
                    finally:
                        self.transfer_lock.release()
                elif (
                    re.fullmatch(r"/api/transfers/[0-9a-f]{32}", path)
                    and method == "GET"
                ):
                    await console_transfers.download(
                        self.config, path.rsplit("/", 1)[1], send, security
                    )
                    return
                elif (
                    re.fullmatch(r"/api/checkpoints/[0-9a-f]{32}", path)
                    and method == "GET"
                ):
                    body = json.dumps(
                        await asyncio.to_thread(
                            console_admin.checkpoint_summary,
                            self.config,
                            path.rsplit("/", 1)[1],
                        )
                    ).encode()
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
                elif (
                    path == "/api/actions"
                    or re.fullmatch(r"/api/jobs/[0-9a-f]{32}/acknowledge", path)
                ) and method == "POST":
                    if headers.get("content-type") != "application/json":
                        status = 415
                    else:
                        data = bytearray()
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            data.extend(message.get("body", b""))
                            if len(data) > 65536:
                                raise ValueError("Action request exceeds 65536 bytes")
                            if not message.get("more_body"):
                                break
                        value = json.loads(data)
                        if not isinstance(value, dict):
                            raise ValueError("Action request must be an object")
                        if path.endswith("/acknowledge"):
                            if value != {"confirmed": True}:
                                raise ValueError(
                                    "Confirm service and operation inspection"
                                )
                            await asyncio.to_thread(
                                acknowledge,
                                self.config,
                                Store(self.config.state_dir),
                                path.split("/")[3],
                            )
                            body = b'{"acknowledged":true}'
                        else:
                            body = json.dumps(
                                await asyncio.to_thread(self.submit, value)
                            ).encode()
                            status = 202
                else:
                    status = 404
            else:
                status = 404
        except BusyError:
            status, body = (
                409,
                b'{"error":"Another operation is running or needs recovery"}',
            )
        except (ValueError, KeyError, TimeoutError) as error:
            message = str(error) if isinstance(error, ValueError) else "Invalid request"
            # Only action validation messages are safe to expose directly.
            if (
                path != "/api/actions"
                and not path.endswith("/acknowledge")
                and path != "/api/transfers"
            ):
                message = "Invalid request"
            status, body = 400, json.dumps({"error": message}).encode()
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
    app = Console(config, config_path, token_file, origin)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            workers=1,
            proxy_headers=False,
            access_log=False,
            server_header=False,
            lifespan="off",
            limit_concurrency=32,
        )
    )
    asyncio.run(run_server(server, app))


async def start_after_ready(server, app: Console) -> None:
    """Wait for Uvicorn to bind its socket before attempting automatic startup."""
    while not server.started:
        if server.should_exit:
            return
        await asyncio.sleep(0.05)
    if not server.should_exit:
        report = await asyncio.to_thread(autostart.attempt, app)
        if app.auto_start.get("state") == "waiting":
            app.auto_start = report


async def run_server(server, app: Console) -> None:
    """Keep console readiness independent of BSM startup and recovery outcomes."""
    startup = asyncio.create_task(start_after_ready(server, app))
    try:
        await server.serve()
    finally:
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)


def acknowledge(config: Config, store: Store, job_id: str) -> None:
    """Clear console bookkeeping only after the operator separately recovers BSM."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid console job ID")
    path = config.state_dir / "web-jobs" / (job_id + ".json")
    lease_path = path.with_suffix(".lock")
    descriptor = os.open(lease_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusyError("Console job is still executing") from exc
        with store.lock():
            store.require_idle()
            job = read_json(path)
            if job["state"] not in ("running", "interrupted"):
                raise ValueError("Console job does not need acknowledgement")
            # Legacy jobs have no lease. Check their recorded process identity too.
            for prefix in ("controller", "child"):
                pid = job.get(prefix + "_pid")
                generation = job.get(prefix + "_generation")
                try:
                    alive = (
                        job.get("boot_id") == boot_identity()
                        and pid is not None
                        and generation is not None
                        and process_generation(pid) == generation
                    )
                except (OSError, ValueError, IndexError):
                    alive = False
                if alive and prefix == "child":
                    raise BusyError("Console child process is still running")
            path.with_suffix(".request").unlink(missing_ok=True)
            job["state"] = "acknowledged"
            write_json(path, job)
    finally:
        os.close(descriptor)


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
