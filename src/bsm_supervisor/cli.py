"""Local operator commands for supervisor configuration and service state."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import checkpoints, cleanup, restoration, runtime, upgrades
from .activation import activate, selection, service_unit
from .config import Config
from .git_sources import install_git
from .install import install
from .integration import inspect as inspect_integration
from .releases import ReleaseError, discover, select
from .service import ServiceError, SystemdService
from .store import BusyError, Store


def parser() -> argparse.ArgumentParser:
    """Build the command line interface."""
    root = argparse.ArgumentParser(prog="bsm-supervisor")
    root.add_argument(
        "--config", type=Path, default=Path("/etc/bsm-supervisor/config.toml")
    )
    commands = root.add_subparsers(dest="command", required=True)
    for name in (
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
        "start",
        "stop",
        "restart",
    ):
        commands.add_parser(name)
    releases = commands.add_parser("releases")
    releases.add_argument(
        "--channel", choices=("all", "stable", "prerelease"), default="all"
    )
    upgrade = commands.add_parser("upgrade")
    upgrade.add_argument("selector", help="stable, prerelease, or exact version")
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
        config = Config.load(args.config)
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
            print(json.dumps(SystemdService(config).status(), indent=2))
            return 0
        store = Store(config.state_dir)
        if args.command == "upgrades":
            print(json.dumps(store.workflows(), indent=2))
        elif args.command == "upgrade-status":
            print(json.dumps(upgrades.status(config, args.workflow_id), indent=2))
        elif args.command == "upgrade":
            print(
                json.dumps(
                    upgrades.upgrade(
                        config, store, args.selector, allow_same=args.allow_same_version
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
            with store.mutation("service:" + args.command, preserve_on_error=True):
                SystemdService(config).control(args.command)
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
        ServiceError,
        BusyError,
        ReleaseError,
        runtime.RuntimeErrorBase,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
