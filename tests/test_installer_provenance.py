"""Verify full source identity independently of GitHub archive abbreviations."""

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_administration as fixtures

from bsm_supervisor import self_update
from bsm_supervisor.checkpoint_files import write_json


class InstallerProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = (
            Path("scripts/install-lxc.sh").read_text()
            + Path("scripts/lib/install-systemd.sh").read_text()
        )
        self.revision = "5d63feb6866a4831779474e88fe869a63d7ce7ab"

    def program(self, marker):
        return self.script.split("<<'" + marker + "'\n", 1)[1].split(
            "\n" + marker + "\n", 1
        )[0]

    def execute(self, marker, *arguments):
        return subprocess.run(
            [sys.executable, "-I", "-c", self.program(marker), *map(str, arguments)],
            capture_output=True,
            text=True,
        )

    def archive(self, prefix):
        name = "DMedina559-bsm-supervisor-" + prefix
        with tarfile.open(self.root / "source.tar.gz", "w:gz") as archive:
            payload = b"[project]\nname='bsm-supervisor'\n"
            entry = tarfile.TarInfo(name + "/pyproject.toml")
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        return name

    def test_short_archive_name_records_full_commit_in_installed_environment(self):
        name = self.archive(self.revision[:7])
        result = self.execute("PYSOURCE", self.root, self.revision, "dev")
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.root / "source" / name
        record = json.loads((source / "supervisor-source.json").read_text())
        self.assertEqual(
            record,
            {
                "source": "github",
                "ref": "dev",
                "revision": self.revision,
                "dirty": None,
            },
        )
        target = self.root / "environment"
        target.mkdir()
        installed = self.execute("PYREVISION", source, target, "dev")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(
            json.loads((target / "supervisor-installation.json").read_text()), record
        )

    def test_proxmox_bootstrap_preserves_short_archive_provenance(self):
        self.script = Path("scripts/provision-proxmox.sh").read_text()
        name = self.archive(self.revision[:7])
        result = self.execute("PYSOURCE", self.root, self.revision, "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.root / "source" / name
        output = self.root / "supervisor-source.json"
        result = self.execute("PYPROVENANCE", source, output, "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(output.read_text())
        self.assertEqual(record["revision"], self.revision)
        self.assertEqual(record["source"], "github")
        self.assertEqual(record["ref"], "v0.1.0")

    def test_full_archive_name_remains_supported(self):
        name = self.archive(self.revision)
        result = self.execute("PYSOURCE", self.root, self.revision, "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(
                (self.root / "source" / name / "supervisor-source.json").read_text()
            )["revision"],
            self.revision,
        )

    def test_mismatched_archive_prefix_is_rejected_before_provenance_is_written(self):
        self.archive("a" * 7)
        result = self.execute("PYSOURCE", self.root, self.revision, "dev")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from the resolved commit", result.stderr)
        self.assertEqual(list(self.root.rglob("supervisor-source.json")), [])

    def test_commit_resolution_requires_full_sha_and_honors_exact_requested_commit(
        self,
    ):
        path = self.root / "commit.json"
        path.write_text(json.dumps({"sha": self.revision}))
        result = self.execute("PYCOMMIT", path, self.revision)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, self.revision))
        result = self.execute("PYCOMMIT", path, "a" * 40)
        self.assertNotEqual(result.returncode, 0)
        path.write_text(json.dumps({"sha": self.revision[:7]}))
        self.assertNotEqual(self.execute("PYCOMMIT", path, "dev").returncode, 0)


class ResumeTargetTests(unittest.TestCase):
    setUp = fixtures.AdministrationTests.setUp
    stopped = fixtures.AdministrationTests.stopped
    update_fixture = fixtures.AdministrationTests.update_fixture

    def test_resume_keeps_saved_commit_even_when_another_ref_is_supplied(self):
        prefix, _, state, commands, command = self.update_fixture(running=False)
        revision = "b" * 40
        write_json(
            self.config.state_dir / "supervisor-update.json",
            {
                "phase": "preparing",
                "config": str(self.config_file.resolve()),
                "scope": [
                    str(self.config.data_dir),
                    str(self.config.config_dir),
                    str(self.config.installations_dir),
                    self.config.service_unit,
                ],
                "was_running": False,
                "user": "bsm",
                "ref": "dev",
                "revision": revision,
                "source_dir": None,
            },
        )
        with (
            patch("bsm_supervisor.self_update.PREFIX", prefix),
            patch(
                "bsm_supervisor.self_update.fetch",
                side_effect=[json.dumps([{"sha": revision}]).encode(), b"installer"],
            ) as fetch,
            patch("bsm_supervisor.self_update.execute", side_effect=command),
            patch("bsm_supervisor.service.SystemdService.status", return_value=state),
        ):
            result = self_update.update(
                self.config, self.config_file, self.store, "v0.1.0", resume=True
            )
        self.assertEqual(result["revision"], revision)
        self.assertEqual(result["ref"], "dev")
        self.assertIn(revision, fetch.call_args_list[0].args[0])
        self.assertIn(revision, commands[0][0])
        self.assertNotIn("--package-version", commands[0][0])
