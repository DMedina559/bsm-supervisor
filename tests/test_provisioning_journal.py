"""Exercise the standalone journal against cached source and guest tampering."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_proxmox


class ProvisioningProfileTests(test_proxmox.ProxmoxPlanTests):
    def test_profile_defaults_and_command_line_precedence(self):
        profile = self.base / "profile.json"
        profile.write_text(json.dumps({"hostname": "profile-guest", "memory": 4096}))
        result = self.run_plan("--profile", str(profile), "--memory", "3072")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile-guest", result.stdout)
        self.assertIn("--memory 3072", result.stdout)
        self.assertNotIn("--memory 4096", result.stdout)

    def test_profile_rejects_guest_identity_static_address_and_secrets(self):
        profile = self.base / "profile.json"
        for key in ("ctid", "address", "GH_TOKEN", "BSM_DB_URL"):
            profile.write_text(json.dumps({key: "private"}))
            result = self.run_plan("--profile", str(profile))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("private", result.stderr)


class ProvisioningJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "settings.json").write_text('{"hostname":"example"}')
        (self.work / "environment.json").write_text('{"HOST":"0.0.0.0"}')
        (self.work / "bootstrap.tar.gz").write_bytes(b"pinned source bundle")
        script = Path("scripts/provision-proxmox.sh").read_text()
        self.program = script.split("<<'PYJOURNAL'\n", 1)[1].split("\nPYJOURNAL", 1)[0]
        self.journal = self.root / "state/101/plan.json"
        result = self.run_record("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.identity = result.stdout.strip()
        self.guest = self.work / "guest-config"
        self.guest.write_text(
            "description: BSM Supervisor deployment "
            + self.identity
            + "\nmemory: 2048\n"
        )

    def run_record(self, action):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                self.program,
                action,
                str(self.root / "state"),
                "101",
                str(self.work),
                "dev",
                "",
            ],
            capture_output=True,
            text=True,
        )

    def test_prepare_is_private_and_never_overwrites_a_deployment(self):
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.run_record("prepare").returncode, 0)
        self.assertEqual(json.loads(self.journal.read_text())["id"], self.identity)

    def test_owned_guest_verified_and_configuration_changes_refused(self):
        self.assertEqual(self.run_record("created").returncode, 0)
        self.assertEqual(self.run_record("verify").returncode, 0)
        self.guest.write_text(self.guest.read_text().replace("2048", "4096"))
        self.assertNotEqual(self.run_record("verify").returncode, 0)

    def test_cache_environment_and_ownership_tampering_refused(self):
        self.assertEqual(self.run_record("created").returncode, 0)
        cache = self.journal.parent / "bootstrap.tar.gz"
        cache.write_bytes(b"changed source")
        self.assertNotEqual(self.run_record("verify").returncode, 0)
        cache.write_bytes(b"pinned source bundle")
        (self.work / "environment.json").write_text('{"HOST":"127.0.0.1"}')
        self.assertNotEqual(self.run_record("verify").returncode, 0)
        (self.work / "environment.json").write_text('{"HOST":"0.0.0.0"}')
        self.guest.write_text("memory: 2048\n")
        self.assertNotEqual(self.run_record("verify").returncode, 0)

    def test_only_never_created_guest_can_be_created_on_resume(self):
        self.assertEqual(self.run_record("verify-empty").returncode, 0)
        self.assertEqual(self.run_record("created").returncode, 0)
        self.assertNotEqual(self.run_record("verify-empty").returncode, 0)
