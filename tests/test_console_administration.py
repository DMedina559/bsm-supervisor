"""Exercise browser administration with real archives, databases and guarded jobs."""

import asyncio
import json
import os
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_checkpoints as fixtures
import test_management as web_fixtures

from bsm_supervisor import (
    checkpoints,
    console_admin,
    console_transfers,
    scheduling,
)
from bsm_supervisor.checkpoint_files import read_json, write_json
from bsm_supervisor.config import Config
from bsm_supervisor.store import BusyError


class ArchiveAdministrationTests(unittest.TestCase):
    setUp = fixtures.CheckpointTests.setUp
    checkpoint = fixtures.CheckpointTests.checkpoint

    def selected(self):
        path = self.config.installations_dir / "release-test"
        (path / "bin").mkdir(parents=True)
        for name in ("bin/python", "bin/bedrock-server-manager", "pyvenv.cfg"):
            (path / name).touch()
        self.store.register(
            "release-test", "4.0.0b7", path, self.config.installations_dir
        )
        (self.config.installations_dir / "current").symlink_to("release-test")

    def execute(self, action, **value):
        return console_admin.execute(
            self.config,
            Path(self.temp.name) / "config.toml",
            self.store,
            {"action": action, "confirmed": True, **value},
        )

    def test_encrypted_download_upload_import_preserves_sqlite_and_world(self):
        checkpoint_id = self.checkpoint()
        result = self.execute(
            "checkpoint-export",
            target=checkpoint_id,
            passphrase="private encryption password",
        )
        archive = console_transfers.resolve(self.config, result["id"], "download")
        self.assertNotIn(b"original world", archive.read_bytes())
        download = archive.read_bytes()
        checkpoints.checkpoint_path(self.config, checkpoint_id).rename(
            Path(self.temp.name) / "saved-checkpoint"
        )
        messages = iter(
            [
                {"type": "http.request", "body": download[:80], "more_body": True},
                {"type": "http.request", "body": download[80:]},
            ]
        )

        async def receive():
            return next(messages)

        uploaded = asyncio.run(
            console_transfers.upload(
                self.config, {"content-type": "application/octet-stream"}, receive
            )
        )
        imported = self.execute(
            "checkpoint-import",
            target=uploaded["id"],
            passphrase="private encryption password",
        )
        self.assertFalse(imported["restored"])
        self.assertEqual(imported["id"], checkpoint_id)
        self.assertFalse(
            console_transfers.archive(self.config, uploaded["id"]).exists()
        )
        self.assertEqual(
            (self.config.data_dir / "servers/world.dat").read_bytes(), b"original world"
        )
        self.assertEqual(list(self.config.state_dir.glob(".console-secret-*")), [])
        checkpoints.verify(self.config, checkpoint_id)

    def test_failed_password_retains_upload_for_retry(self):
        checkpoint_id = self.checkpoint()
        exported = self.execute(
            "checkpoint-export",
            target=checkpoint_id,
            passphrase="correct password value",
        )
        path = console_transfers.archive(self.config, exported["id"])
        metadata = read_json(path.with_suffix(".json"))
        metadata["direction"] = "upload"
        write_json(path.with_suffix(".json"), metadata)
        with self.assertRaisesRegex(ValueError, "authentication"):
            self.execute(
                "checkpoint-import",
                target=exported["id"],
                passphrase="incorrect password value",
            )
        self.assertTrue(path.exists())
        self.assertEqual(list(self.config.state_dir.glob(".console-secret-*")), [])

    def test_restore_requires_preview_and_matching_version_before_shutdown(self):
        self.selected()
        checkpoint_id = self.checkpoint()
        plan = console_admin.checkpoint_summary(self.config, checkpoint_id)
        self.assertTrue(plan["compatible_version"])
        self.assertEqual(plan["database_path"], str(self.database))
        with patch("bsm_supervisor.console_admin.runtime.shutdown") as shutdown:
            with self.assertRaisesRegex(ValueError, "Checkpoint changed"):
                self.execute("backup-restore", target=checkpoint_id, digest="0" * 64)
            shutdown.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Inspect"):
            console_admin.validate(
                {"action": "backup-restore", "target": checkpoint_id, "confirmed": True}
            )

    def test_restore_captures_pinned_safety_checkpoint_and_restores_database(self):
        self.selected()
        checkpoint_id = self.checkpoint()
        (self.config.data_dir / "servers/world.dat").write_bytes(b"new world")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE content SET value='new'")
        plan = console_admin.checkpoint_summary(self.config, checkpoint_id)
        with patch("bsm_supervisor.console_admin.service_backend") as service:
            service.return_value.status.return_value = (
                self.mock_service.return_value.status.return_value
            )
            result = self.execute(
                "backup-restore", target=checkpoint_id, digest=plan["digest"]
            )
        self.assertTrue(
            (
                checkpoints.checkpoint_path(self.config, result["recovery_checkpoint"])
                / "pinned"
            ).exists()
        )
        self.assertEqual(
            (self.config.data_dir / "servers/world.dat").read_bytes(), b"original world"
        )
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(
                db.execute("SELECT value FROM content").fetchone()[0], "saved"
            )
        self.assertTrue(result["originals_retained"])

    def test_pending_operation_blocks_shutdown_and_checkpoint_creation(self):
        self.store.begin("interrupted")
        with patch("bsm_supervisor.console_admin.runtime.shutdown") as shutdown:
            with self.assertRaises(BusyError):
                self.execute("backup-create")
            shutdown.assert_not_called()

    def test_pinned_and_latest_checkpoint_deletion_is_refused(self):
        self.selected()
        first = self.checkpoint()
        second = self.checkpoint()
        checkpoints.pin(self.config, self.store, first, True)
        for checkpoint_id in (first, second):
            with self.assertRaisesRegex(ValueError, "pinned"):
                self.execute("checkpoint-delete", target=checkpoint_id)
        checkpoints.pin(self.config, self.store, first, False)
        self.assertEqual(
            self.execute("checkpoint-delete", target=first), {"deleted": first}
        )

    def test_export_limit_is_enforced_before_archive_creation(self):
        checkpoint_id = self.checkpoint()
        config = replace(self.config, web_transfer_limit=1024)
        with self.assertRaisesRegex(ValueError, "staging limits"):
            console_admin.execute(
                config,
                Path(self.temp.name) / "config.toml",
                self.store,
                {
                    "action": "checkpoint-export",
                    "target": checkpoint_id,
                    "confirmed": True,
                },
            )

    def test_sqlite_schema_metadata_is_verified_before_restore(self):
        checkpoint_id = self.checkpoint()
        path = checkpoints.checkpoint_path(self.config, checkpoint_id) / "manifest.json"
        manifest = read_json(path)
        manifest["schema_revision"] = ["incorrect_revision"]
        write_json(path, manifest)
        with self.assertRaisesRegex(ValueError, "schema differs"):
            checkpoints.verify(self.config, checkpoint_id)

    def test_mismatched_bsm_version_is_not_eligible_for_restore(self):
        self.selected()
        checkpoint_id = self.checkpoint()
        path = checkpoints.checkpoint_path(self.config, checkpoint_id) / "manifest.json"
        manifest = read_json(path)
        manifest["installation"]["version"] = "3.10.6"
        write_json(path, manifest)
        plan = console_admin.checkpoint_summary(self.config, checkpoint_id)
        self.assertFalse(plan["compatible_version"])
        with patch("bsm_supervisor.console_admin.runtime.shutdown") as shutdown:
            with self.assertRaisesRegex(ValueError, "version differs"):
                self.execute(
                    "backup-restore", target=checkpoint_id, digest=plan["digest"]
                )
            shutdown.assert_not_called()


class TransferBoundaryTests(unittest.TestCase):
    setUp = web_fixtures.ManagementTests.setUp
    request = web_fixtures.ManagementTests.request

    def upload(self, blocks, **headers):
        messages = iter(blocks)

        async def receive():
            return next(messages)

        return asyncio.run(
            console_transfers.upload(
                self.config,
                {"content-type": "application/octet-stream", **headers},
                receive,
            )
        )

    def test_length_actual_limit_disconnect_and_cleanup(self):
        self.config = replace(self.config, web_transfer_limit=1024)
        for messages, headers in (
            ([{"type": "http.request", "body": b"a" * 1025}], {}),
            ([{"type": "http.disconnect"}], {}),
            ([{"type": "http.request", "body": b"a"}], {"content-length": "2"}),
            ([{"type": "http.request", "body": b""}], {}),
        ):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.upload(messages, **headers)
            self.assertEqual(
                list(console_transfers.folder(self.config).glob("*.bsmcheckpoint")), []
            )

    def test_auth_and_origin_checked_before_upload(self):
        for auth, origin, expected in (
            (False, None, 401),
            (True, "https://evil.example", 403),
        ):
            self.assertEqual(
                self.request(
                    "/api/transfers", method="POST", authenticated=auth, origin=origin
                )[0],
                expected,
            )
        self.assertEqual(
            self.request("/api/transfers/" + "a" * 32, authenticated=False)[0], 401
        )

    def test_download_streams_bytes_with_no_store_and_attachment_headers(self):
        transfer_id = "b" * 32
        path = console_transfers.archive(self.config, transfer_id)
        path.write_bytes(b"archive data")
        console_transfers.publish(self.config, transfer_id, "download")
        status, body, headers = self.request("/api/transfers/" + transfer_id)
        self.assertEqual((status, body), (200, b"archive data"))
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertIn(b"attachment", headers[b"content-disposition"])
        self.assertEqual(headers[b"content-type"], b"application/octet-stream")

    def test_expired_transfers_and_symlinks_are_not_downloadable(self):
        transfer_id = "c" * 32
        path = console_transfers.archive(self.config, transfer_id)
        path.write_bytes(b"archive")
        report = console_transfers.publish(self.config, transfer_id, "download")
        report["expires_at"] = 0
        write_json(path.with_suffix(".json"), report)
        with self.assertRaises(ValueError):
            console_transfers.resolve(self.config, transfer_id, "download")
        report["expires_at"] = 10**15
        write_json(path.with_suffix(".json"), report)
        path.unlink()
        path.symlink_to(self.token)
        with self.assertRaises(ValueError):
            console_transfers.resolve(self.config, transfer_id, "download")

    def test_secrets_are_private_requests_not_job_metadata_or_argv(self):
        with patch("bsm_supervisor.web.threading.Thread") as thread:
            job = self.console.submit(
                {
                    "action": "checkpoint-export",
                    "target": "a" * 32,
                    "passphrase": "private password value",
                    "confirmed": True,
                }
            )
            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[1], ["console-task", job["id"]])
            self.assertNotIn("private password", json.dumps(job))
            path = self.console.jobs / (job["id"] + ".request")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch(
                "bsm_supervisor.console_admin.execute", return_value={"ok": True}
            ) as execute:
                result = console_admin.execute_request(
                    self.config, self.root / "config.toml", self.store, job["id"]
                )
            self.assertEqual(result, {"ok": True})
            self.assertEqual(
                execute.call_args.args[-1]["passphrase"], "private password value"
            )
            self.assertFalse(path.exists())
            os.close(args[2])
            self.console.lock.release()

    def test_unknown_fields_bad_types_and_missing_confirmations_rejected(self):
        for value in (
            {"action": []},
            {
                "action": "settings-save",
                "settings": {"state_dir": "/tmp"},
                "confirmed": True,
            },
            {"action": "checkpoint-export", "target": "a" * 32},
            {
                "action": "checkpoint-import",
                "target": "a" * 32,
                "confirmed": True,
                "passphrase": "short",
            },
            {"action": "supervisor-update", "confirmed": True, "ref": "../../evil"},
            {
                "action": "journal-prune",
                "confirmed": True,
                "keep_days": True,
                "keep_jobs": 10,
            },
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                console_admin.validate(value)

    def test_settings_secrets_are_concealed_and_paths_read_only(self):
        config = replace(
            self.config, bsm_db_url="postgresql://secret:password@localhost/database"
        )
        self.assertNotIn("password", json.dumps(console_admin.settings(config)))
        self.assertTrue(console_admin.settings(config)["database_url_configured"])
        with self.assertRaises(ValueError):
            console_admin.validate(
                {
                    "action": "settings-save",
                    "settings": {"config_dir": "/tmp"},
                    "confirmed": True,
                }
            )

    def config_file(self):
        path = self.root / "config.toml"
        path.write_text(
            "\n".join(
                key + " = " + json.dumps(str(getattr(self.config, key)))
                for key in ("state_dir", "installations_dir", "data_dir")
            )
            + "\n"
        )
        return path

    def test_nonruntime_settings_persist_and_configuration_backup_is_private(self):
        path = self.config_file()
        result = console_admin.save_settings(
            self.config,
            path,
            self.store,
            {"settings": {"web_auto_start": False, "web_transfer_limit": 1024}},
        )
        self.assertFalse(result["managed_unit_refreshed"])
        self.assertFalse(Config.load(path).web_auto_start)
        self.assertEqual(
            (self.config.state_dir / "config-before-console.toml").stat().st_mode
            & 0o777,
            0o600,
        )

    def test_invalid_or_environment_controlled_settings_leave_file_unchanged(self):
        path = self.config_file()
        original = path.read_bytes()
        with patch(
            "bsm_supervisor.console_admin.overrides", return_value={"HOST": "0.0.0.0"}
        ):
            with self.assertRaisesRegex(ValueError, "environment"):
                console_admin.save_settings(
                    self.config,
                    path,
                    self.store,
                    {"settings": {"bsm_host": "127.0.0.1"}},
                )
        with self.assertRaises(ValueError):
            console_admin.save_settings(
                self.config, path, self.store, {"settings": {"bsm_port": -1}}
            )
        self.assertEqual(path.read_bytes(), original)

    def test_notification_configuration_does_not_echo_webhook(self):
        value = {
            "action": "notifications-save",
            "url": "https://example.com/private-webhook-secret",
            "confirmed": True,
        }
        result = console_admin.execute(
            self.config, self.root / "config.toml", self.store, value
        )
        self.assertEqual(result, {"enabled": True})
        self.assertNotIn("private-webhook-secret", json.dumps(self.store.operations()))
        self.assertTrue(console_admin.settings(self.config)["notifications_enabled"])

    def test_interrupted_job_acknowledgement_uses_confirmed_authenticated_route(self):
        job_id = "f" * 32
        write_json(
            self.console.jobs / (job_id + ".json"),
            {"id": job_id, "state": "interrupted"},
        )
        status, _, _ = self.request(
            "/api/jobs/" + job_id + "/acknowledge", "POST", {"confirmed": False}
        )
        self.assertEqual(status, 400)
        status, body, _ = self.request(
            "/api/jobs/" + job_id + "/acknowledge", "POST", {"confirmed": True}
        )
        self.assertEqual((status, json.loads(body)), (200, {"acknowledged": True}))
        self.assertEqual(
            read_json(self.console.jobs / (job_id + ".json"))["state"], "acknowledged"
        )


class ScheduleAndUpdateTests(unittest.TestCase):
    setUp = web_fixtures.ManagementTests.setUp

    def policy(self):
        return {
            "name": "nightly",
            "action": "checkpoint",
            "calendar": "*-*-* 03:00:00 UTC",
            "window_start": "03:00",
            "window_end": "04:00",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "maintenance_confirmed": True,
        }

    def test_schedule_edit_preserves_previous_policy_and_updates_owned_timer(self):
        units = self.root / "units"
        units.mkdir()
        source = self.root / "policy.json"
        original = self.policy()
        write_json(source, original)
        with (
            patch("bsm_supervisor.scheduling.UNIT_DIRECTORY", units),
            patch("bsm_supervisor.scheduling.os.geteuid", return_value=0),
            patch(
                "bsm_supervisor.scheduling.subprocess.run",
                return_value=SimpleNamespace(stdout="inactive"),
            ),
        ):
            scheduling.install(
                self.config, self.root / "config.toml", self.store, source
            )
            changed = {**original, "calendar": "*-*-* 03:30:00 UTC"}
            write_json(source, changed)
            scheduling.install(
                self.config,
                self.root / "config.toml",
                self.store,
                source,
                replace_existing=True,
            )
        self.assertIn(
            "03:30:00 UTC",
            (units / "bsm-supervisor-schedule-nightly.timer").read_text(),
        )
        self.assertEqual(
            read_json(self.config.state_dir / "schedules/nightly.previous"), original
        )

    def test_foreign_schedule_units_are_not_overwritten_or_journaled_pending(self):
        units = self.root / "units"
        units.mkdir()
        foreign = units / "bsm-supervisor-schedule-nightly.service"
        foreign.write_text("Description=Another service\n")
        source = self.root / "policy.json"
        write_json(source, self.policy())
        with (
            patch("bsm_supervisor.scheduling.UNIT_DIRECTORY", units),
            patch("bsm_supervisor.scheduling.os.geteuid", return_value=0),
            patch("bsm_supervisor.scheduling.subprocess.run"),
        ):
            with self.assertRaisesRegex(ValueError, "owned"):
                scheduling.install(
                    self.config,
                    self.root / "config.toml",
                    self.store,
                    source,
                    replace_existing=True,
                )
        self.assertEqual(foreign.read_text(), "Description=Another service\n")
        self.assertEqual(self.store.operations(), [])

    def test_supervisor_update_uses_shared_backend_without_token_in_results(self):
        value = {
            "action": "supervisor-update",
            "ref": "dev",
            "github_token": "github_private_token",
            "confirmed": True,
            "resume": True,
        }
        with patch(
            "bsm_supervisor.console_admin.self_update.update",
            return_value={"phase": "completed", "revision": "a" * 40},
        ) as update:
            result = console_admin.execute(
                self.config, self.root / "config.toml", self.store, value
            )
        self.assertTrue(result["console_restart_required"])
        self.assertNotIn("github_private_token", json.dumps(result))
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assertEqual(update.call_args.kwargs, {"resume": True})
        self.assertEqual(update.call_args.args[3], "dev")

    def test_read_only_diagnostics_can_run_while_interrupted_jobs_need_recovery(self):
        write_json(
            self.console.jobs / ("a" * 32 + ".json"),
            {"id": "a" * 32, "state": "interrupted"},
        )
        with patch("bsm_supervisor.web.threading.Thread") as thread:
            job = self.console.submit({"action": "doctor"})
            self.assertEqual(job["action"], "doctor")
            os.close(thread.call_args.kwargs["args"][2])
            self.console.lock.release()
