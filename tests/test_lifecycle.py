"""Exercise optional systemd lifecycle coupling and guarded restart ordering."""

import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_administration as fixtures

from bsm_supervisor import autostart, console_admin
from bsm_supervisor.activation import service_unit
from bsm_supervisor.config import Config
from bsm_supervisor.integration import require_managed
from bsm_supervisor.service_environment import write_environment


class LifecycleTests(unittest.TestCase):
    setUp = fixtures.AdministrationTests.setUp
    stopped = fixtures.AdministrationTests.stopped
    installation = fixtures.AdministrationTests.installation

    def test_default_independent_and_coupled_unit(self):
        independent = service_unit(self.config, "bsm")
        self.assertNotIn("BindsTo=", independent)
        coupled = replace(self.config, stop_bsm_with_supervisor=True)
        unit = service_unit(coupled, "bsm")
        self.assertIn("BindsTo=bsm-supervisor-console.service\n", unit)
        self.assertIn("After=bsm-supervisor-console.service\n", unit)
        self.assertIn("KillMode=control-group", unit)
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, stop_bsm_with_supervisor=value)
        with self.assertRaises(ValueError):
            replace(coupled, service_unit="bsm-supervisor-console.service")

    def test_managed_checks_require_matching_lifecycle_dependencies(self):
        self.config.config_dir.mkdir(parents=True)
        write_environment(self.config)
        coupled = replace(self.config, stop_bsm_with_supervisor=True)
        state = self.stopped()
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            require_managed(coupled, state)
        state.update(
            BindsTo="bsm-supervisor-console.service",
            After="network.target bsm-supervisor-console.service",
        )
        require_managed(coupled, state)
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            require_managed(self.config, state)

    def test_settings_apply_coupling_only_while_stopped_and_refresh_unit(self):
        self.config_file.write_text(
            f'state_dir = "{self.config.state_dir}"\n'
            f'installations_dir = "{self.config.installations_dir}"\n'
            f'data_dir = "{self.config.data_dir}"\n'
        )
        value = {"settings": {"stop_bsm_with_supervisor": True}}
        with (
            patch(
                "bsm_supervisor.console_admin.require_offline",
                side_effect=ValueError("still active"),
            ),
            patch("bsm_supervisor.console_admin.refresh") as refresh,
        ):
            with self.assertRaisesRegex(ValueError, "still active"):
                console_admin.save_settings(
                    self.config, self.config_file, self.store, value
                )
            refresh.assert_not_called()
        self.assertFalse(Config.load(self.config_file).stop_bsm_with_supervisor)
        with (
            patch("bsm_supervisor.console_admin.require_offline"),
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value=self.stopped(),
            ),
            patch("bsm_supervisor.console_admin.refresh") as refresh,
        ):
            result = console_admin.save_settings(
                self.config, self.config_file, self.store, value
            )
            self.assertTrue(result["managed_unit_refreshed"])
            self.assertTrue(refresh.call_args.args[0].stop_bsm_with_supervisor)
        self.assertTrue(Config.load(self.config_file).stop_bsm_with_supervisor)

    def test_console_restart_waits_for_bsm_stop_before_eligibility(self):
        config = replace(self.config, stop_bsm_with_supervisor=True)
        console = SimpleNamespace(
            config=config, submit=Mock(return_value={"id": "test"})
        )
        with (
            patch(
                "bsm_supervisor.service.SystemdService.status",
                side_effect=[
                    {"ActiveState": "deactivating"},
                    {"ActiveState": "inactive"},
                ],
            ),
            patch(
                "bsm_supervisor.autostart.threading.Event.wait", return_value=False
            ) as sleep,
            patch(
                "bsm_supervisor.autostart.eligible", return_value={"state": "eligible"}
            ) as eligible,
        ):
            self.assertEqual(autostart.attempt(console)["state"], "queued")
            sleep.assert_called_once_with(0.2)
            eligible.assert_called_once()
        console.submit.assert_called_once_with({"action": "auto-start"})

    def test_shutdown_timeout_never_queues_start(self):
        console = SimpleNamespace(
            config=replace(self.config, stop_bsm_with_supervisor=True), submit=Mock()
        )
        with (
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value={"ActiveState": "deactivating"},
            ),
            patch("bsm_supervisor.autostart.time.monotonic", side_effect=[0, 9999]),
            patch("bsm_supervisor.autostart.eligible") as eligible,
        ):
            self.assertEqual(autostart.attempt(console)["state"], "blocked")
            eligible.assert_not_called()
        console.submit.assert_not_called()

    def test_console_shutdown_cancels_waiting_start(self):
        stopping = threading.Event()
        stopping.set()
        console = SimpleNamespace(
            config=replace(self.config, stop_bsm_with_supervisor=True),
            submit=Mock(),
            shutdown_event=stopping,
        )
        with patch(
            "bsm_supervisor.service.SystemdService.status",
            return_value={"ActiveState": "deactivating"},
        ):
            self.assertEqual(autostart.attempt(console)["state"], "skipped")
        console.submit.assert_not_called()
