"""Recovery inspection preserves live-worker and update publication boundaries."""

import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_management

from bsm_supervisor import deployment
from bsm_supervisor import recovery_console as recovery
from bsm_supervisor.checkpoint_files import read_json, write_json
from bsm_supervisor.config import Config
from bsm_supervisor.store import BusyError, Store


class RecoveryConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(
            self.root / "state", self.root / "installations", self.root / "data"
        )
        self.store = Store(self.config.state_dir)
        self.state = dict(
            ActiveState="failed",
            SubState="failed",
            MainPID="0",
            Job="",
            Result="exit-code",
        )
        service = patch.object(
            recovery,
            "service_backend",
            return_value=Mock(status=lambda: dict(self.state)),
        )
        service.start()
        self.addCleanup(service.stop)

    def pending(self):
        with self.store.connect() as db:
            return db.execute(
                "INSERT INTO operations(action,state,detail,started_at) "
                "VALUES ('service:start','running','failure','now')"
            ).lastrowid

    def test_inspection_and_acknowledgement_with_service_drift(self):
        operation = self.pending()
        report = recovery.inspect_operation(self.config, self.store, operation)
        self.state["ActiveState"] = "activating"
        with self.assertRaisesRegex(ValueError, "changed"):
            recovery.acknowledge_operation(
                self.config, self.store, operation, report["digest"]
            )
        report = recovery.inspect_operation(self.config, self.store, operation)
        with self.assertRaises(BusyError):
            recovery.acknowledge_operation(
                self.config, self.store, operation, report["digest"]
            )
        self.state["ActiveState"] = "failed"
        report = recovery.inspect_operation(self.config, self.store, operation)
        recovery.acknowledge_operation(
            self.config, self.store, operation, report["digest"]
        )
        self.assertEqual(self.store.operations()[0]["state"], "acknowledged")

    def test_live_job_lease_blocks_acknowledgement(self):
        operation = self.pending()
        jobs = self.config.state_dir / "web-jobs"
        jobs.mkdir()
        write_json(jobs / "abc.json", {"id": "abc", "state": "running"})
        with (jobs / "abc.lock").open("w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.assertTrue(recovery.job_active(self.config, {"id": "abc"}))
            with self.assertRaises(BusyError):
                recovery.acknowledge_operation(
                    self.config,
                    self.store,
                    operation,
                    recovery.inspect_operation(self.config, self.store, operation)[
                        "digest"
                    ],
                )
        report = recovery.inspect_operation(self.config, self.store, operation)
        recovery.acknowledge_operation(
            self.config, self.store, operation, report["digest"]
        )

    def test_history_pagination_does_not_skip_old_operations(self):
        for _ in range(105):
            operation = self.pending()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE operations SET state = 'succeeded' WHERE id = ?",
                    (operation,),
                )
        first = self.store.operations()
        second = self.store.operations(first[-1]["id"])
        self.assertEqual(len(first), 100)
        self.assertEqual(len(second), 5)
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in second})

    def update_plan(self):
        plan = dict(
            phase="preparing",
            config=str(self.root / "config.toml"),
            previous_environment="/old",
            scope=[
                str(self.config.data_dir),
                str(self.config.config_dir),
                str(self.config.installations_dir),
                self.config.service_unit,
            ],
        )
        write_json(self.config.state_dir / "supervisor-update.json", plan)
        return plan

    def test_cancel_only_matching_unpublished_update_and_archive_plan(self):
        self.update_plan()
        with patch.object(
            recovery.self_update, "installed_environment", return_value=Path("/old")
        ):
            report = recovery.inspect_update(self.config, self.root / "config.toml")
            self.assertTrue(report["cancellable"])
            self.assertFalse(
                recovery.inspect_update(self.config, self.root / "different.toml")[
                    "cancellable"
                ]
            )
            recovery.cancel_update(
                self.config, self.store, report["digest"], self.root / "config.toml"
            )
        self.assertFalse((self.config.state_dir / "supervisor-update.json").exists())
        archived = list(
            self.config.state_dir.glob("supervisor-update-cancelled-*.json")
        )
        self.assertEqual(read_json(archived[0])["phase"], "preparing")
        self.assertEqual(self.state["ActiveState"], "failed")

    def test_installed_update_cannot_be_cancelled(self):
        self.update_plan()
        with patch.object(
            recovery.self_update, "installed_environment", return_value=Path("/new")
        ):
            report = recovery.inspect_update(self.config, self.root / "config.toml")
            self.assertFalse(report["cancellable"])
            with self.assertRaises(ValueError):
                recovery.cancel_update(
                    self.config, self.store, report["digest"], self.root / "config.toml"
                )
        with patch.object(
            recovery.self_update, "installed_environment", side_effect=FileNotFoundError
        ):
            self.assertFalse(
                recovery.inspect_update(self.config, self.root / "config.toml")[
                    "cancellable"
                ]
            )

    def test_deployment_report_contains_capabilities_without_credentials(self):
        deployment.record(self.config, "linux")
        report = deployment.information(self.config)
        self.assertEqual(report["installation"]["method"], "linux")
        self.assertTrue(report["capabilities"]["service_control"])
        self.assertNotIn("bsm_db_url", report)
        with self.assertRaises(ValueError):
            deployment.record(self.config, "docker")


class RecoveryHTTPTests(unittest.TestCase):
    setUp = test_management.ManagementTests.setUp
    request = test_management.ManagementTests.request

    def test_history_requires_auth_and_validates_cursor(self):
        self.assertEqual(self.request("/api/history", authenticated=False)[0], 401)
        self.assertEqual(self.request("/api/history", query=b"before=invalid")[0], 400)
        status, body, _ = self.request("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"operations": [], "next_before": None})

    def test_recovery_requires_inspected_digest_and_confirmation(self):
        status, _, _ = self.request(
            "/api/recovery/operations/1/acknowledge", "POST", {"confirmed": True}
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "/api/recovery/update/cancel",
            "POST",
            {"confirmed": True, "digest": "a" * 64},
            origin="https://untrusted.example",
        )
        self.assertEqual(status, 403)
