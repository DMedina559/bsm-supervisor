"""Exercise setup, read-only diagnostics, readiness, and staged self updates."""

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bsm_supervisor import diagnostics, operations, runtime, self_update
from bsm_supervisor.activation import selection
from bsm_supervisor.checkpoint_files import read_json, write_json
from bsm_supervisor.config import Config
from bsm_supervisor.service import (
    ServiceError,
    guard_command,
    managed_command,
    unset_environment,
)
from bsm_supervisor.setup import setup
from bsm_supervisor.store import BusyError, Store


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        self.operator_uid = os.geteuid()
        root = patch("bsm_supervisor.setup.os.geteuid", return_value=0)
        root.start()
        self.addCleanup(root.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(
            self.root / "state", self.root / "envs", self.root / "data"
        )
        self.store = Store(self.config.state_dir)
        self.units = self.root / "units"
        self.units.mkdir()
        self.config_file = self.root / "config.toml"
        self.config_file.touch()

    def stopped(self):
        return {
            "ActiveState": "inactive",
            "MainPID": "0",
            "Job": "",
            "User": "bsm",
            "ExecStart": "{ path=/usr/bin/flock ; argv[]="
            + managed_command(self.config)
            + " ; }",
            "ExecCondition": (
                "{ argv[]=/usr/bin/test ! -e "
                f"{self.config.config_dir}/supervisor-recovery.required ; }} "
                f"{{ argv[]={guard_command(self.config)} ; }}"
            ),
            "EnvironmentFiles": str(self.config.state_dir / "bsm-runtime.env")
            + " (ignore_errors=no)",
            "UnsetEnvironment": unset_environment(self.config),
        }

    def configured_state(self):
        if (self.units / self.config.service_unit).exists():
            return self.stopped()
        raise ServiceError(f"Service is not installed: {self.config.service_unit}")

    def installation(self):
        path = self.config.installations_dir / "release-test"
        (path / "bin").mkdir(parents=True)
        for name in ("pyvenv.cfg", "bin/python", "bin/bedrock-server-manager"):
            (path / name).touch()
        self.store.register(
            "release-test", "4.0.0b7", path, self.config.installations_dir
        )
        return path

    def test_setup_reuses_inventory_creates_unit_and_is_rerunnable(self):
        path = self.installation()
        account = SimpleNamespace(pw_uid=self.operator_uid, pw_gid=os.getegid())
        with (
            patch("bsm_supervisor.setup.pwd.getpwnam", return_value=account),
            patch(
                "bsm_supervisor.service.SystemdService.status",
                side_effect=self.configured_state,
            ),
            patch("bsm_supervisor.service.SystemdService.reload_units"),
            patch("bsm_supervisor.unit_refresh.UNIT_DIRECTORY", self.units),
            patch(
                "bsm_supervisor.unit_refresh.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0),
            ),
            patch("bsm_supervisor.setup.os.geteuid", return_value=0),
            patch("bsm_supervisor.setup.sys.stdin.isatty", return_value=False),
            patch("bsm_supervisor.setup.install") as install,
        ):
            result = setup(
                self.config, self.store, None, None, "bsm", True, start=False
            )
            repeated = setup(
                self.config, self.store, None, None, "bsm", True, start=False
            )
        self.assertEqual(result["installation"], repeated["installation"])
        self.assertEqual(selection(self.config, self.store)["path"], str(path))
        self.assertTrue((self.units / self.config.service_unit).is_file())
        self.assertTrue((self.config.state_dir / "bsm-runtime.env").is_file())
        install.assert_not_called()

    def test_setup_refuses_unselected_existing_data_and_pending_recovery(self):
        self.config.config_dir.mkdir(parents=True)
        (self.config.config_dir / "bedrock_server_manager.json").write_text("{}")
        with patch(
            "bsm_supervisor.service.SystemdService.status", return_value=self.stopped()
        ):
            with self.assertRaisesRegex(ValueError, "Existing BSM data"):
                setup(self.config, self.store, None, None, "bsm", True)
        self.store.begin("interrupted")
        with self.assertRaises(BusyError):
            setup(self.config, self.store, None, None, "bsm", True)

    def test_running_setup_keeps_selection_and_does_not_restart(self):
        self.installation()
        (self.config.installations_dir / "current").symlink_to("release-test")
        with (
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value={"ActiveState": "active"},
            ),
            patch("bsm_supervisor.setup.require_managed"),
            patch("bsm_supervisor.setup.refresh") as refresh,
            patch("bsm_supervisor.setup.operations.control") as control,
        ):
            result = setup(self.config, self.store, None, None, "bsm", False)
        self.assertTrue(result["running"])
        refresh.assert_not_called()
        control.assert_not_called()

    def test_start_wait_reports_readiness_failure_with_log_command(self):
        self.installation()
        (self.config.installations_dir / "current").symlink_to("release-test")
        with (
            patch("bsm_supervisor.operations.require_managed"),
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value=self.stopped(),
            ),
            patch("bsm_supervisor.service.SystemdService.control"),
            patch(
                "bsm_supervisor.operations.runtime.wait_ready",
                side_effect=runtime.RuntimeErrorBase("not healthy"),
            ) as wait,
        ):
            with self.assertRaisesRegex(ServiceError, "journalctl"):
                operations.control(self.config, self.store, "start", wait=True)
        wait.assert_called_once_with(self.config, "4.0.0b7")
        self.assertEqual(self.store.operations()[0]["state"], "running")

    def test_doctor_does_not_initialize_missing_state(self):
        config = replace(self.config, state_dir=self.root / "absent")
        with patch(
            "bsm_supervisor.service.SystemdService.status",
            side_effect=ServiceError("missing"),
        ):
            report = diagnostics.doctor(config)
        self.assertFalse(report["ok"])
        self.assertFalse(config.state_dir.exists())

    def test_doctor_retains_pending_operations_and_redacts_errors(self):
        config = replace(self.config, bsm_db_url="postgresql://user:secret@host/db")
        self.store.begin("interrupted")
        before = self.store.path.stat().st_mtime_ns
        with patch(
            "bsm_supervisor.service.SystemdService.status",
            side_effect=ServiceError("secret"),
        ):
            report = diagnostics.doctor(config)
        self.assertFalse(report["ok"])
        self.assertNotIn("secret", json.dumps(report))
        self.assertEqual(self.store.path.stat().st_mtime_ns, before)
        self.assertEqual(self.store.operations()[0]["state"], "running")

    def test_listener_urls_use_container_addresses_and_bracket_ipv6(self):
        response = subprocess.CompletedProcess(
            [],
            0,
            '[{"addr_info":[{"local":"192.0.2.20"},'
            '{"local":"127.0.0.1"},{"local":"::1"}]}]',
        )
        with patch("bsm_supervisor.diagnostics.subprocess.run", return_value=response):
            self.assertEqual(
                diagnostics.urls(replace(self.config, bsm_host="0.0.0.0")),
                ["http://192.0.2.20:11325"],
            )
        self.assertEqual(
            diagnostics.urls(replace(self.config, bsm_host="::1")),
            ["http://[::1]:11325"],
        )

    def update_fixture(self, running=True):
        prefix = self.root / "supervisor"
        previous = prefix / "versions/old"
        target = prefix / "versions/new"
        previous.mkdir(parents=True)
        (target / "bin").mkdir(parents=True)
        (target / "bin/bsm-supervisor").touch()
        (target / "bin/bsm-supervisor").chmod(0o755)
        (prefix / "current").symlink_to(previous)
        state = self.stopped()
        if running:
            state.update(ActiveState="active", MainPID="123")
        commands = []

        def command(arguments, **kwargs):
            commands.append((arguments, kwargs))
            if arguments[0] == "/bin/bash":
                (prefix / "current").unlink()
                (prefix / "current").symlink_to(target)
                requested = arguments[arguments.index("--ref") + 1]
                write_json(
                    target / "supervisor-installation.json", {"revision": requested}
                )
            else:
                with Store(self.config.state_dir).lock():
                    pass
            return subprocess.CompletedProcess(arguments, 0)

        return prefix, target, state, commands, command

    def test_supervisor_update_pins_source_keeps_token_out_of_args_and_restarts(self):
        prefix, target, state, commands, command = self.update_fixture()
        sha = "a" * 40
        with (
            patch("bsm_supervisor.self_update.PREFIX", prefix),
            patch(
                "bsm_supervisor.self_update.fetch",
                side_effect=[json.dumps([{"sha": sha}]).encode(), b"installer"],
            ),
            patch("bsm_supervisor.self_update.execute", side_effect=command),
            patch("bsm_supervisor.service.SystemdService.status", return_value=state),
            patch("bsm_supervisor.self_update.runtime.shutdown") as shutdown,
            patch.dict(os.environ, {"GH_TOKEN": "private_token"}),
        ):
            result = self_update.update(
                self.config, self.config_file, self.store, "dev", wait=True
            )
        self.assertEqual(result["phase"], "completed")
        self.assertEqual(result["revision"], sha)
        self.assertEqual(result["environment"], str(target))
        self.assertEqual(commands[-1][0][-2:], ["start", "--wait"])
        self.assertIn(sha, commands[0][0])
        self.assertNotIn("private_token", json.dumps([item[0] for item in commands]))
        self.assertNotIn("private_token", json.dumps(result))
        shutdown.assert_called_once()

    def test_supervisor_update_preserves_stopped_state(self):
        prefix, _, state, commands, command = self.update_fixture(running=False)
        source = self.root / "checkout"
        (source / "scripts").mkdir(parents=True)
        (source / "scripts/install-lxc.sh").write_text("installer")
        with (
            patch("bsm_supervisor.self_update.PREFIX", prefix),
            patch("bsm_supervisor.self_update.execute", side_effect=command),
            patch("bsm_supervisor.service.SystemdService.status", return_value=state),
            patch("bsm_supervisor.self_update.runtime.shutdown") as shutdown,
        ):
            self_update.update(self.config, self.config_file, self.store, "dev", source)
        shutdown.assert_not_called()
        self.assertEqual(len(commands), 2)
        self.assertIn("unit-refresh", commands[1][0])

    def test_interrupted_unit_refresh_resumes_without_downloading_again(self):
        prefix, target, _, commands, command = self.update_fixture(running=False)
        write_json(
            self.config.state_dir / "supervisor-update.json",
            {
                "phase": "installed",
                "config": str(self.config_file.resolve()),
                "scope": [
                    str(self.config.data_dir),
                    str(self.config.config_dir),
                    str(self.config.installations_dir),
                    self.config.service_unit,
                ],
                "environment": str(target),
                "was_running": False,
                "user": "bsm",
            },
        )
        with (
            patch("bsm_supervisor.self_update.PREFIX", prefix),
            patch("bsm_supervisor.self_update.fetch") as fetch,
            patch("bsm_supervisor.self_update.execute", side_effect=command),
        ):
            self_update.update(
                self.config, self.config_file, self.store, "dev", resume=True
            )
        fetch.assert_not_called()
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            read_json(self.config.state_dir / "supervisor-update.json")["phase"],
            "completed",
        )

    def test_failed_installer_keeps_bsm_running_and_resume_reuses_pinned_revision(self):
        prefix, _, state, commands, command = self.update_fixture()
        sha = "b" * 40
        calls = []

        def failing(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 9)

        def fetch(path, token, limit):
            calls.append(path)
            return (
                json.dumps([{"sha": sha}]).encode()
                if path.startswith("/commits")
                else b"installer"
            )

        with (
            patch("bsm_supervisor.self_update.PREFIX", prefix),
            patch("bsm_supervisor.self_update.fetch", side_effect=fetch),
            patch("bsm_supervisor.service.SystemdService.status", return_value=state),
            patch("bsm_supervisor.self_update.runtime.shutdown") as shutdown,
            patch("bsm_supervisor.self_update.execute", side_effect=failing),
        ):
            with self.assertRaisesRegex(self_update.UpdateError, "installer failed"):
                self_update.update(self.config, self.config_file, self.store, "dev")
            shutdown.assert_not_called()
        self.assertEqual(
            read_json(self.config.state_dir / "supervisor-update.json")["revision"], sha
        )
        with (
            patch("bsm_supervisor.self_update.PREFIX", prefix),
            patch("bsm_supervisor.self_update.fetch", side_effect=fetch),
            patch("bsm_supervisor.service.SystemdService.status", return_value=state),
            patch("bsm_supervisor.self_update.runtime.shutdown"),
            patch("bsm_supervisor.self_update.execute", side_effect=command),
        ):
            result = self_update.update(
                self.config, self.config_file, self.store, "dev", resume=True
            )
        self.assertEqual(result["phase"], "completed")
        self.assertIn("sha=" + sha, calls[2])
        self.assertIn("unit-refresh", commands[1][0])

    def test_waited_start_success_is_journaled_only_after_readiness(self):
        self.installation()
        (self.config.installations_dir / "current").symlink_to("release-test")

        def ready(config, version):
            self.assertEqual(self.store.operations()[0]["state"], "running")
            return {"ready": True}

        with (
            patch("bsm_supervisor.operations.require_managed"),
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value=self.stopped(),
            ),
            patch("bsm_supervisor.service.SystemdService.control"),
            patch("bsm_supervisor.runtime.wait_ready", side_effect=ready),
        ):
            operations.control(self.config, self.store, "start", wait=True)
        self.assertEqual(self.store.operations()[0]["state"], "succeeded")

    def test_doctor_access_checks_parent_traversal(self):
        path = self.root / "service"
        path.mkdir(mode=0o700)
        (path / "data").mkdir(mode=0o777)
        self.assertFalse(diagnostics.accessible(path / "data", os.geteuid() + 1, [], 1))

    def test_update_lock_excludes_concurrent_controllers(self):
        with self_update.update_lock(self.config):
            with self.assertRaises(BusyError):
                with self_update.update_lock(self.config):
                    self.fail("concurrent update entered")

    def test_invalid_update_ref_does_not_download_or_write_progress(self):
        with patch("bsm_supervisor.self_update.fetch") as fetch:
            with self.assertRaisesRegex(self_update.UpdateError, "reference"):
                self_update.update(
                    self.config, self.config_file, self.store, "../other"
                )
        fetch.assert_not_called()
        self.assertFalse((self.config.state_dir / "supervisor-update.json").exists())

    def test_setup_selects_compatible_release_on_a_fresh_deployment(self):
        from bsm_supervisor.releases import Release

        account = SimpleNamespace(pw_uid=self.operator_uid, pw_gid=os.getegid())

        def state():
            if (self.units / self.config.service_unit).exists():
                return self.stopped()
            raise ServiceError(f"Service is not installed: {self.config.service_unit}")

        def prepare(config, store, release):
            self.installation()
            return {"id": "release-test"}

        with (
            patch("bsm_supervisor.setup.pwd.getpwnam", return_value=account),
            patch("bsm_supervisor.setup.discover", return_value=[Release("4.0.0b7")]),
            patch("bsm_supervisor.setup.install", side_effect=prepare) as install,
            patch("bsm_supervisor.service.SystemdService.status", side_effect=state),
            patch("bsm_supervisor.service.SystemdService.reload_units"),
            patch("bsm_supervisor.unit_refresh.UNIT_DIRECTORY", self.units),
            patch(
                "bsm_supervisor.unit_refresh.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0),
            ),
        ):
            result = setup(
                self.config, self.store, "prerelease", None, "bsm", True, start=False
            )
        install.assert_called_once()
        self.assertEqual(result["installation"], "release-test")

    def test_unit_creation_refuses_a_loaded_vendor_service(self):
        from bsm_supervisor.unit_refresh import refresh

        with (
            patch("bsm_supervisor.unit_refresh.UNIT_DIRECTORY", self.units),
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value=self.stopped(),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "existing managed unit"):
                refresh(self.config, self.store, "bsm", create=True)
        self.assertFalse((self.units / self.config.service_unit).exists())

    def test_timeout_terminates_the_whole_updater_process_group(self):
        import signal

        with (
            patch("bsm_supervisor.self_update.subprocess.Popen") as popen,
            patch("bsm_supervisor.self_update.os.killpg") as kill,
        ):
            process = popen.return_value.__enter__.return_value
            process.pid = 1234
            process.wait.side_effect = [subprocess.TimeoutExpired("installer", 1), -9]
            with self.assertRaises(subprocess.TimeoutExpired):
                self_update.execute(["/bin/bash", "installer"], timeout=1)
        kill.assert_called_once_with(1234, signal.SIGKILL)
