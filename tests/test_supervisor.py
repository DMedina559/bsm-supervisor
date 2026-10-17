"""Verify persistent state, mutation exclusion, and the service boundary."""

import contextlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor.cli import main
from bsm_supervisor.config import Config
from bsm_supervisor.integration import require_managed
from bsm_supervisor.service import (
    ServiceError,
    SystemdService,
    guard_command,
    managed_command,
)
from bsm_supervisor.service_environment import write_environment
from bsm_supervisor.store import BusyError, Store


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(
            self.root / "state", self.root / "installations", self.root / "data"
        )

    def environment(self, name="release"):
        path = self.config.installations_dir / name
        (path / "bin").mkdir(parents=True)
        (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (path / "bin/bedrock-server-manager").touch()
        return path

    def test_registration_survives_reopen_and_duplicate_is_journaled(self):
        store = Store(self.config.state_dir)
        path = self.environment()
        store.register("release", "4.0.0b7", path, self.config.installations_dir)
        reopened = Store(self.config.state_dir)
        self.assertEqual(reopened.installations()[0]["path"], str(path))
        with self.assertRaises(sqlite3.IntegrityError):
            reopened.register("release", "4.0.0b7", path, self.config.installations_dir)
        self.assertEqual(reopened.operations()[0]["state"], "failed")
        self.assertEqual(len(reopened.installations()), 1)

    def test_registration_rejects_escaped_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.config.installations_dir.mkdir()
        link = self.config.installations_dir / "escape"
        link.symlink_to(outside, target_is_directory=True)
        store = Store(self.config.state_dir)
        with self.assertRaisesRegex(ValueError, "inside"):
            store.register("escape", "1", link, self.config.installations_dir)
        self.assertEqual(store.installations(), [])

    def test_incomplete_environment_is_not_registered(self):
        path = self.config.installations_dir / "bad"
        path.mkdir(parents=True)
        store = Store(self.config.state_dir)
        with self.assertRaisesRegex(ValueError, "Missing"):
            store.register("bad", "1", path, self.config.installations_dir)

    def test_interrupted_operation_blocks_until_acknowledged(self):
        script = (
            "import os; from pathlib import Path; "
            "from bsm_supervisor.store import Store; "
            f"store = Store(Path({str(self.config.state_dir)!r})); "
            "store.begin('interrupted'); os._exit(9)"
        )
        result = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(result.returncode, 9)
        reopened = Store(self.config.state_dir)
        with self.assertRaises(BusyError):
            reopened.begin("second")
        reopened.acknowledge(reopened.operations()[0]["id"])
        with reopened.mutation("next"):
            pass
        self.assertEqual(reopened.operations()[0]["state"], "succeeded")

    def test_live_operation_cannot_be_acknowledged_or_overlapped(self):
        store = Store(self.config.state_dir)
        other = Store(self.config.state_dir)
        with store.mutation("live") as operation:
            with self.assertRaises(BusyError):
                other.acknowledge(operation)
            with self.assertRaises(BusyError):
                with other.mutation("overlap"):
                    self.fail("overlapping mutation entered")
        self.assertEqual(store.operations()[0]["state"], "succeeded")

    def test_uncertain_service_failure_blocks_further_mutations(self):
        store = Store(self.config.state_dir)
        with self.assertRaises(ServiceError):
            with store.mutation("service:start", preserve_on_error=True):
                raise ServiceError("timeout")
        with self.assertRaises(BusyError):
            store.begin("retry")

    def test_unknown_database_version_rejected(self):
        store = Store(self.config.state_dir)
        with store.connect() as db:
            db.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ValueError, "version"):
            Store(self.config.state_dir)

    def test_config_rejects_overlap_and_unsafe_unit(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            Config(self.root / "state", self.root / "state/envs", self.root / "data")
        with self.assertRaisesRegex(ValueError, "service"):
            Config(self.root / "s", self.root / "i", self.root / "d", "--bad.service")
        with self.assertRaisesRegex(ValueError, "absolute"):
            Config(Path("relative"), self.root / "i", self.root / "d")
        with self.assertRaisesRegex(ValueError, "integer"):
            Config(
                self.root / "s", self.root / "i", self.root / "d", service_timeout=True
            )

    def test_config_unknown_and_missing_fields(self):
        config = self.root / "config.toml"
        config.write_text('unexpected = "value"\n')
        with self.assertRaisesRegex(ValueError, "Unknown"):
            Config.load(config)
        config.write_text('state_dir = "/tmp/state"\n')
        with self.assertRaisesRegex(ValueError, "installations_dir"):
            Config.load(config)

    @patch("bsm_supervisor.service.subprocess.run")
    def test_inactive_service_status_and_argument_boundary(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\n", ""
        )
        service = SystemdService(self.config)
        self.assertEqual(service.status()["ActiveState"], "inactive")
        service.control("stop")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/systemctl",
                "--no-pager",
                "--no-ask-password",
                "stop",
                "bedrock-server-manager-webui.service",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 240)
        with self.assertRaises(ValueError):
            service.control("enable")

    @patch("bsm_supervisor.service.subprocess.run")
    def test_repeated_systemd_conditions_are_retained_and_validated(self, run):
        write_environment(self.config)
        recovery = (
            "{ path=/usr/bin/test ; argv[]=/usr/bin/test ! -e "
            f"{self.config.config_dir}/supervisor-recovery.required ; "
            "ignore_errors=no ; }"
        )
        guard = "{ argv[]=" + guard_command(self.config) + " ; ignore_errors=no ; }"
        launch = (
            "{ path=/usr/bin/flock ; argv[]="
            + managed_command(self.config)
            + " ; ignore_errors=no ; }"
        )
        for conditions in ((recovery, guard), (guard, recovery)):
            output = (
                "LoadState=loaded\nActiveState=inactive\nMainPID=0\nJob=\n"
                + "\n".join("ExecCondition=" + item for item in conditions)
                + "\nExecStart="
                + launch
                + "\nUnsetEnvironment=BSM_DB_URL BSM_DATA_DIR BSM_CONFIG_DIR "
                + "BSM_PLUGIN_RECURSION_GUARD BSM_LOG_LEVEL\n"
                + f"EnvironmentFiles={self.config.state_dir}/bsm-runtime.env "
                + "(ignore_errors=no)\n"
            )
            run.return_value = subprocess.CompletedProcess([], 0, output, "")
            state = SystemdService(self.config).status()
            self.assertIn(recovery, state["ExecCondition"])
            self.assertIn(guard, state["ExecCondition"])
            require_managed(self.config, state)
            for field, changed in (
                (
                    "EnvironmentFiles",
                    state["EnvironmentFiles"] + " /tmp/extra.env (ignore_errors=no)",
                ),
                ("UnsetEnvironment", state["UnsetEnvironment"] + " HOST"),
            ):
                with self.assertRaises(ValueError):
                    require_managed(self.config, {**state, field: changed})
            for missing in (recovery, guard):
                run.return_value = subprocess.CompletedProcess(
                    [], 0, output.replace("ExecCondition=" + missing + "\n", ""), ""
                )
                with self.assertRaisesRegex(ValueError, "managed service"):
                    require_managed(self.config, SystemdService(self.config).status())

    @patch("bsm_supervisor.service.subprocess.run")
    def test_missing_service_and_timeout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "LoadState=not-found", "")
        with self.assertRaisesRegex(ServiceError, "not installed"):
            SystemdService(self.config).status()
        run.side_effect = subprocess.TimeoutExpired("systemctl", 120)
        with self.assertRaisesRegex(ServiceError, "inspect"):
            SystemdService(self.config).control("start")

    def test_cli_persistence_without_systemd(self):
        config = self.root / "config.toml"
        config.write_text(
            f'state_dir = "{self.config.state_dir}"\n'
            f'installations_dir = "{self.config.installations_dir}"\n'
            f'data_dir = "{self.config.data_dir}"\n'
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--config", str(config), "check"]), 0)
            self.assertFalse(self.config.state_dir.exists())
            self.assertEqual(main(["--config", str(config), "init"]), 0)
            self.assertEqual(main(["--config", str(config), "installations"]), 0)
        self.assertIn("[]", output.getvalue())

    @patch("bsm_supervisor.cli.require_managed")
    @patch("bsm_supervisor.cli.SystemdService.status", return_value={})
    @patch("bsm_supervisor.cli.SystemdService.control")
    def test_cli_records_service_failure_and_returns_error(
        self, control, status, managed
    ):
        control.side_effect = ServiceError("permission denied")
        config = self.root / "config.toml"
        config.write_text(
            f'state_dir = "{self.config.state_dir}"\n'
            f'installations_dir = "{self.config.installations_dir}"\n'
            f'data_dir = "{self.config.data_dir}"\n'
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", str(config), "start"]), 1)
        self.assertEqual(
            Store(self.config.state_dir).operations()[0]["state"], "running"
        )


if __name__ == "__main__":
    unittest.main()
