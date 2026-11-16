"""Verify shared installation dispatch and the default service adapter boundary."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_administration as fixtures

from bsm_supervisor import operations
from bsm_supervisor.backends.systemd import SystemdService
from bsm_supervisor.service import service_backend
from bsm_supervisor.service_environment import write_environment


class BackendBoundaryTests(unittest.TestCase):
    setUp = fixtures.AdministrationTests.setUp
    stopped = fixtures.AdministrationTests.stopped

    def test_default_adapter_is_systemd_without_running_a_command(self):
        with patch("bsm_supervisor.backends.systemd.subprocess.run") as run:
            service = service_backend(self.config)
            self.assertIsInstance(service, SystemdService)
            self.assertEqual(service.unit, self.config.service_unit)
            run.assert_not_called()

    def test_workflow_controls_only_the_selected_adapter(self):
        self.config.config_dir.mkdir(parents=True)
        write_environment(self.config)
        adapter = Mock()
        adapter.status.side_effect = [
            self.stopped(),
            {"ActiveState": "active", "MainPID": "22"},
        ]
        with (
            patch(
                "bsm_supervisor.operations.selection", return_value={"version": "4.0.0"}
            ),
            patch(
                "bsm_supervisor.operations.service_backend", return_value=adapter
            ) as factory,
            patch("bsm_supervisor.backends.systemd.subprocess.run") as systemctl,
        ):
            operations.control(self.config, self.store, "start")
        factory.assert_called_once_with(self.config)
        adapter.control.assert_called_once_with("start")
        systemctl.assert_not_called()
        self.assertEqual(self.store.operations()[0]["state"], "succeeded")


class InstallerBoundaryTests(unittest.TestCase):
    def test_generic_entry_preserves_arguments_and_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "install.sh").write_bytes(Path("scripts/install.sh").read_bytes())
            (root / "install-lxc.sh").write_text(
                'printf "%s\\n" "$BSM_INSTALL_ENTRY" "$@"; exit 7\n'
            )
            result = subprocess.run(
                [
                    "bash",
                    str(root / "install.sh"),
                    "--config",
                    "/tmp/config with spaces.toml",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 7)
            self.assertEqual(
                result.stdout.splitlines(),
                ["install.sh", "--config", "/tmp/config with spaces.toml"],
            )

    def test_bootstrap_loads_engine_from_target_source_bundle(self):
        script = Path("scripts/install-lxc.sh").read_text()
        dispatch = script[
            script.index('engine="$source_dir/scripts/lib/install-systemd.sh"') :
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source with spaces"
            (source / "scripts/lib").mkdir(parents=True)
            record = root / "arguments"
            (source / "scripts/lib/install-systemd.sh").write_text(
                'bsm_install_systemd() { printf "%s\\n" "$@" > "$ENGINE_ARGS"; }\n'
            )
            program = (
                "set -euo pipefail; source_dir=$1; work=$2; ref=dev; package_version=; "
                "config=/etc/bsm-supervisor/config.toml; install_unit=false;\n"
                + dispatch
            )
            result = subprocess.run(
                ["bash", "-c", program, "test", str(source), str(root)],
                capture_output=True,
                text=True,
                env={**os.environ, "ENGINE_ARGS": str(record)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                record.read_text().splitlines(),
                [
                    str(source),
                    str(root),
                    "dev",
                    "",
                    "/etc/bsm-supervisor/config.toml",
                    "false",
                ],
            )

    def test_incomplete_modular_bundle_is_not_recursively_reinstalled(self):
        script = Path("scripts/install-lxc.sh").read_text()
        dispatch = script[
            script.index('engine="$source_dir/scripts/lib/install-systemd.sh"') :
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "scripts").mkdir()
            (source / "scripts/install-lxc.sh").write_text(script)
            program = "set -euo pipefail; source_dir=$1;\n" + dispatch
            result = subprocess.run(
                ["bash", "-c", program, "test", str(source)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Incomplete source bundle", result.stderr)

    def test_legacy_release_dispatch_preserves_flags_and_cleans_up_normally(self):
        script = Path("scripts/install-lxc.sh").read_text()
        dispatch = script[
            script.index('engine="$source_dir/scripts/lib/install-systemd.sh"') :
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "scripts").mkdir()
            record = source / "arguments"
            (source / "scripts/install-lxc.sh").write_text(
                'printf "%s\\n" "$@" > "$ENGINE_ARGS"\n'
            )
            program = (
                "set -euo pipefail; source_dir=$1; ref=v0.1.0; package_version=0.1.0; "
                "config=/etc/bsm-supervisor/config.toml; install_unit=true; "
                'exec 9>"$source_dir/lock"; flock 9;\n' + dispatch
            )
            result = subprocess.run(
                ["bash", "-c", program, "test", str(source)],
                capture_output=True,
                text=True,
                env={**os.environ, "ENGINE_ARGS": str(record)},
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                record.read_text().splitlines(),
                [
                    "--source-dir",
                    str(source),
                    "--ref",
                    "v0.1.0",
                    "--package-version",
                    "0.1.0",
                    "--config",
                    "/etc/bsm-supervisor/config.toml",
                    "--install-bsm-unit",
                ],
            )
