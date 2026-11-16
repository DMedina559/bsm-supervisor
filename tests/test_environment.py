"""Bind configuration is validated consistently across TOML, LXC and systemd."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor.activation import service_unit
from bsm_supervisor.config import Config
from bsm_supervisor.environment import overrides
from bsm_supervisor.service import managed_command


class BindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        self.path.write_text(
            f'state_dir = "{self.root}/state"\n'
            f'installations_dir = "{self.root}/envs"\n'
            f'data_dir = "{self.root}/data"\n'
            'bsm_host = "0.0.0.0"\nbsm_port = 11325\n'
        )

    def test_unit_and_validation_command_share_host_and_port(self):
        with patch("bsm_supervisor.config.overrides", return_value={}):
            config = Config.load(self.path)
        command = managed_command(config)
        self.assertIn("--host 0.0.0.0 --port 11325", command)
        unit = service_unit(config, "bsm")
        self.assertIn("ExecStart=" + command, unit)
        self.assertIn(f"EnvironmentFile={config.state_dir}/bsm-runtime.env", unit)
        self.assertIn("UnsetEnvironment=BSM_DB_URL BSM_DATA_DIR BSM_CONFIG_DIR", unit)

    def test_validated_environment_overrides_toml(self):
        with patch(
            "bsm_supervisor.config.overrides",
            return_value={
                "BSM_SUPERVISOR_WEB_HOST": "::",
                "BSM_SUPERVISOR_WEB_PORT": "12345",
            },
        ):
            config = Config.load(self.path)
        self.assertEqual(config.bsm_host, "::")
        self.assertEqual(config.bsm_port, 12345)
        for environment in (
            {"BSM_SUPERVISOR_WEB_HOST": "0.0.0.0 --evil"},
            {"BSM_SUPERVISOR_WEB_PORT": "65536"},
            {"BSM_SUPERVISOR_WEB_PORT": "--evil"},
        ):
            with patch("bsm_supervisor.config.overrides", return_value=environment):
                with self.assertRaises(ValueError):
                    Config.load(self.path)

    def test_bind_types_and_ranges_fail_closed(self):
        for value in (True, "11325", 0, 65536):
            with self.assertRaises(ValueError):
                Config(
                    self.root / "s", self.root / "e", self.root / "d", bsm_port=value
                )
        for value in ([], True, "example.com", "192.168.1.2"):
            with self.assertRaises(ValueError):
                Config(
                    self.root / "s", self.root / "e", self.root / "d", bsm_host=value
                )

    def test_native_lxc_environment_is_whitelisted_and_shell_takes_precedence(self):
        native = (
            b"BSM_SUPERVISOR_WEB_HOST=0.0.0.0\0"
            b"BSM_SUPERVISOR_WEB_PORT=12345\0BSM_DB_URL=secret\0"
        )
        with (
            patch.object(Path, "read_text", return_value="lxc\n"),
            patch.object(Path, "read_bytes", return_value=native),
            patch.dict(os.environ, {"BSM_SUPERVISOR_WEB_PORT": "23456"}),
        ):
            values = overrides()
        self.assertEqual(
            values,
            {
                "BSM_SUPERVISOR_WEB_HOST": "0.0.0.0",
                "BSM_SUPERVISOR_WEB_PORT": "23456",
                "BSM_DB_URL": "secret",
            },
        )

    def test_non_lxc_never_reads_init_environment(self):
        with (
            patch.object(Path, "read_text", return_value="docker\n"),
            patch.object(Path, "read_bytes") as read,
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(overrides(), {})
            read.assert_not_called()

    def test_all_public_bootstrap_variables_align_paths_and_database_scope(self):
        from bsm_supervisor.integration import inspect
        from bsm_supervisor.service_environment import (
            verify_environment,
            write_environment,
        )

        environment = {
            "BSM_CONFIG_DIR": str(self.root / "configuration"),
            "BSM_DATA_DIR": str(self.root / "worlds"),
            "BSM_DB_URL": "sqlite:///" + str(self.root / "configuration/custom.db"),
            "BSM_LOG_LEVEL": "warn",
            "HOST": "127.0.0.1",
            "PORT": "12345",
        }
        with patch("bsm_supervisor.config.overrides", return_value=environment):
            config = Config.load(self.path)
        self.assertEqual(config.data_dir, self.root / "worlds")
        self.assertEqual(config.config_dir, self.root / "configuration")
        self.assertEqual(config.bsm_log_level, "WARNING")
        self.assertEqual(config.bsm_host, "127.0.0.1")
        self.assertEqual(config.bsm_port, 12345)
        self.assertEqual(
            inspect(config)["database_path"], str(config.config_dir / "custom.db")
        )
        write_environment(config)
        verify_environment(config)
        self.assertEqual(
            (config.state_dir / "bsm-runtime.env").stat().st_mode & 0o777, 0o600
        )
        self.assertNotIn(config.bsm_db_url, service_unit(config, "bsm"))

    def test_private_environment_drift_and_external_database_fail_closed(self):
        from dataclasses import replace

        from bsm_supervisor.integration import inspect
        from bsm_supervisor.service_environment import (
            verify_environment,
            write_environment,
        )

        config = Config(
            self.root / "state",
            self.root / "envs",
            self.root / "data",
            bsm_db_url="postgresql://user:secret@db/bsm",
            bsm_log_level="INFO",
        )
        write_environment(config)
        self.assertTrue(inspect(config)["blockers"])
        self.assertNotIn("secret", str(inspect(config)))
        self.assertNotIn("secret", service_unit(config, "bsm"))
        with self.assertRaisesRegex(ValueError, "regenerate"):
            verify_environment(replace(config, bsm_log_level="DEBUG"))
        path = config.state_dir / "bsm-runtime.env"
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permissions"):
            verify_environment(config)

    def test_internal_guard_is_never_imported(self):
        with (
            patch.object(Path, "read_text", return_value="lxc"),
            patch.object(
                Path, "read_bytes", return_value=b"BSM_PLUGIN_RECURSION_GUARD=1\0"
            ),
            patch.dict(os.environ, {"BSM_PLUGIN_RECURSION_GUARD": "1"}, clear=True),
        ):
            self.assertEqual(overrides(), {})

    def test_environment_render_command_requires_stopped_service(self):
        import contextlib
        import io

        from bsm_supervisor.cli import main
        from bsm_supervisor.service_environment import verify_environment

        with (
            patch("bsm_supervisor.config.overrides", return_value={}),
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value={"ActiveState": "active", "MainPID": "123", "Job": ""},
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(self.path),
                        "service-environment",
                        "--offline-confirmed",
                    ]
                ),
                1,
            )
        self.assertFalse((self.root / "state/bsm-runtime.env").exists())
        with (
            patch("bsm_supervisor.config.overrides", return_value={}),
            patch(
                "bsm_supervisor.service.SystemdService.status",
                return_value={"ActiveState": "inactive", "MainPID": "0", "Job": ""},
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(self.path),
                        "service-environment",
                        "--offline-confirmed",
                    ]
                ),
                0,
            )
            verify_environment(Config.load(self.path))

    def test_start_refuses_environment_drift_before_controlling_service(self):
        import contextlib
        import io

        from bsm_supervisor.cli import main
        from bsm_supervisor.store import Store

        with (
            patch("bsm_supervisor.config.overrides", return_value={}),
            patch("bsm_supervisor.service.SystemdService.status", return_value={}),
            patch(
                "bsm_supervisor.operations.require_managed",
                side_effect=ValueError("environment drift"),
            ),
            patch("bsm_supervisor.service.SystemdService.control") as control,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--config", str(self.path), "start"]), 1)
            control.assert_not_called()
        self.assertEqual(Store(self.root / "state").operations(), [])
