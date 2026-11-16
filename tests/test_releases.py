"""Exercise release selection, staging failures, and offline activation."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor.activation import activate, selection, service_unit
from bsm_supervisor.config import Config
from bsm_supervisor.install import install, run_command
from bsm_supervisor.releases import (
    Release,
    ReleaseError,
    compatible_releases,
    discover,
    select,
)
from bsm_supervisor.service import guard_command, managed_command
from bsm_supervisor.service_environment import write_environment
from bsm_supervisor.store import Store


class ReleaseTests(unittest.TestCase):
    def test_pep440_order_channels_yanks_and_python_requirements(self):
        def wheel(**kwargs):
            return {
                "packagetype": "bdist_wheel",
                "filename": "bedrock_server_manager-1.0-py3-none-any.whl",
                **kwargs,
            }

        metadata = {
            "releases": {
                "3.9.0": [wheel()],
                "3.10.0": [wheel()],
                "4.0.0b9": [wheel()],
                "4.0.0b10": [wheel()],
                "8.0.0": [wheel(yanked=True)],
                "9.0.0": [wheel(requires_python=">=99")],
                "10.0.0": [{"packagetype": "sdist"}],
                "11.0.0": [wheel(requires_python="broken")],
                "12.0.0": [
                    wheel(filename="bedrock_server_manager-12.0-cp27-cp27m-win32.whl")
                ],
                "invalid": [wheel()],
            }
        }
        releases = compatible_releases(metadata)
        self.assertEqual(select(releases, "stable").version, "3.10.0")
        self.assertEqual(select(releases, "prerelease").version, "4.0.0b10")
        self.assertEqual(select(releases, "v3.9.0").version, "3.9.0")
        for selector in ("8.0.0", "https://evil/package.whl", "--extra-index-url"):
            with self.assertRaises(ReleaseError):
                select(releases, selector)

    @patch("bsm_supervisor.releases.urlopen")
    def test_metadata_timeout_redirect_and_corruption(self, urlopen):
        urlopen.side_effect = OSError("offline")
        with self.assertRaises(ReleaseError):
            discover()
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 20)
        urlopen.side_effect = None
        response = urlopen.return_value.__enter__.return_value
        response.url = "https://example.com/metadata"
        with self.assertRaisesRegex(ReleaseError, "redirect"):
            discover()
        response.url = "https://pypi.org/pypi/bedrock-server-manager/json"
        response.read.return_value = b"not json"
        with self.assertRaises(ReleaseError):
            discover()


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(root / "state", root / "envs", root / "data")
        self.config.config_dir.mkdir(parents=True)
        self.store = Store(self.config.state_dir)
        write_environment(self.config)

    def fake_command(self, arguments, log):
        target = self.config.installations_dir / "release-4.0.0b7"
        (target / "bin").mkdir(exist_ok=True)
        (target / "bin/python").touch()
        (target / "bin/bedrock-server-manager").touch()
        (target / "pyvenv.cfg").touch()

    @patch("bsm_supervisor.install.run_command")
    def test_success_registers_verified_environment_without_moving_or_starting(
        self, run
    ):
        run.side_effect = self.fake_command
        result = install(self.config, self.store, Release("4.0.0b7"))
        self.assertEqual(result["source"], "pypi")
        installed = self.store.installations()[0]
        self.assertEqual(installed["version"], "4.0.0b7")
        target = Path(installed["path"])
        self.assertTrue((target / "supervisor-installation.json").is_file())
        self.assertEqual(self.store.operations()[0]["state"], "succeeded")
        self.assertFalse((self.config.installations_dir / "current").exists())
        pip_command = run.call_args_list[1].args[0]
        self.assertIn("bedrock-server-manager==4.0.0b7", pip_command)
        self.assertIn("--only-binary=:all:", pip_command)
        self.assertEqual(
            pip_command[pip_command.index("--index-url") + 1], "https://pypi.org/simple"
        )
        run.reset_mock()
        run.side_effect = None
        result = install(self.config, self.store, Release("4.0.0b7"))
        self.assertTrue(result["reused"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(self.store.installations()), 1)

    @patch("bsm_supervisor.install.run_command")
    def test_failure_never_registers_or_changes_selection(self, run):
        root = self.config.installations_dir
        root.mkdir()
        (root / "current").symlink_to("previous", target_is_directory=True)
        run.side_effect = ReleaseError("download failed")
        with self.assertRaises(ReleaseError):
            install(self.config, self.store, Release("4.0.0b7"))
        self.assertEqual(self.store.installations(), [])
        self.assertEqual((root / "current").readlink(), Path("previous"))
        self.assertEqual(self.store.operations()[0]["state"], "failed")
        self.assertTrue((root / "release-4.0.0b7").is_dir())

    @patch("bsm_supervisor.install.run_command")
    def test_reuse_refuses_changed_metadata_and_unregistered_directories(self, run):
        run.side_effect = self.fake_command
        install(self.config, self.store, Release("4.0.0b7"))
        target = self.config.installations_dir / "release-4.0.0b7"
        run.reset_mock()
        for content in ("{}", "[]"):
            (target / "supervisor-installation.json").write_text(content)
            with self.assertRaisesRegex(ReleaseError, "metadata"):
                install(self.config, self.store, Release("4.0.0b7"))
        run.assert_not_called()
        other = self.config.installations_dir / "release-9.0.0"
        other.mkdir()
        with self.assertRaisesRegex(ReleaseError, "unregistered"):
            install(self.config, self.store, Release("9.0.0"))
        self.assertTrue(other.is_dir())

    @patch("bsm_supervisor.install.subprocess.Popen")
    @patch("bsm_supervisor.install.os.killpg")
    def test_timeout_kills_command_group(self, killpg, popen):
        process = popen.return_value.__enter__.return_value
        process.pid = 4321
        process.wait.side_effect = [subprocess.TimeoutExpired("pip", 1), -9]
        with self.assertRaisesRegex(ReleaseError, "timed out"):
            run_command(
                ["/python", "-m", "pip"], self.config.state_dir / "log", timeout=1
            )
        killpg.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def registered(self):
        target = self.config.installations_dir / "release"
        (target / "bin").mkdir(parents=True)
        for name in ("bin/python", "bin/bedrock-server-manager", "pyvenv.cfg"):
            (target / name).touch()
        self.store.register("release", "4.0.0b7", target, self.config.installations_dir)
        return target

    def stopped(self):
        return {
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

    @patch("bsm_supervisor.integration.service_backend")
    def test_offline_activation_is_atomic_and_does_not_start_service(self, service):
        target = self.registered()
        self.assertIsNone(selection(self.config, self.store))
        service.return_value.status.return_value = self.stopped()
        activate(self.config, self.store, "release")
        current = self.config.installations_dir / "current"
        self.assertEqual(current.resolve(), target)
        self.assertEqual(selection(self.config, self.store)["id"], "release")
        service.return_value.control.assert_not_called()
        self.assertEqual(self.store.operations()[0]["state"], "succeeded")

    @patch("bsm_supervisor.integration.service_backend")
    def test_activation_refuses_running_queued_or_unmanaged_service(self, service):
        self.registered()
        for overrides in (
            {"ActiveState": "active"},
            {"MainPID": "123"},
            {"Job": "5/start"},
            {"ExecStart": "/usr/bin/python"},
            {"Job": None},
        ):
            status = {**self.stopped(), **overrides}
            service.return_value.status.return_value = status
            with self.assertRaises(ValueError):
                activate(self.config, self.store, "release")
            self.assertFalse((self.config.installations_dir / "current").exists())
            self.assertFalse(
                any(item["state"] == "running" for item in self.store.operations())
            )

    def test_render_unit_with_bounded_restart_and_explicit_paths(self):
        unit = service_unit(self.config, "bsm")
        self.assertIn("StartLimitBurst=3", unit)
        self.assertIn("TimeoutStopSec=180", unit)
        self.assertIn("--mode direct", unit)
        self.assertIn("--config-dir", unit)
        self.assertNotIn("sudo", unit)
        with self.assertRaises(ValueError):
            service_unit(self.config, "root\nExecStart=/bad")


if __name__ == "__main__":
    unittest.main()
