"""Regressions for public release recovery, privacy and unattended operation."""

import fcntl
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_administration as administration
import test_management as management

from bsm_supervisor import events, retention, scheduling, self_update, transfer, web
from bsm_supervisor.checkpoint_files import read_json, write_json
from bsm_supervisor.store import BusyError


class TransferHardeningTests(unittest.TestCase):
    setUp = management.TransferTests.setUp
    checkpoint = management.TransferTests.checkpoint

    def test_encrypted_export_stages_plaintext_only_in_private_local_storage(self):
        saved = self.checkpoint()
        destination = Path(self.temp.name) / "remote"
        destination.mkdir()
        password = Path(self.temp.name) / "password"
        password.write_text("a long private passphrase")
        password.chmod(0o600)
        encrypt = transfer._crypt

        def inspect(source, target, secret, decrypt):
            self.assertIn(self.config.state_dir, source.parents)
            self.assertIn(destination, target.parents)
            self.assertEqual(list(destination.rglob("*.zip")), [])
            return encrypt(source, target, secret, decrypt)

        with patch("bsm_supervisor.transfer._crypt", side_effect=inspect):
            transfer.export_checkpoint(
                self.config, self.store, saved, destination / "backup.enc", password
            )
        self.assertEqual([path.name for path in destination.iterdir()], ["backup.enc"])

    def test_low_space_and_missing_mount_fail_before_publication(self):
        saved = self.checkpoint()
        output = Path(self.temp.name) / "archive"
        with patch(
            "bsm_supervisor.transfer.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaisesRegex(ValueError, "free space"):
                transfer.export_checkpoint(self.config, self.store, saved, output)
        self.assertFalse(output.exists())
        with (
            patch("bsm_supervisor.transfer.mount_points", return_value=set()),
            patch("bsm_supervisor.transfer.os.path.ismount", return_value=False),
        ):
            with self.assertRaisesRegex(ValueError, "not mounted"):
                transfer.export_checkpoint(
                    self.config, self.store, saved, output, require_mount=output.parent
                )
        self.assertFalse(output.exists())

    def test_mount_guard_rejects_unrelated_destination(self):
        anchor = Path(self.temp.name) / "mount"
        anchor.mkdir()
        with patch("bsm_supervisor.transfer.mount_points", return_value={anchor}):
            with self.assertRaisesRegex(ValueError, "outside"):
                transfer.destination_guard(Path(self.temp.name) / "elsewhere", anchor)


class ConsoleHardeningTests(unittest.TestCase):
    setUp = management.ManagementTests.setUp
    request = management.ManagementTests.request

    def test_live_job_lease_blocks_acknowledgement_before_child_starts(self):
        job_id = "c" * 32
        path = self.console.jobs / (job_id + ".json")
        write_json(path, {"id": job_id, "state": "running"})
        with path.with_suffix(".lock").open("w") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(BusyError, "executing"):
                web.acknowledge(self.config, self.store, job_id)
        web.acknowledge(self.config, self.store, job_id)
        self.assertEqual(read_json(path)["state"], "acknowledged")

    def test_jobs_are_sorted_by_creation_not_uuid(self):
        for name, timestamp in (
            ("f" * 32, "2026-01-01T00:00:00+00:00"),
            ("0" * 32, "2026-10-01T00:00:00+00:00"),
        ):
            write_json(
                self.console.jobs / (name + ".json"),
                {"id": name, "created_at": timestamp, "state": "succeeded"},
            )
        with patch("bsm_supervisor.web.SystemdService"):
            jobs = self.console.snapshot()["jobs"]
        self.assertEqual(jobs[0]["id"], "0" * 32)

    def test_timeout_terminates_child_and_retains_explicit_recovery_state(self):
        self.console.config = replace(self.config, web_job_timeout=1)
        job_id = "d" * 32
        job = {"id": job_id, "action": "start", "state": "running"}
        lease = os.open(
            self.console.jobs / (job_id + ".lock"), os.O_CREAT | os.O_RDWR, 0o600
        )
        self.console.lock.acquire()
        real_popen = subprocess.Popen
        children = []

        def popen(args, **kwargs):
            process = real_popen(
                [
                    sys.executable,
                    "-c",
                    "import time; print('started',flush=True); time.sleep(30)",
                ],
                **kwargs,
            )
            children.append(process)
            return process

        with patch("bsm_supervisor.web.subprocess.Popen", side_effect=popen):
            self.console.execute(job, ["start"], lease)
        self.assertIsNotNone(children[0].poll())
        result = read_json(self.console.jobs / (job_id + ".json"))
        self.assertEqual(result["state"], "interrupted")
        web.acknowledge(self.config, self.store, job_id)

    def test_busy_response_has_distinct_status(self):
        self.console.lock.acquire()
        try:
            status, body, _ = self.request("/api/actions", "POST", {"action": "start"})
        finally:
            self.console.lock.release()
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(body))

    def test_notify_schedule_automatically_retries_transport_failures(self):
        folder = self.config.state_dir / "schedules"
        folder.mkdir()
        write_json(
            folder / "notify.json",
            {
                "name": "notify",
                "action": "notify",
                "calendar": "*-*-* 02:00:00 UTC",
                "window_start": "01:00",
                "window_end": "04:00",
                "days": [1, 2, 3, 4, 5, 6, 7],
            },
        )
        with (
            patch("bsm_supervisor.scheduling.datetime") as clock,
            patch(
                "bsm_supervisor.scheduling.events.deliver",
                side_effect=[events.DeliveryError("network"), {"delivered": 1}],
            ) as deliver,
        ):
            clock.now.return_value = datetime(2026, 10, 19, 2, tzinfo=timezone.utc)
            self.assertEqual(
                scheduling.run(self.config, self.store, "notify")["state"], "retry"
            )
            self.assertEqual(
                scheduling.run(self.config, self.store, "notify")["state"], "succeeded"
            )
        self.assertEqual(deliver.call_count, 2)

    def test_journal_retention_preserves_pending_and_undelivered_events(self):
        with self.store.mutation("old-completed"):
            pass
        pending = self.store.begin("pending")
        with self.store.connect() as db:
            db.execute("UPDATE events SET created_at='2000-01-01T00:00:00Z'")
        write_json(
            self.config.state_dir / "notifications.json",
            {"url": "https://example.invalid/webhook"},
        )
        # Retention cannot operate across an unresolved mutation.
        with self.assertRaises(BusyError):
            retention.prune(self.config, self.store, 1, 1)
        self.store.acknowledge(pending)
        result = retention.prune(self.config, self.store, 1, 1)
        self.assertEqual(result["removed_events"], 0)
        write_json(self.config.state_dir / "notification-cursor.json", {"after": 1000})
        result = retention.prune(self.config, self.store, 1, 1)
        self.assertGreater(result["removed_events"], 0)

    def test_job_retention_keeps_recent_and_interrupted_jobs(self):
        for count, state in enumerate(
            ("succeeded", "failed", "interrupted", "succeeded")
        ):
            job_id = str(count) * 32
            write_json(
                self.console.jobs / (job_id + ".json"),
                {
                    "id": job_id,
                    "state": state,
                    "created_at": f"2000-01-0{count+1}T00:00:00+00:00",
                },
            )
            (self.console.jobs / (job_id + ".log")).write_text("log")
        result = retention.prune(self.config, self.store, 1, 1)
        self.assertEqual(result["removed_jobs"], 2)
        self.assertTrue((self.console.jobs / ("2" * 32 + ".json")).exists())
        self.assertTrue((self.console.jobs / ("3" * 32 + ".json")).exists())


class ReleaseVersionTests(unittest.TestCase):
    setUp = administration.AdministrationTests.setUp
    stopped = administration.AdministrationTests.stopped
    update_fixture = administration.AdministrationTests.update_fixture

    def test_latest_update_pins_commit_and_preserves_release_version(self):
        prefix, target, state, commands, command = self.update_fixture()
        sha = "e" * 40
        with (
            patch("bsm_supervisor.self_update.PREFIX", prefix),
            patch(
                "bsm_supervisor.self_update.fetch",
                side_effect=[
                    json.dumps(
                        {"tag_name": "v0.1.0", "draft": False, "prerelease": False}
                    ).encode(),
                    json.dumps([{"sha": sha}]).encode(),
                    b"installer",
                ],
            ) as fetch,
            patch("bsm_supervisor.self_update.execute", side_effect=command),
            patch("bsm_supervisor.service.SystemdService.status", return_value=state),
            patch("bsm_supervisor.self_update.runtime.shutdown"),
            patch.dict(os.environ, {"GH_TOKEN": ""}),
            patch("bsm_supervisor.self_update.getpass.getpass") as prompt,
        ):
            plan = self_update.update(
                self.config, self.config_file, self.store, "latest"
            )
        prompt.assert_not_called()
        self.assertEqual(fetch.call_args_list[0].args[0], "/releases/latest")
        self.assertEqual(plan["revision"], sha)
        self.assertEqual(plan["package_version"], "0.1.0")
        self.assertIn("--package-version", commands[0][0])
        self.assertIn("0.1.0", commands[0][0])

    def test_embedded_installer_version_program_sets_metadata_in_copy(self):
        program = (
            Path("scripts/install-lxc.sh")
            .read_text()
            .split("<<'PYVERSION'\n", 1)[1]
            .split("\nPYVERSION\n", 1)[0]
        )
        target = self.root / "source"
        (target / "src/bsm_supervisor").mkdir(parents=True)
        module = target / "src/bsm_supervisor/__init__.py"
        module.write_text('__version__ = "0.1.0.dev0"\n')
        result = subprocess.run(
            [sys.executable, "-I", "-c", program, str(target), "0.1.0b1"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(module.read_text(), '__version__ = "0.1.0b1"\n')
