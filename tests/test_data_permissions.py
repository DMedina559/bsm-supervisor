"""Regression coverage for root-owned runtime data and scope boundaries."""

import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bsm_supervisor.config import Config
from bsm_supervisor.data_permissions import repair
from bsm_supervisor.store import Store


class DataPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(root / "state", root / "installations", root / "data")
        self.config.config_dir.mkdir(parents=True)
        self.store = Store(self.config.state_dir)
        self.backup = self.config.data_dir / "backups" / "preview"
        self.backup.mkdir(parents=True)
        self.backup.chmod(0o500)
        self.secret = self.config.config_dir / "secret"
        self.secret.write_text("private")
        self.secret.chmod(0o400)
        self.uid = os.getuid() or 65534
        self.gid = os.getgid() or 65534

    def run_repair(self):
        with (
            patch("bsm_supervisor.data_permissions.os.geteuid", return_value=0),
            patch(
                "bsm_supervisor.data_permissions.offline_guard",
                return_value=nullcontext(),
            ),
            patch("bsm_supervisor.data_permissions.service_backend") as service,
            patch(
                "bsm_supervisor.data_permissions.pwd.getpwnam",
                return_value=SimpleNamespace(pw_uid=self.uid, pw_gid=self.gid),
            ),
            patch("bsm_supervisor.data_permissions.os.chown") as chown,
        ):
            service.return_value.status.return_value = {
                "User": "custom-bsm",
                "Group": "",
            }
            result = repair(self.config, self.store)
            return result, chown

    def test_repairs_backup_and_keeps_secrets_private(self):
        result, chown = self.run_repair()
        self.assertEqual(result["user"], "custom-bsm")
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        chown.assert_any_call(self.backup, self.uid, self.gid, follow_symlinks=False)
        self.assertFalse(
            any(
                self.config.state_dir in call.args[0].parents
                for call in chown.call_args_list
            )
        )

    def test_does_not_follow_links_or_change_startup_gate(self):
        outside = self.config.data_dir.parent / "outside"
        outside.write_text("outside")
        outside.chmod(0o400)
        self.backup.chmod(0o700)
        (self.backup / "link").symlink_to(outside)
        marker = self.config.config_dir / "supervisor-maintenance.lock"
        marker.write_text("")
        marker.chmod(0o644)
        _, chown = self.run_repair()
        self.assertEqual(outside.stat().st_mode & 0o777, 0o400)
        self.assertNotIn(marker, [call.args[0] for call in chown.call_args_list])
        self.assertNotIn(
            self.backup / "link", [call.args[0] for call in chown.call_args_list]
        )

    def test_hardlink_rejected_before_changes(self):
        self.backup.chmod(0o700)
        os.link(self.secret, self.backup / "hardlink")
        self.backup.chmod(0o500)
        with self.assertRaisesRegex(ValueError, "Hard-linked"):
            self.run_repair()
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o500)

    def test_active_service_blocks_repair(self):
        with (
            patch("bsm_supervisor.data_permissions.os.geteuid", return_value=0),
            patch("bsm_supervisor.integration.service_backend") as service,
        ):
            service.return_value.status.return_value = {
                "ActiveState": "active",
                "MainPID": "22",
                "Job": "",
            }
            with self.assertRaisesRegex(ValueError, "inactive"):
                repair(self.config, self.store)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o500)

    def test_restore_workspace_rejected_before_changes(self):
        (self.config.data_dir / ".bsm-restore-1-0").mkdir()
        with self.assertRaisesRegex(ValueError, "restore workspaces"):
            self.run_repair()
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o400)

    def test_nested_mount_rejected_before_changes(self):
        original = Path.is_mount
        with patch.object(
            Path, "is_mount", lambda path: path == self.backup or original(path)
        ):
            with self.assertRaisesRegex(ValueError, "Nested runtime mount"):
                self.run_repair()
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o400)

    def test_doctor_detects_unwritable_server_backup(self):
        from bsm_supervisor import diagnostics

        with (
            patch("bsm_supervisor.diagnostics.service_backend") as service,
            patch(
                "bsm_supervisor.diagnostics.pwd.getpwnam",
                return_value=SimpleNamespace(pw_uid=65534, pw_gid=65534),
            ),
            patch("bsm_supervisor.diagnostics.os.getgrouplist", return_value=[65534]),
        ):
            service.return_value.status.return_value = {"User": "bsm"}
            report = diagnostics.doctor(self.config, check_health=False)
        check = next(
            item
            for item in report["checks"]
            if item["code"] == "service-access:backup:preview"
        )
        self.assertFalse(check["ok"])
        self.assertIn("permissions-repair", check["remedy"])
