"""Local operator commands for supervisor configuration and service state."""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from . import (
    autostart,
    checkpoints,
    cleanup,
    console_admin,
    diagnostics,
    events,
    operations,
    restoration,
    retention,
    runtime,
    scheduling,
    self_update,
    transfer,
    upgrades,
    web,
)
from .activation import activate, selection, service_unit
from .config import Config
from .git_sources import install_git
from .install import install
from .integration import inspect as inspect_integration
from .releases import ReleaseError, discover, select
from .service import ServiceError, service_backend
from .service_environment import write_environment
from .setup import setup as setup_installation
from .store import BusyError, Store
from .unit_refresh import refresh


def parser() -> argparse.ArgumentParser:
    """Build the command line interface."""
    root = argparse.ArgumentParser(prog="bsm-supervisor")
    root.add_argument(
        "--config", type=Path, default=Path("/etc/bsm-supervisor/config.toml")
    )
    commands = root.add_subparsers(dest="command", required=True)
    for name in (
        "auto-start",
        "check",
        "integration",
        "checkpoints",
        "upgrades",
        "health",
        "graceful-stop",
        "init",
        "installations",
        "selection",
        "history",
        "status",
        "stop",
    ):
        commands.add_parser(name)
    for name in ("start", "restart"):
        command = commands.add_parser(name)
        command.add_argument(
            "--wait",
            action="store_true",
            help="Wait for authenticated application readiness",
        )
    for name in ("checkpoint-export", "checkpoint-import"):
        command = commands.add_parser(name)
        command.add_argument(
            "target", help="Checkpoint ID for export; archive path for import"
        )
        if name == "checkpoint-export":
            command.add_argument("destination", type=Path)
            command.add_argument("--require-mount", type=Path)
        else:
            command.add_argument(
                "--max-bytes", type=int, default=transfer.DEFAULT_LIMIT
            )
        command.add_argument("--passphrase-file", type=Path)
    task = commands.add_parser("console-task")
    task.add_argument("id")
    progress = commands.add_parser("events")
    progress.add_argument("--after", type=int, default=0)
    progress.add_argument("--follow", action="store_true")
    journal = commands.add_parser("journal-prune")
    journal.add_argument("--keep-days", type=int, default=30)
    journal.add_argument("--keep-jobs", type=int, default=100)
    journal.add_argument("--delete-confirmed", action="store_true", required=True)
    commands.add_parser("schedules")
    schedule_install = commands.add_parser("schedule-install")
    schedule_install.add_argument("policy", type=Path)
    for name in ("schedule-run", "schedule-remove", "schedule-acknowledge"):
        command = commands.add_parser(name)
        command.add_argument("name")
        if name != "schedule-run":
            command.add_argument(
                "--service-checked", action="store_true", required=True
            )
    notify = commands.add_parser("notifications-configure")
    notify.add_argument("file", type=Path)
    commands.add_parser("notifications-deliver")
    commands.add_parser("notifications-disable")
    token = commands.add_parser("web-token")
    token.add_argument("file", type=Path)
    console = commands.add_parser("web")
    console.add_argument("--token-file", type=Path, required=True)
    console.add_argument("--host", default="127.0.0.1")
    console.add_argument("--port", type=int, default=11326)
    console.add_argument("--origin", default="http://127.0.0.1:11326")
    console_unit = commands.add_parser("web-unit")
    console_unit.add_argument("--token-file", type=Path, required=True)
    console_unit.add_argument("--origin", default="http://127.0.0.1:11326")
    job_ack = commands.add_parser("web-job-acknowledge")
    job_ack.add_argument("id")
    job_ack.add_argument("--service-checked", action="store_true", required=True)
    commands.add_parser("version")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--no-health", action="store_true")
    setup = commands.add_parser("setup")
    setup.add_argument("selector", nargs="?")
    setup.add_argument("--installation")
    setup.add_argument("--user", default="bsm")
    setup.add_argument("--offline-confirmed", action="store_true")
    setup.add_argument("--no-start", action="store_true")
    setup.add_argument("--wait", action="store_true")
    updater = commands.add_parser("supervisor-update")
    updater.add_argument("--ref", default="latest")
    updater.add_argument("--source-dir", type=Path)
    updater.add_argument("--wait", action="store_true")
    updater.add_argument("--resume", action="store_true")
    updater.add_argument("--maintenance-confirmed", action="store_true", required=True)
    environment = commands.add_parser("service-environment")
    environment.add_argument("--offline-confirmed", action="store_true", required=True)
    refresh_unit = commands.add_parser("unit-refresh")
    refresh_unit.add_argument("--user", default="bsm")
    refresh_unit.add_argument("--offline-confirmed", action="store_true", required=True)
    releases = commands.add_parser("releases")
    releases.add_argument(
        "--channel", choices=("all", "stable", "prerelease"), default="all"
    )
    upgrade = commands.add_parser("upgrade")
    upgrade.add_argument(
        "selector", nargs="?", help="stable, prerelease, or exact version"
    )
    upgrade.add_argument("--installation", help="Registered Git installation ID")
    upgrade.add_argument("--maintenance-confirmed", action="store_true", required=True)
    upgrade.add_argument("--accept-migrations", action="store_true", required=True)
    upgrade.add_argument("--allow-same-version", action="store_true")
    recovery = commands.add_parser("upgrade-recover")
    recovery.add_argument("workflow_id", type=int)
    recovery.add_argument(
        "--action", choices=("resume", "restore", "cancel"), default="resume"
    )
    recovery.add_argument("--maintenance-confirmed", action="store_true", required=True)
    recovery.add_argument("--restore-data-confirmed", action="store_true")
    upgrade_status = commands.add_parser("upgrade-status")
    upgrade_status.add_argument("workflow_id", type=int)
    installation = commands.add_parser("install")
    installation.add_argument("selector", help="stable, prerelease, or exact version")
    git_install = commands.add_parser("install-git")
    refs = git_install.add_mutually_exclusive_group(required=True)
    for kind in ("branch", "tag", "commit"):
        refs.add_argument("--" + kind)
    git_install.add_argument("--trust-source-build", action="store_true", required=True)
    removal = commands.add_parser("installation-remove")
    removal.add_argument("id")
    removal.add_argument("--offline-confirmed", action="store_true", required=True)
    removal.add_argument("--delete-confirmed", action="store_true", required=True)
    activation = commands.add_parser("activate")
    activation.add_argument("id")
    activation.add_argument(
        "--offline-confirmed",
        action="store_true",
        required=True,
        help="Confirm BSM is not running outside the configured unit",
    )
    capture = commands.add_parser("checkpoint-create")
    capture.add_argument("--offline-confirmed", action="store_true", required=True)
    for name in ("checkpoint-verify", "restore-plan"):
        command = commands.add_parser(name)
        command.add_argument("id")
    restore = commands.add_parser("checkpoint-restore")
    restore.add_argument("id")
    restore.add_argument("--offline-confirmed", action="store_true", required=True)
    restore.add_argument("--restore-data-confirmed", action="store_true", required=True)
    for name in ("restore-recover", "restore-cleanup"):
        command = commands.add_parser(name)
        command.add_argument("operation_id", type=int)
        command.add_argument("--offline-confirmed", action="store_true", required=True)
        if name == "restore-cleanup":
            command.add_argument(
                "--discard-originals", action="store_true", required=True
            )
    pin = commands.add_parser("checkpoint-pin")
    pin.add_argument("id")
    pin.add_argument("--unpin", action="store_true")
    prune = commands.add_parser("checkpoint-prune")
    prune.add_argument("--keep", type=int, default=3)
    prune.add_argument("--delete-confirmed", action="store_true", required=True)
    unit = commands.add_parser("unit")
    unit.add_argument("--user", default="bsm")
    register = commands.add_parser("register")
    register.add_argument("id")
    register.add_argument("--version", required=True)
    register.add_argument("--path", required=True, type=Path)
    acknowledge = commands.add_parser("acknowledge")
    acknowledge.add_argument("operation_id", type=int)
    acknowledge.add_argument("--service-checked", action="store_true", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    """Execute an operator command and return a process exit code."""
    args = parser().parse_args(argv)
    try:
        if args.command == "version":
            print(json.dumps(self_update.information(), indent=2))
            return 0
        config = Config.load(args.config)
        if args.command == "events":
            cursor = args.after
            while True:
                page = events.read(config.state_dir, cursor)
                for event in page:
                    print(json.dumps(event), flush=True)
                    cursor = event["id"]
                if not args.follow:
                    break
                time.sleep(1)
            return 0
        if args.command == "web-token":
            web.token_create(args.file)
            print("Private console credential created")
            return 0
        if args.command == "web-unit":
            print(web.unit(args.config.resolve(), args.token_file, args.origin))
            return 0
        if args.command == "web":
            web.serve(
                config, args.config, args.token_file, args.host, args.port, args.origin
            )
            return 0
        if args.command == "schedules":
            print(json.dumps(scheduling.listing(config), indent=2))
            return 0
        if args.command == "doctor":
            report = diagnostics.doctor(config, not args.no_health)
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        if args.command == "check":
            print("Configuration valid")
            return 0
        if args.command == "integration":
            report = inspect_integration(config)
            print(json.dumps(report, indent=2))
            return 0 if report["supported"] else 1
        if args.command == "unit":
            print(service_unit(config, args.user), end="")
            return 0
        if args.command == "releases":
            releases = discover()
            if args.channel != "all":
                releases = [select(releases, args.channel)]
            print(json.dumps([release.to_dict() for release in releases], indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(service_backend(config).status(), indent=2))
            return 0
        store = Store(config.state_dir)
        if args.command == "console-task":
            print(
                json.dumps(
                    console_admin.execute_request(config, args.config, store, args.id),
                    indent=2,
                )
            )
        elif args.command == "auto-start":
            print(json.dumps(autostart.start(config, store), indent=2))
        elif args.command == "checkpoint-export":
            print(
                json.dumps(
                    transfer.export_checkpoint(
                        config,
                        store,
                        args.target,
                        args.destination,
                        args.passphrase_file,
                        args.require_mount,
                    ),
                    indent=2,
                )
            )
        elif args.command == "checkpoint-import":
            print(
                json.dumps(
                    transfer.import_checkpoint(
                        config,
                        store,
                        Path(args.target),
                        args.passphrase_file,
                        args.max_bytes,
                    ),
                    indent=2,
                )
            )
        elif args.command == "journal-prune":
            print(
                json.dumps(
                    retention.prune(config, store, args.keep_days, args.keep_jobs),
                    indent=2,
                )
            )
        elif args.command == "schedule-install":
            print(
                json.dumps(
                    scheduling.install(config, args.config, store, args.policy),
                    indent=2,
                )
            )
        elif args.command == "schedule-run":
            print(json.dumps(scheduling.run(config, store, args.name), indent=2))
        elif args.command == "schedule-remove":
            scheduling.remove(config, store, args.name)
            print("Schedule removed")
        elif args.command == "schedule-acknowledge":
            scheduling.acknowledge(config, store, args.name)
            print("Schedule bookkeeping acknowledged; no service recovery performed")
        elif args.command == "notifications-configure":
            with store.mutation("notifications:configure"):
                events.configure(config.state_dir, args.file)
            print("Private notification configuration written")
        elif args.command == "notifications-disable":
            with store.mutation("notifications:disable"):
                (config.state_dir / "notifications.json").unlink(missing_ok=True)
            print("Notifications disabled")
        elif args.command == "notifications-deliver":
            print(json.dumps(events.deliver(config.state_dir), indent=2))
        elif args.command == "web-job-acknowledge":
            web.acknowledge(config, store, args.id)
            print("Console job acknowledged; no service recovery performed")
        elif args.command == "setup":
            offline = args.offline_confirmed
            result = setup_installation(
                config,
                store,
                args.selector,
                args.installation,
                args.user,
                offline,
                not args.no_start,
                args.wait,
            )
            print(json.dumps(result, indent=2))
        elif args.command == "supervisor-update":
            print(
                json.dumps(
                    self_update.update(
                        config,
                        args.config,
                        store,
                        args.ref,
                        args.source_dir,
                        args.wait,
                        args.resume,
                    ),
                    indent=2,
                )
            )
        elif args.command == "service-environment":
            with store.mutation("service:environment"):
                try:
                    state = service_backend(config).status()
                except ServiceError as error:
                    if str(error) != f"Service is not installed: {config.service_unit}":
                        raise
                else:
                    if (
                        state.get("ActiveState") != "inactive"
                        or state.get("MainPID") != "0"
                        or state.get("Job")
                    ):
                        raise ValueError(
                            "Stop the managed service before rendering its environment"
                        )
                write_environment(config)
            print("Private managed service environment written")
        elif args.command == "unit-refresh":
            refresh(config, store, args.user)
            print("Managed unit and environment refreshed; BSM remains stopped")
        elif args.command == "upgrades":
            print(json.dumps(store.workflows(), indent=2))
        elif args.command == "upgrade-status":
            print(json.dumps(upgrades.status(config, args.workflow_id), indent=2))
        elif args.command == "upgrade":
            print(
                json.dumps(
                    upgrades.upgrade(
                        config,
                        store,
                        args.selector,
                        allow_same=args.allow_same_version,
                        installation_id=args.installation,
                    ),
                    indent=2,
                )
            )
        elif args.command == "upgrade-recover":
            if args.action == "restore" and not args.restore_data_confirmed:
                raise ValueError("Data restoration requires --restore-data-confirmed")
            print(
                json.dumps(
                    upgrades.recover(config, store, args.workflow_id, args.action),
                    indent=2,
                )
            )
        elif args.command == "health":
            current = selection(config, store)
            if current is None:
                raise ValueError(
                    "Select a registered installation before checking readiness"
                )
            print(json.dumps(runtime.ready(config, current["version"]), indent=2))
        elif args.command == "graceful-stop":
            with store.mutation("service:graceful-stop", preserve_on_error=True):
                runtime.shutdown(config)
            print("BSM stopped gracefully")
        elif args.command == "checkpoints":
            print(json.dumps(checkpoints.list_checkpoints(config), indent=2))
        elif args.command == "checkpoint-create":
            manifest = checkpoints.create(config, store)
            print(
                json.dumps(
                    {
                        "id": manifest["id"],
                        "created_at": manifest["created_at"],
                        "total_bytes": manifest["total_bytes"],
                    },
                    indent=2,
                )
            )
        elif args.command in ("checkpoint-verify", "restore-plan"):
            manifest = checkpoints.verify(config, args.id)
            print(
                json.dumps(
                    {
                        "id": manifest["id"],
                        "created_at": manifest["created_at"],
                        "roots": [record["path"] for record in manifest["roots"]],
                        "installation": manifest["installation"],
                        "schema_revision": manifest["schema_revision"],
                        "total_bytes": manifest["total_bytes"],
                        "changes_version_selection": False,
                        "effect": (
                            "Replace saved root contents; discard newer data on restore"
                        ),
                    },
                    indent=2,
                )
            )
        elif args.command == "checkpoint-restore":
            print(json.dumps(restoration.restore(config, store, args.id), indent=2))
        elif args.command == "restore-recover":
            print(restoration.recover(config, store, args.operation_id))
        elif args.command == "restore-cleanup":
            restoration.cleanup(config, store, args.operation_id)
            print("Retained originals discarded")
        elif args.command == "checkpoint-pin":
            checkpoints.pin(config, store, args.id, not args.unpin)
            print("Checkpoint pin updated")
        elif args.command == "checkpoint-prune":
            print(json.dumps(checkpoints.prune(config, store, args.keep), indent=2))
        elif args.command == "init":
            print("Supervisor state initialized")
        elif args.command == "installations":
            print(json.dumps(store.installations(), indent=2))
        elif args.command == "selection":
            print(json.dumps(selection(config, store), indent=2))
        elif args.command == "history":
            print(json.dumps(store.operations(), indent=2))
        elif args.command == "install":
            release = select(discover(), args.selector)
            print(json.dumps(install(config, store, release), indent=2))
        elif args.command == "install-git":
            kind = next(k for k in ("branch", "tag", "commit") if getattr(args, k))
            print(
                json.dumps(
                    install_git(config, store, kind, getattr(args, kind)), indent=2
                )
            )
        elif args.command == "installation-remove":
            cleanup.remove(config, store, args.id)
            print("Installation removed")
        elif args.command == "activate":
            activate(config, store, args.id)
            print("Installation selected; BSM remains stopped and no data was restored")
        elif args.command == "register":
            store.register(args.id, args.version, args.path, config.installations_dir)
            print(f"Registered {args.id}; service configuration was not changed")
        elif args.command == "acknowledge":
            store.acknowledge(args.operation_id)
            print("Interruption acknowledged; no rollback performed")
        else:
            wait = getattr(args, "wait", False)
            operations.control(config, store, args.command, wait)
            if args.command in ("start", "restart"):
                print(
                    "BSM application ready"
                    if wait
                    else "BSM process running; use health to verify "
                    "application readiness"
                )
            else:
                print(f"Service {args.command} completed")
        return 0
    except KeyboardInterrupt:
        print(
            "Interrupted; inspect upgrades and history before continuing",
            file=sys.stderr,
        )
        return 130
    except (
        OSError,
        ValueError,
        sqlite3.Error,
        zipfile.BadZipFile,
        KeyError,
        ServiceError,
        BusyError,
        ReleaseError,
        runtime.RuntimeErrorBase,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
