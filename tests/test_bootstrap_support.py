"""Verify shared standalone support and private persistent stage logging."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class BootstrapSupportTests(unittest.TestCase):
    def test_standalone_support_is_generated_from_shared_source(self):
        path = Path("scripts/sync-bootstrap.py")
        spec = importlib.util.spec_from_file_location("bootstrap_sync", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.sync(path.parent, check=True)

    def test_stage_log_is_private_and_does_not_echo_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -Eeuo pipefail; source scripts/lib/bootstrap.sh; "
                    "bsm_log_begin test; trap 'bsm_finish $?' EXIT; "
                    "bsm_stage Publish; secret=not-logged; false",
                ],
                env={**os.environ, "BSM_SUPERVISOR_LOG_DIR": temporary},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            logs = list(Path(temporary).glob("*.log"))
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
            self.assertIn("Publish", logs[0].read_text())
            self.assertIn("Result: 1", logs[0].read_text())
            self.assertNotIn("not-logged", logs[0].read_text())
