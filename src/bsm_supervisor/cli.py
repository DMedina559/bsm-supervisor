"""Local operator commands for supervisor configuration and service state."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .activation import activate, selection, service_unit
from .config import Config
from .install import install
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
    installation = commands.add_parser("install")
    installation.add_argument("selector", help="stable, prerelease, or exact version")
    activation = commands.add_parser("activate")
    activation.add_argument("id")
    activation.add_argument(
        "--offline-confirmed",
        action="store_true",
        required=True,
        help="Confirm BSM is not running outside the configured unit",
    )
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
        if args.command == "init":
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
    except (
        OSError,
        ValueError,
        sqlite3.Error,
        ServiceError,
        BusyError,
        ReleaseError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
