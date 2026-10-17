"""Exercise workflow composition against real checkpoints and simulated services."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor import checkpoints, launchguard, restoration, runtime, upgrades
from bsm_supervisor.activation import selection
from bsm_supervisor.config import Config
from bsm_supervisor.releases import Release
from bsm_supervisor.service import guard_command, managed_command
from bsm_supervisor.service_environment import write_environment
from bsm_supervisor.store import BusyError, Store


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(
            root / "state",
            root / "envs",
            root / "data",
            api_token_file=root / "state/token",
        )
        self.config.config_dir.mkdir(parents=True)
        self.store = Store(self.config.state_dir)
        write_environment(self.config)
        self.config.api_token_file.write_text("private-token")
        self.config.api_token_file.chmod(0o600)
        self.database = self.config.config_dir / "bedrock-server-manager.db"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE settings(key TEXT, value TEXT);
                CREATE TABLE alembic_version(version_num TEXT);
                INSERT INTO alembic_version VALUES ('old-schema');
                CREATE TABLE content(value TEXT);
                INSERT INTO content VALUES ('old-data');
            """)
        (self.config.data_dir / "world").write_text("old-world")
        for name, version in (("original", "4.0.0b6"), ("release-4.0.0b7", "4.0.0b7")):
            directory = self.config.installations_dir / name
            (directory / "bin").mkdir(parents=True)
            for file in ("bin/python", "bin/bedrock-server-manager", "pyvenv.cfg"):
                (directory / file).touch()
            self.store.register(name, version, directory, self.config.installations_dir)
        (self.config.installations_dir / "current").symlink_to("original")
        self.state = {
            "ActiveState": "active",
            "MainPID": "123",
            "Job": "",
            "UnsetEnvironment": (
                "BSM_DB_URL BSM_DATA_DIR BSM_CONFIG_DIR "
                "BSM_PLUGIN_RECURSION_GUARD BSM_LOG_LEVEL"
            ),
            "EnvironmentFiles": str(self.config.state_dir / "bsm-runtime.env")
            + " (ignore_errors=no)",
            "ExecStart": "{ path=/usr/bin/flock ; argv[]="
            + managed_command(self.config)
            + " ; }",
            "ExecCondition": "{ argv[]=/usr/bin/test ! -e "
            + str(self.config.config_dir / "supervisor-recovery.required")
            + " ; } { argv[]="
            + guard_command(self.config)
            + " ; }",
        }
        self.fail_readiness = False
        self.service = self
        self.patches = [
            patch("bsm_supervisor.upgrades.SystemdService", return_value=self),
            patch("bsm_supervisor.integration.SystemdService", return_value=self),
            patch("bsm_supervisor.upgrades._verify_environment"),
            patch(
                "bsm_supervisor.upgrades.discover", return_value=[Release("4.0.0b7")]
            ),
            patch("bsm_supervisor.runtime.ready", side_effect=self.ready),
            patch("bsm_supervisor.runtime.wait_ready", side_effect=self.ready),
            patch("bsm_supervisor.runtime.shutdown", side_effect=self.shutdown),
            patch(
                "bsm_supervisor.upgrades.permit_path", return_value=root / "run/permit"
            ),
            patch(
                "bsm_supervisor.launchguard.permit_path",
                return_value=root / "run/permit",
            ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def status(self):
        return self.state.copy()

    def reset_failed(self):
        self.state.update(ActiveState="inactive", MainPID="0")

    def shutdown(self, config):
        self.state.update(ActiveState="inactive", MainPID="0")

    def control(self, action):
        self.assertEqual(action, "start")
        self.assertTrue(launchguard.allowed(self.config.config_dir))
        self.state.update(ActiveState="active", MainPID="123")
        if selection(self.config, self.store)["version"] == "4.0.0b7":
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE content SET value = 'new-data'")
                db.execute("UPDATE alembic_version SET version_num = 'new-schema'")
            (self.config.data_dir / "world").write_text("new-world")

    def ready(self, config, expected):
        self.assertEqual(selection(config, self.store)["version"], expected)
        if self.fail_readiness and expected == "4.0.0b7":
            raise runtime.RuntimeErrorBase("simulated unhealthy startup")
        return {
            "ready": True,
            "version": expected,
            "pid": 123,
            "instance_id": "runtime",
        }

    def workflow_id(self):
        return self.store.workflows()[0]["id"]

    def git_target(self):
        from bsm_supervisor.git_sources import UPSTREAM

        commit = "a" * 40
        name = "git-" + commit
        directory = self.config.installations_dir / name
        (directory / "bin").mkdir(parents=True)
        for file in ("bin/python", "bin/bedrock-server-manager", "pyvenv.cfg"):
            (directory / file).touch()
        self.store.register(name, "4.0.0b7", directory, self.config.installations_dir)
        manifest = dict(
            source="git",
            upstream=UPSTREAM,
            commit=commit,
            version="4.0.0b7",
            path=str(directory),
        )
        (directory / "supervisor-installation.json").write_text(json.dumps(manifest))
        return name, directory, manifest

    def test_git_upgrade_records_commit_and_uses_checkpoint_workflow(self):
        name, _, manifest = self.git_target()
        with patch(
            "bsm_supervisor.upgrades.discover", side_effect=AssertionError("PyPI used")
        ):
            plan = upgrades.upgrade(self.config, self.store, installation_id=name)
        self.assertEqual(plan["phase"], "completed")
        self.assertEqual(plan["target_commit"], manifest["commit"])
        self.assertEqual(selection(self.config, self.store)["id"], name)
        self.assertIsNotNone(plan["checkpoint_id"])

    def test_git_resume_refuses_changed_commit_and_recovers_pinned_target(self):
        name, directory, manifest = self.git_target()
        self.fail_readiness = True
        with self.assertRaises(runtime.RuntimeErrorBase):
            upgrades.upgrade(self.config, self.store, installation_id=name)
        path = directory / "supervisor-installation.json"
        path.write_text(json.dumps({**manifest, "commit": "b" * 40}))
        self.fail_readiness = False
        with self.assertRaisesRegex(ValueError, "provenance"):
            upgrades.recover(self.config, self.store, self.workflow_id(), "resume")
        path.write_text(json.dumps(manifest))
        plan = upgrades.recover(self.config, self.store, self.workflow_id(), "resume")
        self.assertEqual(plan["target_commit"], manifest["commit"])
        self.assertEqual(plan["phase"], "completed")

    def test_git_resume_never_downloads_a_missing_pinned_installation(self):
        name, _, _ = self.git_target()
        with patch(
            "bsm_supervisor.upgrades.checkpoints.create",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                upgrades.upgrade(self.config, self.store, installation_id=name)
        with self.store.connect() as db:
            db.execute("DELETE FROM installations WHERE id = ?", (name,))
        with patch(
            "bsm_supervisor.upgrades.install",
            side_effect=AssertionError("Downloaded replacement"),
        ):
            with self.assertRaisesRegex(
                ValueError, "Pinned Git installation is missing"
            ):
                upgrades.recover(self.config, self.store, self.workflow_id(), "resume")
        self.assertEqual(self.store.workflows()[0]["state"], "running")

    def test_git_restore_returns_to_original_environment_and_data(self):
        name, _, _ = self.git_target()
        self.fail_readiness = True
        with self.assertRaises(runtime.RuntimeErrorBase):
            upgrades.upgrade(self.config, self.store, installation_id=name)
        plan = upgrades.recover(self.config, self.store, self.workflow_id(), "restore")
        self.assertEqual(plan["phase"], "rolled_back")
        self.assertEqual(selection(self.config, self.store)["id"], "original")
        self.assertEqual((self.config.data_dir / "world").read_text(), "old-world")

    def test_git_target_must_be_registered_and_selector_is_exclusive(self):
        for selector, identifier in (
            (None, "missing"),
            ("stable", "missing"),
            (None, None),
        ):
            with self.assertRaises(ValueError):
                upgrades.upgrade(
                    self.config, self.store, selector, installation_id=identifier
                )
        self.assertEqual(self.store.workflows(), [])

    def test_success_protects_checkpoint_and_verifies_running_target(self):
        result = upgrades.upgrade(self.config, self.store, "4.0.0b7")
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(selection(self.config, self.store)["version"], "4.0.0b7")
        self.assertEqual(self.store.workflows()[0]["state"], "succeeded")
        saved = checkpoints.verify(self.config, result["checkpoint_id"])
        self.assertEqual(saved["schema_revision"], ["old-schema"])
        self.assertTrue(checkpoints.list_checkpoints(self.config)[0]["pinned"])
        self.assertTrue(launchguard.allowed(self.config.config_dir))
        self.assertTrue(
            all(
                step["workflow_id"] == result["id"]
                for step in self.store.workflow_operations(result["id"])
            )
        )

    def failed(self):
        self.fail_readiness = True
        with self.assertRaisesRegex(runtime.RuntimeErrorBase, "unhealthy"):
            upgrades.upgrade(self.config, self.store, "4.0.0b7")
        return self.workflow_id()

    def test_failed_upgrade_keeps_new_data_and_blocks_mutations_until_recovery(self):
        workflow_id = self.failed()
        self.assertEqual((self.config.data_dir / "world").read_text(), "new-world")
        self.assertEqual(selection(self.config, self.store)["version"], "4.0.0b7")
        self.assertFalse(launchguard.allowed(self.config.config_dir))
        with self.assertRaises(BusyError):
            Store(self.config.state_dir).begin("another-command")
        with self.assertRaises(BusyError):
            self.store.acknowledge(
                self.store.workflow_operations(workflow_id)[-1]["id"]
            )
        self.assertEqual(upgrades.status(self.config, workflow_id)["phase"], "starting")

    def test_explicit_restore_reverts_checkpoint_and_original_version(self):
        workflow_id = self.failed()
        result = upgrades.recover(self.config, self.store, workflow_id, "restore")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(selection(self.config, self.store)["version"], "4.0.0b6")
        self.assertEqual((self.config.data_dir / "world").read_text(), "old-world")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute("SELECT value FROM content").fetchone()[0], "old-data"
            )
        self.assertTrue(launchguard.allowed(self.config.config_dir))
        self.state.update(ActiveState="inactive", MainPID="0")
        restoration.cleanup(self.config, self.store, result["restore_operation_id"])

    def test_resume_retries_target_without_restoring_old_data(self):
        workflow_id = self.failed()
        self.fail_readiness = False
        result = upgrades.recover(self.config, self.store, workflow_id, "resume")
        self.assertEqual(result["phase"], "completed")
        self.assertEqual((self.config.data_dir / "world").read_text(), "new-world")

    def test_cancel_is_refused_after_new_runtime_was_started(self):
        workflow_id = self.failed()
        with self.assertRaisesRegex(ValueError, "Started upgrades"):
            upgrades.recover(self.config, self.store, workflow_id, "cancel")
        self.assertFalse(launchguard.allowed(self.config.config_dir))

    def test_cancel_before_activation_preserves_original_data(self):
        with patch(
            "bsm_supervisor.upgrades.checkpoints.create",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                upgrades.upgrade(self.config, self.store, "4.0.0b7")
        result = upgrades.recover(self.config, self.store, self.workflow_id(), "cancel")
        self.assertEqual(result["phase"], "cancelled")
        self.assertEqual(selection(self.config, self.store)["version"], "4.0.0b6")
        self.assertEqual((self.config.data_dir / "world").read_text(), "old-world")

    def test_interrupted_data_restore_is_recovered_before_rollback_retry(self):
        workflow_id = self.failed()
        move = restoration._move
        moved = []

        def fail_once(source, destination):
            move(source, destination)
            moved.append(source)
            if len(moved) == 2:
                raise OSError("restore interruption")

        with patch("bsm_supervisor.restoration._move", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "restore interruption"):
                upgrades.recover(self.config, self.store, workflow_id, "restore")
        self.assertFalse(launchguard.allowed(self.config.config_dir))
        result = upgrades.recover(self.config, self.store, workflow_id, "restore")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual((self.config.data_dir / "world").read_text(), "old-world")

    def test_interruption_after_selection_can_cancel_without_data_restoration(self):
        original_activate = upgrades.activate

        def interrupted(*args, **kwargs):
            original_activate(*args, **kwargs)
            raise KeyboardInterrupt

        with patch("bsm_supervisor.upgrades.activate", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                upgrades.upgrade(self.config, self.store, "4.0.0b7")
        self.assertEqual(selection(self.config, self.store)["version"], "4.0.0b7")
        self.assertFalse(launchguard.allowed(self.config.config_dir))
        result = upgrades.recover(self.config, self.store, self.workflow_id(), "cancel")
        self.assertEqual(result["phase"], "cancelled")
        self.assertEqual((self.config.data_dir / "world").read_text(), "old-world")

    def test_same_version_repair_requires_explicit_opt_in(self):
        self.fail_readiness = False
        upgrades.upgrade(self.config, self.store, "4.0.0b7")
        with self.assertRaisesRegex(ValueError, "same-version"):
            upgrades.upgrade(self.config, self.store, "4.0.0b7")
        result = upgrades.upgrade(self.config, self.store, "4.0.0b7", allow_same=True)
        self.assertEqual(result["phase"], "completed")


class LaunchGuardTests(unittest.TestCase):
    def test_real_controller_exit_invalidates_its_permit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "supervisor-upgrade.required").write_text(
                json.dumps({"workflow_id": 1, "workflow_key": "key"})
            )
            permit = root / "permit"
            script = (
                "import json; from pathlib import Path; "
                "from bsm_supervisor.launchguard import controller_pid, "
                "process_generation, boot_identity; "
                f"Path({str(permit)!r}).write_text(json.dumps({{"
                "'workflow_id': 1, 'workflow_key': 'key', 'pid': controller_pid(), "
                "'generation': process_generation(controller_pid()), "
                "'boot_id': boot_identity()}))"
            )
            subprocess.run([sys.executable, "-c", script], check=True)
            with patch("bsm_supervisor.launchguard.permit_path", return_value=permit):
                self.assertFalse(launchguard.allowed(root))

    def test_stale_controller_and_boot_identity_cannot_authorize_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "supervisor-upgrade.required"
            marker.write_text(json.dumps({"workflow_id": 1, "workflow_key": "key"}))
            permit = root / "permit"
            with patch("bsm_supervisor.launchguard.permit_path", return_value=permit):
                self.assertFalse(launchguard.allowed(root))
                record = {
                    "workflow_id": 1,
                    "workflow_key": "key",
                    "pid": launchguard.controller_pid(),
                    "generation": launchguard.process_generation(
                        launchguard.controller_pid()
                    ),
                    "boot_id": launchguard.boot_identity(),
                }
                permit.write_text(json.dumps(record))
                self.assertTrue(launchguard.allowed(root))
                record["generation"] = "stale-generation"
                permit.write_text(json.dumps(record))
                self.assertFalse(launchguard.allowed(root))
                record["generation"] = launchguard.process_generation(
                    launchguard.controller_pid()
                )
                record["boot_id"] = "different-boot"
                permit.write_text(json.dumps(record))
                self.assertFalse(launchguard.allowed(root))
                (root / "supervisor-recovery.required").touch()
                self.assertFalse(launchguard.allowed(root))


if __name__ == "__main__":
    unittest.main()
