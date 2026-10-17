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
        self.assertIn("PassEnvironment=BSM_LOG_LEVEL", unit)
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
            {"BSM_SUPERVISOR_WEB_HOST": "0.0.0.0", "BSM_SUPERVISOR_WEB_PORT": "23456"},
        )

    def test_non_lxc_never_reads_init_environment(self):
        with (
            patch.object(Path, "read_text", return_value="docker\n"),
            patch.object(Path, "read_bytes") as read,
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(overrides(), {})
            read.assert_not_called()
