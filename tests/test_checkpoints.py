"""Test actual checkpoint bytes, SQLite WAL snapshots, and restore recovery."""

import fcntl
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor import checkpoints, restoration
from bsm_supervisor.checkpoint_files import read_json, write_json
from bsm_supervisor.cli import main
from bsm_supervisor.config import Config
from bsm_supervisor.integration import inspect
from bsm_supervisor.service import guard_command, managed_command
from bsm_supervisor.service_environment import write_environment
from bsm_supervisor.store import BusyError, Store


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(root / "state", root / "envs", root / "data")
        self.config.config_dir.mkdir(parents=True)
        (self.config.data_dir / "servers").mkdir()
        (self.config.data_dir / "servers/world.dat").write_bytes(b"original world")
        self.database = self.config.config_dir / "bedrock-server-manager.db"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""
                CREATE TABLE settings(key TEXT, value TEXT);
                CREATE TABLE alembic_version(version_num TEXT);
                INSERT INTO alembic_version VALUES ('revision_1');
                CREATE TABLE content(value TEXT);
                INSERT INTO content VALUES ('saved');
            """)
        self.store = Store(self.config.state_dir)
        write_environment(self.config)
        self.service = patch("bsm_supervisor.integration.service_backend")
        self.mock_service = self.service.start()
        self.addCleanup(self.service.stop)
        self.mock_service.return_value.status.return_value = {
            "ActiveState": "inactive",
            "MainPID": "0",
            "Job": "",
            "UnsetEnvironment": (
                "BSM_DB_URL BSM_DATA_DIR BSM_CONFIG_DIR "
                "BSM_PLUGIN_RECURSION_GUARD BSM_LOG_LEVEL"
            ),
            "EnvironmentFiles": str(self.config.state_dir / "bsm-runtime.env")
            + " (ignore_errors=no)",
            "ExecCondition": (
                "{ path=/usr/bin/test ; argv[]=/usr/bin/test ! -e "
                f"{self.config.config_dir}/supervisor-recovery.required ; }} "
                f"{{ argv[]={guard_command(self.config)} ; }}"
            ),
            "ExecStart": "{ path=/usr/bin/flock ; argv[]="
            + managed_command(self.config)
            + " ; }",
        }

    def checkpoint(self):
        return checkpoints.create(self.config, self.store)["id"]

    def operation(self):
        return self.store.operations()[0]["id"]

    def test_snapshot_manifest_and_payload_integrity(self):
        checkpoint_id = self.checkpoint()
        manifest = checkpoints.verify(self.config, checkpoint_id)
        self.assertEqual(manifest["schema_revision"], ["revision_1"])
        names = [item["name"] for item in manifest["roots"][0]["entries"]]
        self.assertNotIn("config/web_server.lock", names)
        self.assertEqual(len(checkpoints.list_checkpoints(self.config)), 1)
        payload = (
            checkpoints.checkpoint_path(self.config, checkpoint_id)
            / "payload/0/servers/world.dat"
        )
        payload.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "integrity"):
            checkpoints.verify(self.config, checkpoint_id)

    def test_wal_commits_are_included_without_restoring_sidecars(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO content VALUES ('committed in WAL')")
        connection.commit()
        self.assertTrue(Path(str(self.database) + "-wal").exists())
        checkpoint_id = self.checkpoint()
        snapshot = (
            checkpoints.checkpoint_path(self.config, checkpoint_id)
            / "payload/0/config/bedrock-server-manager.db"
        )
        with closing(sqlite3.connect(snapshot)) as db, db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM content").fetchone()[0], 2
            )
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        manifest = checkpoints.verify(self.config, checkpoint_id)
        names = [item["name"] for item in manifest["roots"][0]["entries"]]
        self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in names))

    def test_external_settings_require_explicit_coverage(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        (external / "plugin.py").write_text("# saved plugin")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO settings VALUES (?, ?)",
                ("paths.plugins", json.dumps(str(external))),
            )
        report = inspect(self.config)
        self.assertFalse(report["supported"])
        self.assertIn("paths.plugins", report["blockers"][0])
        with self.assertRaises(ValueError):
            self.checkpoint()
        configured = replace(self.config, checkpoint_paths=(external,))
        manifest = checkpoints.create(configured, self.store)
        self.assertEqual(len(manifest["roots"]), 2)
        checkpoints.verify(configured, manifest["id"])

    def test_external_database_is_not_silently_excluded_or_credentials_printed(self):
        secret = "postgresql://username:do-not-print@db.example/bsm"
        (self.config.config_dir / "bedrock_server_manager.json").write_text(
            json.dumps({"db_url": secret})
        )
        report = inspect(self.config)
        self.assertFalse(report["supported"])
        self.assertNotIn(secret, json.dumps(report))
        with self.assertRaisesRegex(ValueError, "SQLite"):
            self.checkpoint()
        self.assertEqual(checkpoints.list_checkpoints(self.config), [])

    def test_internal_symlinks_preserved_and_uncovered_external_link_rejected(self):
        link = self.config.data_dir / "world-link"
        link.symlink_to("servers/world.dat")
        checkpoint_id = self.checkpoint()
        checkpoints.verify(self.config, checkpoint_id)
        link.unlink()
        link.symlink_to(Path(self.temp.name) / "not-covered")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.checkpoint()

    def test_runtime_lock_prevents_capture_even_when_service_looks_inactive(self):
        lock = self.config.config_dir / "web_server.lock"
        with lock.open("wb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "runtime owns"):
                self.checkpoint()
        self.assertEqual(checkpoints.list_checkpoints(self.config), [])

    def test_active_and_unmanaged_services_are_rejected(self):
        state = self.mock_service.return_value.status.return_value
        state["ActiveState"] = "active"
        with self.assertRaisesRegex(ValueError, "inactive"):
            self.checkpoint()
        state["ActiveState"] = "inactive"
        state["ExecStart"] = "/usr/bin/python"
        with self.assertRaisesRegex(ValueError, "managed"):
            self.checkpoint()

    def test_restore_replaces_contents_preserves_lock_and_retains_originals(self):
        checkpoint_id = self.checkpoint()
        lock = self.config.config_dir / "web_server.lock"
        lock_inode = lock.stat().st_ino
        world = self.config.data_dir / "servers/world.dat"
        world.write_bytes(b"newer world")
        (self.config.data_dir / "new-file").write_text("new")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE content SET value = 'newer'")
        result = restoration.restore(self.config, self.store, checkpoint_id)
        self.assertEqual(world.read_bytes(), b"original world")
        self.assertFalse((self.config.data_dir / "new-file").exists())
        self.assertEqual(lock.stat().st_ino, lock_inode)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(
                db.execute("SELECT value FROM content").fetchone()[0], "saved"
            )
        workspace = self.config.data_dir / (f".bsm-restore-{result['operation_id']}-0")
        self.assertEqual(
            (workspace / "old/servers/world.dat").read_bytes(), b"newer world"
        )
        restoration.cleanup(self.config, self.store, result["operation_id"])
        self.assertFalse(workspace.exists())
        checkpoints.verify(self.config, checkpoint_id)
        self.assertEqual(world.read_bytes(), b"original world")

    def test_restore_failure_blocks_mutations_and_recovery_reverses_renames(self):
        checkpoint_id = self.checkpoint()
        world = self.config.data_dir / "servers/world.dat"
        world.write_bytes(b"newer world")
        move = restoration._move
        calls = []

        def fail_after_move(source, destination):
            move(source, destination)
            calls.append(source)
            if len(calls) == 3:
                raise OSError("simulated crash after rename")

        with patch("bsm_supervisor.restoration._move", side_effect=fail_after_move):
            with self.assertRaisesRegex(OSError, "simulated"):
                restoration.restore(self.config, self.store, checkpoint_id)
        operation_id = self.operation()
        with self.assertRaises(BusyError):
            self.store.begin("start")
        with self.assertRaisesRegex(ValueError, "restore-recover"):
            self.store.acknowledge(operation_id)
        state = restoration.recover(self.config, self.store, operation_id)
        self.assertEqual(state, "rolled_back")
        self.assertEqual(world.read_bytes(), b"newer world")
        restoration.cleanup(self.config, self.store, operation_id)
        with self.store.mutation("next"):
            pass

    def test_failure_before_staging_can_be_reconciled(self):
        checkpoint_id = self.checkpoint()
        payload = (
            checkpoints.checkpoint_path(self.config, checkpoint_id)
            / "payload/0/servers/world.dat"
        )
        payload.write_text("corrupt")
        with self.assertRaises(ValueError):
            restoration.restore(self.config, self.store, checkpoint_id)
        self.assertEqual(
            restoration.recover(self.config, self.store, self.operation()),
            "not_started",
        )

    def test_manifest_traversal_and_scope_change_rejected_before_restore(self):
        checkpoint_id = self.checkpoint()
        manifest_path = (
            checkpoints.checkpoint_path(self.config, checkpoint_id) / "manifest.json"
        )
        manifest = read_json(manifest_path)
        manifest["roots"][0]["entries"][1]["name"] = "../escape"
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            checkpoints.verify(self.config, checkpoint_id)
        manifest["roots"][0]["path"] = "/etc"
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "coverage"):
            checkpoints.verify(self.config, checkpoint_id)

    def test_retention_keeps_pins_recent_and_installed_recovery_target(self):
        old = self.checkpoint()
        checkpoints.pin(self.config, self.store, old, True)
        second = self.checkpoint()
        newest = self.checkpoint()
        removed = checkpoints.prune(self.config, self.store, 1)
        self.assertEqual(removed, [second])
        self.assertEqual(
            {item["id"] for item in checkpoints.list_checkpoints(self.config)},
            {old, newest},
        )

    def test_real_process_exit_during_restore_is_recoverable(self):
        checkpoint_id = self.checkpoint()
        world = self.config.data_dir / "servers/world.dat"
        world.write_bytes(b"newer world")
        state = self.mock_service.return_value.status.return_value
        script = (
            "import os; from pathlib import Path; from unittest.mock import patch; "
            "from bsm_supervisor import restoration; "
            "from bsm_supervisor.config import Config; "
            "from bsm_supervisor.store import Store; "
            f"config = Config(Path({str(self.config.state_dir)!r}), "
            f"Path({str(self.config.installations_dir)!r}), "
            f"Path({str(self.config.data_dir)!r})); "
            f"patch('bsm_supervisor.service.SystemdService.status', "
            f"return_value={state!r}).start(); "
            "original = restoration._move; "
            "restoration._move = lambda source, destination: "
            "(original(source, destination), os._exit(17)); "
            f"restoration.restore(config, Store(config.state_dir), {checkpoint_id!r})"
        )
        process = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(process.returncode, 17)
        marker = self.config.config_dir / "supervisor-recovery.required"
        self.assertTrue(marker.exists())
        condition = subprocess.run(
            ["/usr/bin/test", "!", "-e", str(marker)], check=False
        )
        self.assertEqual(condition.returncode, 1)
        reopened = Store(self.config.state_dir)
        operation_id = reopened.operations()[0]["id"]
        self.assertEqual(
            restoration.recover(self.config, reopened, operation_id), "rolled_back"
        )
        self.assertEqual(world.read_bytes(), b"newer world")
        self.assertFalse(marker.exists())
        restoration.cleanup(self.config, reopened, operation_id)

    def test_preparation_failure_keeps_all_roots_recoverable(self):
        external = Path(self.temp.name) / "other-root"
        external.mkdir()
        configured = replace(self.config, checkpoint_paths=(external,))
        # The launch paths are unchanged by an extra checkpoint root.
        checkpoint_id = checkpoints.create(configured, self.store)["id"]
        original = restoration.shutil.copytree
        count = []

        def fail_second(*args, **kwargs):
            count.append(1)
            if len(count) == 2:
                raise OSError("staging failure")
            return original(*args, **kwargs)

        with patch(
            "bsm_supervisor.restoration.shutil.copytree", side_effect=fail_second
        ):
            with self.assertRaisesRegex(OSError, "staging failure"):
                restoration.restore(configured, self.store, checkpoint_id)
        operation_id = self.operation()
        self.assertEqual(
            restoration.recover(configured, self.store, operation_id), "rolled_back"
        )
        restoration.cleanup(configured, self.store, operation_id)
        self.assertEqual(
            (self.config.data_dir / "servers/world.dat").read_bytes(), b"original world"
        )

    def test_launch_lock_blocks_python_before_import(self):
        from bsm_supervisor.integration import offline_guard

        marker = Path(self.temp.name) / "python-started"
        lock = self.config.config_dir / "supervisor-maintenance.lock"
        command = [
            "/usr/bin/flock",
            "--shared",
            "--nonblock",
            "--no-fork",
            str(lock),
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close()",
        ]
        with offline_guard(self.config):
            process = subprocess.run(command, check=False)
            self.assertEqual(process.returncode, 1)
            self.assertFalse(marker.exists())
        process = subprocess.run(command, check=False)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(marker.exists())

    def test_installed_version_keeps_its_latest_recovery_checkpoint(self):
        target = self.config.installations_dir / "release"
        (target / "bin").mkdir(parents=True)
        (target / "pyvenv.cfg").touch()
        (target / "bin/bedrock-server-manager").touch()
        self.store.register("release", "4.0.0b7", target, self.config.installations_dir)
        current = self.config.installations_dir / "current"
        current.symlink_to("release")
        old = self.checkpoint()
        current.unlink()
        newest = self.checkpoint()
        self.assertEqual(checkpoints.prune(self.config, self.store, 1), [])
        self.assertEqual(
            {item["id"] for item in checkpoints.list_checkpoints(self.config)},
            {old, newest},
        )

    def test_unknown_layout_and_missing_database_fail_closed(self):
        self.database.unlink()
        report = inspect(self.config)
        self.assertFalse(report["supported"])
        with self.assertRaisesRegex(ValueError, "initialized"):
            self.checkpoint()

    def test_mount_root_inode_is_preserved_and_nested_mount_is_rejected(self):
        root_inode = self.config.data_dir.stat().st_ino
        checkpoint_id = self.checkpoint()
        result = restoration.restore(self.config, self.store, checkpoint_id)
        self.assertEqual(self.config.data_dir.stat().st_ino, root_inode)
        restoration.cleanup(self.config, self.store, result["operation_id"])
        mounted = self.config.data_dir / "servers"
        with patch(
            "bsm_supervisor.checkpoint_files.mount_points", return_value={mounted}
        ):
            with self.assertRaisesRegex(ValueError, "Nested mounts"):
                self.checkpoint()

    def test_configuration_rejects_private_coverage_and_reads_external_paths(self):
        path = Path(self.temp.name) / "settings.toml"
        path.write_text(
            f'state_dir = "{self.config.state_dir}"\n'
            f'installations_dir = "{self.config.installations_dir}"\n'
            f'data_dir = "{self.config.data_dir}"\n'
            f'bsm_config_dir = "{self.config.config_dir}"\n'
            'checkpoint_paths = ["/mnt/worlds"]\n'
        )
        configured = Config.load(path)
        self.assertEqual(configured.checkpoint_paths, (Path("/mnt/worlds"),))
        with self.assertRaisesRegex(ValueError, "overlap"):
            replace(self.config, checkpoint_paths=(self.config.state_dir,))

    def test_cli_capture_verify_and_restore_plan(self):
        config_path = Path(self.temp.name) / "supervisor.toml"
        config_path.write_text(
            f'state_dir = "{self.config.state_dir}"\n'
            f'installations_dir = "{self.config.installations_dir}"\n'
            f'data_dir = "{self.config.data_dir}"\n'
        )
        prefix = ["--config", str(config_path)]
        capture = io.StringIO()
        with redirect_stdout(capture):
            self.assertEqual(
                main(prefix + ["checkpoint-create", "--offline-confirmed"]), 0
            )
        checkpoint_id = json.loads(capture.getvalue())["id"]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(prefix + ["restore-plan", checkpoint_id]), 0)
        plan = json.loads(output.getvalue())
        self.assertFalse(plan["changes_version_selection"])
        self.assertIn("discard newer data", plan["effect"])

    def test_external_sqlite_database_and_plugin_files_restore_together(self):
        external = Path(self.temp.name) / "external-db"
        external.mkdir()
        moved_database = external / "bsm.db"
        self.database.rename(moved_database)
        (external / "plugin.py").write_text("saved plugin")
        (self.config.config_dir / "bedrock_server_manager.json").write_text(
            json.dumps({"db_url": "sqlite:///" + str(moved_database)})
        )
        configured = replace(self.config, checkpoint_paths=(external,))
        checkpoint_id = checkpoints.create(configured, self.store)["id"]
        with closing(sqlite3.connect(moved_database)) as db, db:
            db.execute("UPDATE content SET value = 'changed'")
        (external / "plugin.py").write_text("changed plugin")
        result = restoration.restore(configured, self.store, checkpoint_id)
        with closing(sqlite3.connect(moved_database)) as db, db:
            self.assertEqual(
                db.execute("SELECT value FROM content").fetchone()[0], "saved"
            )
        self.assertEqual((external / "plugin.py").read_text(), "saved plugin")
        restoration.cleanup(configured, self.store, result["operation_id"])

    def test_permissions_and_owner_are_restored(self):
        world = self.config.data_dir / "servers/world.dat"
        world.chmod(0o640)
        checkpoint_id = self.checkpoint()
        world.chmod(0o600)
        result = restoration.restore(self.config, self.store, checkpoint_id)
        self.assertEqual(world.stat().st_mode & 0o777, 0o640)
        self.assertEqual(world.stat().st_uid, os.geteuid())
        restoration.cleanup(self.config, self.store, result["operation_id"])


if __name__ == "__main__":
    unittest.main()
