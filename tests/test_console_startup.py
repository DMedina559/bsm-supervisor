"""Console startup ordering, guarded service starts, and bundled frontend assets."""

import asyncio
import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import test_administration as fixtures
import test_management as console_fixtures

from bsm_supervisor import autostart, web
from bsm_supervisor.checkpoint_files import write_json
from bsm_supervisor.service_environment import write_environment


class StartupTests(unittest.TestCase):
    setUp = fixtures.AdministrationTests.setUp
    installation = fixtures.AdministrationTests.installation
    stopped = fixtures.AdministrationTests.stopped

    def selected(self):
        self.installation()
        (self.config.installations_dir / "current").symlink_to("release-test")
        self.config.config_dir.mkdir(parents=True, exist_ok=True)
        write_environment(self.config)

    def test_no_selection_or_disabled_never_controls_service(self):
        with patch("bsm_supervisor.autostart.operations.control") as control:
            self.assertEqual(
                autostart.start(self.config, self.store)["state"], "skipped"
            )
            config = replace(self.config, web_auto_start=False)
            self.assertEqual(autostart.start(config, self.store)["state"], "disabled")
        control.assert_not_called()
        with self.assertRaises(ValueError):
            replace(self.config, web_auto_start="true")

    def test_stopped_service_starts_without_application_readiness(self):
        self.selected()
        active = {**self.stopped(), "ActiveState": "active", "MainPID": "123"}
        with (
            patch(
                "bsm_supervisor.service.SystemdService.status",
                side_effect=[self.stopped(), self.stopped(), active],
            ),
            patch("bsm_supervisor.service.SystemdService.control") as control,
            patch("bsm_supervisor.runtime.wait_ready") as wait,
        ):
            self.assertEqual(
                autostart.start(self.config, self.store)["state"], "started"
            )
        control.assert_called_once_with("start")
        wait.assert_not_called()
        self.assertEqual(self.store.operations()[0]["state"], "succeeded")

    def test_running_or_transitioning_service_is_left_alone(self):
        self.selected()
        for fields, expected in (
            ({"ActiveState": "active", "MainPID": "123"}, "running"),
            ({"Job": "23/start"}, "skipped"),
            ({"ActiveState": "activating"}, "skipped"),
            ({"MainPID": "123"}, "blocked"),
        ):
            with (
                self.subTest(fields=fields),
                patch(
                    "bsm_supervisor.service.SystemdService.status",
                    return_value={**self.stopped(), **fields},
                ),
                patch("bsm_supervisor.autostart.operations.control") as control,
            ):
                self.assertEqual(
                    autostart.start(self.config, self.store)["state"], expected
                )
                control.assert_not_called()

    def test_markers_pending_operations_and_update_recovery_block_start(self):
        self.selected()
        with patch("bsm_supervisor.autostart.operations.control") as control:
            for name in ("supervisor-recovery.required", "supervisor-upgrade.required"):
                marker = self.config.config_dir / name
                marker.symlink_to("missing-target")
                self.assertEqual(
                    autostart.start(self.config, self.store)["state"], "blocked"
                )
                marker.unlink()
            write_json(
                self.config.state_dir / "supervisor-update.json", {"phase": "switching"}
            )
            self.assertEqual(
                autostart.start(self.config, self.store)["state"], "blocked"
            )
            (self.config.state_dir / "supervisor-update.json").unlink()
            self.store.begin("interrupted")
            self.assertEqual(
                autostart.start(self.config, self.store)["state"], "blocked"
            )
            control.assert_not_called()

    def test_child_rechecks_recovery_after_job_is_queued(self):
        self.selected()
        console = MagicMock(config=self.config)
        console.submit.return_value = {"id": "test-job"}
        with patch(
            "bsm_supervisor.service.SystemdService.status", return_value=self.stopped()
        ):
            self.assertEqual(autostart.attempt(console)["state"], "queued")
        console.submit.assert_called_once_with({"action": "auto-start"})
        (self.config.config_dir / "supervisor-recovery.required").touch()
        with patch("bsm_supervisor.autostart.operations.control") as control:
            self.assertEqual(
                autostart.start(self.config, self.store)["state"], "blocked"
            )
            control.assert_not_called()

    def test_failed_process_start_retains_operation_for_inspection(self):
        self.selected()
        with (
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value=self.stopped(),
            ),
            patch(
                "bsm_supervisor.service.SystemdService.control",
                side_effect=OSError("failed"),
            ),
        ):
            self.assertEqual(
                autostart.start(self.config, self.store)["state"], "blocked"
            )
        self.assertEqual(self.store.operations()[0]["state"], "running")

    def test_interrupted_console_job_blocks_automatic_start(self):
        self.selected()
        token = self.root / "token"
        web.token_create(token)
        console = web.Console(
            self.config, self.config_file, token, "http://127.0.0.1:11326"
        )
        write_json(
            console.jobs / ("a" * 32 + ".json"),
            {"id": "a" * 32, "state": "interrupted"},
        )
        with (
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value=self.stopped(),
            ),
            patch("bsm_supervisor.autostart.operations.control") as control,
        ):
            self.assertEqual(autostart.attempt(console)["state"], "blocked")
            control.assert_not_called()

    def test_store_failure_keeps_console_available(self):
        console = SimpleNamespace(config=self.config)
        with patch(
            "bsm_supervisor.autostart.Store", side_effect=Exception("unavailable")
        ):
            self.assertEqual(autostart.attempt(console)["state"], "blocked")


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_bsm_is_not_queued_before_listener_readiness(self):
        server = SimpleNamespace(started=False, should_exit=False)
        app = SimpleNamespace(auto_start={"state": "waiting"})
        with patch(
            "bsm_supervisor.web.autostart.attempt", return_value={"state": "queued"}
        ) as attempt:
            task = asyncio.create_task(web.start_after_ready(server, app))
            await asyncio.sleep(0)
            attempt.assert_not_called()
            server.started = True
            await asyncio.wait_for(task, 2)
            attempt.assert_called_once_with(app)
            self.assertEqual(app.auto_start["state"], "queued")

    async def test_failed_listener_does_not_start_bsm(self):
        with patch("bsm_supervisor.web.autostart.attempt") as attempt:
            await web.start_after_ready(
                SimpleNamespace(started=False, should_exit=True), SimpleNamespace()
            )
            attempt.assert_not_called()


class ConsoleAssetsTests(unittest.TestCase):
    setUp = console_fixtures.ManagementTests.setUp
    request = console_fixtures.ManagementTests.request

    def test_frontend_assets_are_local_and_match_provenance(self):
        manifest = json.loads((web.ASSETS / "frontend/SOURCE.json").read_text())
        for name, info in manifest["files"].items():
            data = (web.ASSETS / "frontend" / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])
        for path in web.STATIC_ASSETS:
            status, body, headers = self.request(path, authenticated=False)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            if path.endswith(".png"):
                self.assertEqual(headers[b"content-type"], b"image/png")
                self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(
            self.request("/frontend/../../config.toml", authenticated=False)[0], 404
        )

    def test_version_validation_happens_before_job_creation(self):
        for target in ("release-4.0.0b7", "latest", "banana"):
            with (
                self.subTest(target=target),
                self.assertRaisesRegex(ValueError, "Choose stable"),
            ):
                self.console.submit({"action": "install", "target": target})
        self.assertEqual(list(self.console.jobs.glob("*.json")), [])
        self.assertEqual(
            web.arguments({"action": "install", "target": " PRERELEASE "}),
            ["install", "prerelease"],
        )
        self.assertEqual(
            web.arguments({"action": "install", "target": " 4.0.0b7 "}),
            ["install", "4.0.0b7"],
        )
        self.assertEqual(
            web.arguments({"action": "restart", "confirmed": True}), ["restart"]
        )
