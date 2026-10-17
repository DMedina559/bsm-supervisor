"""Exercise checkpoint transfer, authenticated control, timers and event delivery."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_checkpoints as fixtures

from bsm_supervisor import events, scheduling, transfer, web
from bsm_supervisor.checkpoint_files import read_json, write_json
from bsm_supervisor.config import Config
from bsm_supervisor.releases import Release
from bsm_supervisor.store import BusyError, Store


class TransferTests(unittest.TestCase):
    setUp = fixtures.CheckpointTests.setUp
    checkpoint = fixtures.CheckpointTests.checkpoint

    def set_archive(self, encrypted=False):
        checkpoint_id = self.checkpoint()
        path = Path(self.temp.name) / "export.bsmcheckpoint"
        password = Path(self.temp.name) / "password"
        password.write_text("a long private recovery password\n")
        password.chmod(0o600)
        transfer.export_checkpoint(
            self.config,
            self.store,
            checkpoint_id,
            path,
            password if encrypted else None,
        )
        shutil.rmtree(transfer.checkpoints.checkpoint_path(self.config, checkpoint_id))
        return checkpoint_id, path, password

    def test_plain_roundtrip_preserves_files_links_and_manifest(self):
        (self.config.data_dir / "world-link").symlink_to("servers/world.dat")
        checkpoint_id, archive, _ = self.set_archive()
        result = transfer.import_checkpoint(self.config, self.store, archive)
        self.assertFalse(result["restored"])
        self.assertEqual(result["id"], checkpoint_id)
        transfer.checkpoints.verify(self.config, checkpoint_id)
        self.assertEqual(
            (self.config.data_dir / "servers/world.dat").read_bytes(), b"original world"
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            transfer.import_checkpoint(self.config, self.store, archive)

    def test_encrypted_roundtrip_and_authentication_failure(self):
        checkpoint_id, archive, password = self.set_archive(True)
        self.assertNotIn(b"original world", archive.read_bytes())
        damaged = bytearray(archive.read_bytes())
        damaged[-25] ^= 1
        corrupt = archive.with_suffix(".corrupt")
        corrupt.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, "authentication"):
            transfer.import_checkpoint(self.config, self.store, corrupt, password)
        self.assertFalse(
            transfer.checkpoints.checkpoint_path(self.config, checkpoint_id).exists()
        )
        self.assertEqual(list((self.config.state_dir / "checkpoints").iterdir()), [])
        transfer.import_checkpoint(self.config, self.store, archive, password)
        transfer.checkpoints.verify(self.config, checkpoint_id)

    def test_wrong_password_and_public_secret_rejected(self):
        _, archive, password = self.set_archive(True)
        password.write_text("another long private password")
        with self.assertRaisesRegex(ValueError, "authentication"):
            transfer.import_checkpoint(self.config, self.store, archive, password)
        password.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "Credential"):
            transfer.import_checkpoint(self.config, self.store, archive, password)

    def test_archive_extra_traversal_member_is_never_extracted(self):
        checkpoint_id, archive, _ = self.set_archive()
        with zipfile.ZipFile(archive, "a") as bundle:
            bundle.writestr("../../escaped", "untrusted")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            transfer.import_checkpoint(self.config, self.store, archive)
        self.assertFalse((Path(self.temp.name) / "escaped").exists())
        self.assertFalse(
            transfer.checkpoints.checkpoint_path(self.config, checkpoint_id).exists()
        )

    def test_payload_corruption_and_budget_rejected(self):
        checkpoint_id, archive, _ = self.set_archive()
        with self.assertRaisesRegex(ValueError, "allowed size"):
            transfer.import_checkpoint(self.config, self.store, archive, max_bytes=1)
        altered = archive.with_suffix(".bad")
        with (
            zipfile.ZipFile(archive) as source,
            zipfile.ZipFile(altered, "w") as target,
        ):
            for member in source.infolist():
                content = source.read(member)
                if member.filename.endswith("world.dat"):
                    content = b"x" * len(content)
                target.writestr(member.filename, content)
        with self.assertRaisesRegex(ValueError, "integrity"):
            transfer.import_checkpoint(self.config, self.store, altered)
        self.assertFalse(
            transfer.checkpoints.checkpoint_path(self.config, checkpoint_id).exists()
        )

    def test_export_never_overwrites_and_scope_must_match(self):
        checkpoint_id = self.checkpoint()
        target = Path(self.temp.name) / "existing"
        target.write_text("keep")
        with self.assertRaises(FileExistsError):
            transfer.export_checkpoint(self.config, self.store, checkpoint_id, target)
        self.assertEqual(target.read_text(), "keep")
        _, archive, _ = self.set_archive()
        from dataclasses import replace

        other = replace(self.config, data_dir=Path(self.temp.name) / "other-data")
        with self.assertRaisesRegex(ValueError, "coverage"):
            transfer.import_checkpoint(other, self.store, archive)


class ManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(
            self.root / "state", self.root / "envs", self.root / "data"
        )
        self.store = Store(self.config.state_dir)
        self.token = self.root / "token"
        web.token_create(self.token)
        self.console = web.Console(
            self.config, self.root / "config.toml", self.token, "http://127.0.0.1:11326"
        )

    def request(
        self,
        path,
        method="GET",
        value=None,
        authenticated=True,
        origin=None,
        host="127.0.0.1:11326",
        raw=None,
        query=b"",
    ):
        headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
        if authenticated:
            headers.append(
                (b"authorization", b"Bearer " + transfer.private_secret(self.token))
            )
        if origin:
            headers.append((b"origin", origin.encode()))
        result = []

        async def receive():
            return {
                "type": "http.request",
                "body": raw if raw is not None else json.dumps(value).encode(),
            }

        async def send(message):
            result.append(message)

        async def invoke():
            await self.console(
                {
                    "type": "http",
                    "method": method,
                    "path": path,
                    "headers": headers,
                    "query_string": query,
                },
                receive,
                send,
            )

        asyncio.run(invoke())
        return result[0]["status"], result[1]["body"], dict(result[0]["headers"])

    def test_console_auth_origin_host_and_rotation(self):
        self.assertEqual(self.request("/api/events", authenticated=False)[0], 401)
        self.assertEqual(
            self.request("/api/events", origin="https://evil.example")[0], 403
        )
        self.assertEqual(self.request("/api/events", host="evil.example")[0], 400)
        status, body, headers = self.request("/api/events")
        self.assertEqual((status, json.loads(body)), (200, []))
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertIn(b"frame-ancestors 'none'", headers[b"content-security-policy"])
        self.token.unlink()
        web.token_create(self.token)
        self.assertEqual(self.request("/api/events")[0], 200)

    def test_public_assets_do_not_require_auth_and_paths_are_fixed(self):
        for path in ("/", "/app.js", "/style.css"):
            self.assertEqual(self.request(path, authenticated=False)[0], 200)
        self.assertEqual(self.request("/../../config.toml")[0], 404)
        self.assertEqual(self.request("/api/jobs/../../token")[0], 404)

    def test_actions_require_separate_downtime_and_data_confirmation(self):
        self.assertEqual(web.arguments({"action": "start"}), ["start"])
        for value in (
            {"action": "stop"},
            {"action": "upgrade", "target": "stable", "confirmed": True},
            {"action": "checkpoint-restore", "target": "abc", "confirmed": True},
            {"action": "install", "target": "--help"},
            {"action": "shell", "target": "id"},
            {"action": "start", "command": "id"},
            {"action": "stop", "confirmed": "yes"},
        ):
            with self.assertRaises(ValueError):
                web.arguments(value)
        self.assertIn(
            "--restore-data-confirmed",
            web.arguments(
                {
                    "action": "checkpoint-restore",
                    "target": "abc",
                    "confirmed": True,
                    "restore_data": True,
                }
            ),
        )
        self.assertEqual(self.request("/api/actions", "POST", raw=b"x" * 8193)[0], 400)

    def test_console_job_output_and_interrupted_job_block(self):
        job = {"id": "a" * 32, "action": "start", "state": "running"}
        write_json(self.console.jobs / (job["id"] + ".json"), job)
        with self.assertRaisesRegex(BusyError, "interrupted"):
            self.console.submit({"action": "start"})
        web.acknowledge(self.config, self.store, job["id"])
        self.console.lock.acquire()
        lease = os.open(self.console.jobs / (job["id"] + ".lock"), os.O_RDWR)
        # Execute the real CLI with an absent configuration; capture its failure.
        self.console.execute(job, ["start", "--wait"], lease)
        self.assertEqual(
            read_json(self.console.jobs / (job["id"] + ".json"))["state"], "failed"
        )
        self.assertEqual(self.request("/api/jobs/" + job["id"])[0], 200)

    def test_durable_event_pages_and_read_only_missing_database(self):
        missing = self.root / "missing"
        self.assertEqual(events.read(missing), [])
        self.assertFalse(missing.exists())
        with self.store.mutation("test") as operation:
            self.store.note(operation, "Copying files")
        saved = events.read(self.config.state_dir)
        self.assertEqual(
            [item["state"] for item in saved], ["running", "progress", "succeeded"]
        )
        self.assertEqual(
            events.read(self.config.state_dir, saved[1]["id"])[0]["state"], "succeeded"
        )

    def test_notification_payload_is_minimal_and_failure_retries(self):
        settings = self.root / "notify"
        settings.write_text(json.dumps({"url": "https://example.invalid/private-hook"}))
        settings.chmod(0o600)
        events.configure(self.config.state_dir, settings)
        with self.store.mutation("private-action") as operation:
            self.store.note(operation, "a secret never sent")
        response = MagicMock()
        response.__enter__.return_value.status = 200
        opener = MagicMock()
        opener.open.side_effect = OSError("secret URL")
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "cursor retained"):
                events.deliver(self.config.state_dir)
            opener.open.side_effect = None
            opener.open.return_value = response
            report = events.deliver(self.config.state_dir)
            self.assertEqual(report["delivered"], 1)
            sent = json.loads(opener.open.call_args.args[0].data)
            self.assertEqual(set(sent), {"id", "operation_id", "state", "created_at"})
            self.assertEqual(events.deliver(self.config.state_dir)["delivered"], 0)

    def policy(self, **changes):
        value = {
            "name": "nightly",
            "calendar": "*-*-* 02:00:00 UTC",
            "action": "check-updates",
            "window_start": "01:00",
            "window_end": "04:00",
            "days": [1, 2, 3, 4, 5, 6, 7],
        }
        value.update(changes)
        return value

    def save_policy(self, value):
        directory = self.config.state_dir / "schedules"
        directory.mkdir(exist_ok=True)
        write_json(directory / "nightly.json", value)

    def test_window_day_boundaries_and_no_catchup(self):
        value = scheduling.policy(
            self.policy(window_start="23:00", window_end="02:00", days=[1])
        )
        monday = datetime(2026, 10, 19, 23, 30, tzinfo=timezone.utc)
        tuesday = datetime(2026, 10, 20, 1, 30, tzinfo=timezone.utc)
        self.assertTrue(scheduling.in_window(value, monday))
        self.assertTrue(scheduling.in_window(value, tuesday))
        self.assertFalse(scheduling.in_window(value, tuesday.replace(hour=2)))
        self.save_policy(value)
        with (
            patch("bsm_supervisor.scheduling.datetime") as clock,
            patch("bsm_supervisor.scheduling.discover") as discover,
        ):
            clock.now.return_value = tuesday.replace(hour=6)
            self.assertEqual(
                scheduling.run(self.config, self.store, "nightly")["state"], "skipped"
            )
            discover.assert_not_called()
        self.assertIn(
            "Persistent=false", scheduling.units(self.root / "config", value)[1]
        )

    def test_policy_rejects_unapproved_mutation_and_unit_injection(self):
        for changes in (
            {"action": "upgrade"},
            {"calendar": "daily\nExecStart=bad UTC"},
            {"days": [True]},
            {"name": "../../bad"},
            {"action": "export", "destination": "relative"},
            {"action": "prune", "maintenance_confirmed": True, "keep": 0},
        ):
            with self.assertRaises(ValueError):
                scheduling.policy(self.policy(**changes))

    def test_scheduled_checks_no_downgrade_and_failed_run_needs_ack(self):
        self.save_policy(
            self.policy(
                action="upgrade", maintenance_confirmed=True, accept_migrations=True
            )
        )
        with (
            patch("bsm_supervisor.scheduling.datetime") as clock,
            patch(
                "bsm_supervisor.scheduling.discover", return_value=[Release("4.0.0")]
            ),
            patch(
                "bsm_supervisor.scheduling.selection", return_value={"version": "4.1.0"}
            ),
            patch("bsm_supervisor.scheduling.upgrades.upgrade") as upgrade,
        ):
            clock.now.return_value = datetime(2026, 10, 19, 2, tzinfo=timezone.utc)
            self.assertEqual(
                scheduling.run(self.config, self.store, "nightly")["state"], "succeeded"
            )
            upgrade.assert_not_called()
            with patch(
                "bsm_supervisor.scheduling.discover",
                side_effect=ValueError("network error"),
            ):
                with self.assertRaisesRegex(ValueError, "network"):
                    scheduling.run(self.config, self.store, "nightly")
            with self.assertRaisesRegex(ValueError, "interrupted"):
                scheduling.run(self.config, self.store, "nightly")
            scheduling.acknowledge(self.config, self.store, "nightly")
            self.assertEqual(
                scheduling.run(self.config, self.store, "nightly")["state"], "succeeded"
            )

    def test_pending_operation_prevents_scheduled_shutdown(self):
        self.save_policy(self.policy(action="checkpoint", maintenance_confirmed=True))
        self.store.begin("interrupted")
        with (
            patch("bsm_supervisor.scheduling.datetime") as clock,
            patch("bsm_supervisor.scheduling.runtime.shutdown") as shutdown,
        ):
            clock.now.return_value = datetime(2026, 10, 19, 2, tzinfo=timezone.utc)
            with self.assertRaises(BusyError):
                scheduling.run(self.config, self.store, "nightly")
            shutdown.assert_not_called()
