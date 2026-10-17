"""Exercise operator setup failures without modifying the host system."""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor.activation import service_unit
from bsm_supervisor.cli import main
from bsm_supervisor.config import Config
from bsm_supervisor.service import ServiceError
from bsm_supervisor.store import Store
from bsm_supervisor.unit_refresh import refresh


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(
            self.root / "state", self.root / "envs", self.root / "data"
        )
        self.store = Store(self.config.state_dir)
        self.config_file = self.root / "config.toml"
        self.config_file.write_text(
            f'state_dir = "{self.config.state_dir}"\n'
            f'installations_dir = "{self.config.installations_dir}"\n'
            f'data_dir = "{self.config.data_dir}"\n'
        )
        self.units = self.root / "units"
        self.units.mkdir()
        self.unit = self.units / self.config.service_unit
        self.original = service_unit(self.config, "bsm")
        self.unit.write_text(self.original)
        self.state = {"ActiveState": "inactive", "MainPID": "0", "Job": ""}

    def test_missing_selection_does_not_control_service_or_block_operations(self):
        error = io.StringIO()
        with (
            patch("bsm_supervisor.config.overrides", return_value={}),
            patch("bsm_supervisor.operations.service_backend") as service,
            contextlib.redirect_stderr(error),
        ):
            self.assertEqual(main(["--config", str(self.config_file), "start"]), 1)
        service.return_value.control.assert_not_called()
        self.assertIn("No BSM installation selected", error.getvalue())
        self.assertEqual(self.store.operations(), [])

    def test_immediate_start_failure_is_reported_and_retains_journal(self):
        with (
            patch("bsm_supervisor.config.overrides", return_value={}),
            patch(
                "bsm_supervisor.operations.selection", return_value={"version": "4.0"}
            ),
            patch("bsm_supervisor.operations.require_managed"),
            patch("bsm_supervisor.operations.service_backend") as service,
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            service.return_value.status.return_value = {
                "ActiveState": "activating",
                "MainPID": "0",
            }
            self.assertEqual(main(["--config", str(self.config_file), "start"]), 1)
        self.assertIn("did not remain running", error.getvalue())
        self.assertEqual(self.store.operations()[0]["state"], "running")

    def run_refresh(self, state=None, verify_code=0):
        with (
            patch("bsm_supervisor.unit_refresh.UNIT_DIRECTORY", self.units),
            patch("bsm_supervisor.unit_refresh.os.geteuid", return_value=0),
            patch("bsm_supervisor.unit_refresh.SystemdService") as service,
            patch("bsm_supervisor.unit_refresh.subprocess.run") as verify,
        ):
            service.return_value.status.return_value = state or self.state
            verify.return_value = subprocess.CompletedProcess([], verify_code)
            refresh(self.config, self.store, "bsm")
            return service, verify

    def test_refresh_backs_up_files_and_leaves_service_stopped(self):
        environment = self.config.state_dir / "bsm-runtime.env"
        environment.write_text("old private settings")
        environment.chmod(0o600)
        service, verify = self.run_refresh()
        service.return_value.reload_units.assert_called_once_with()
        service.return_value.control.assert_not_called()
        verify.assert_called_once()
        backup = next((self.config.state_dir / "service-backups").iterdir())
        self.assertEqual(
            (backup / environment.name).read_text(), "old private settings"
        )
        self.assertEqual((backup / self.config.service_unit).read_text(), self.original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.operations()[0]["state"], "succeeded")

    def test_running_refresh_is_rejected_before_changes(self):
        with self.assertRaisesRegex(ValueError, "Stop BSM"):
            self.run_refresh({"ActiveState": "active", "MainPID": "42", "Job": ""})
        self.assertEqual(self.unit.read_text(), self.original)
        self.assertEqual(self.store.operations(), [])

    def test_verify_failure_preserves_unit_and_environment(self):
        with self.assertRaisesRegex(ServiceError, "verification failed"):
            self.run_refresh(verify_code=1)
        self.assertEqual(self.unit.read_text(), self.original)
        self.assertFalse((self.config.state_dir / "bsm-runtime.env").exists())
        self.assertEqual(self.store.operations(), [])

    def test_unmanaged_unit_is_not_replaced(self):
        self.unit.write_text("[Service]\nExecStart=/bin/true\n")
        with self.assertRaisesRegex(ValueError, "not generated"):
            self.run_refresh()
        self.assertEqual(self.unit.read_text(), "[Service]\nExecStart=/bin/true\n")
