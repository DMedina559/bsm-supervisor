"""Immutable Git provenance and conservative cleanup failure boundaries."""

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from bsm_supervisor import cleanup
from bsm_supervisor.config import Config
from bsm_supervisor.git_sources import install_git, validate_ref
from bsm_supervisor.store import Store


class SourceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.config = Config(base / "state", base / "envs", base / "data")
        self.store = Store(self.config.state_dir)
        self.target = self.config.installations_dir / "old"
        self.target.mkdir(parents=True)
        (self.target / "supervisor-installation.json").write_text(
            json.dumps({"path": str(self.target), "source": "pypi"})
        )
        self.store.add_installation("old", "4.0.0", self.target)
        self.enterContext(
            patch("bsm_supervisor.cleanup.offline_guard", return_value=nullcontext())
        )

    def test_refs_reject_options_paths_and_ambiguous_commits(self):
        for kind, ref in (
            ("branch", "--evil"),
            ("tag", "../bad"),
            ("commit", "abc123"),
            ("branch", "x.lock"),
        ):
            with self.assertRaises(ValueError):
                validate_ref(kind, ref)
        self.assertEqual(
            validate_ref("branch", "feature/test"), "refs/heads/feature/test"
        )

    def test_git_resolves_commit_once_and_records_provenance(self):
        sha = "a" * 40

        def command(args, log):
            target = self.config.installations_dir / ("git-" + sha)
            (target / "bin").mkdir(exist_ok=True)
            (target / "bin/bedrock-server-manager").touch()
            if "-c" in args:
                (target / "verified-version.json").write_text('"4.1.0.dev2"')

        with patch("bsm_supervisor.git_sources.git", return_value=sha) as git:
            with patch("bsm_supervisor.git_sources.run_command", side_effect=command):
                manifest = install_git(self.config, self.store, "branch", "dev")
        self.assertEqual(manifest["commit"], sha)
        self.assertEqual(manifest["source"], "git")
        self.assertIn(
            ["checkout", "--detach", sha], [c.args[0] for c in git.call_args_list]
        )
        self.assertEqual(len(self.store.installations()), 2)

    def test_selected_and_checkpoint_versions_are_protected(self):
        with patch("bsm_supervisor.cleanup.selection", return_value={"id": "old"}):
            with self.assertRaisesRegex(ValueError, "selected"):
                cleanup.remove(self.config, self.store, "old")
        with patch(
            "bsm_supervisor.cleanup.list_checkpoints",
            return_value=[{"installation": {"id": "old"}}],
        ):
            with self.assertRaisesRegex(ValueError, "checkpoint"):
                cleanup.remove(self.config, self.store, "old")
        self.assertTrue(self.target.exists())

    def test_unowned_symlink_and_invalid_checkpoints_block_deletion(self):
        with patch(
            "bsm_supervisor.cleanup.list_checkpoints", return_value=[{"invalid": True}]
        ):
            with self.assertRaises(ValueError):
                cleanup.remove(self.config, self.store, "old")
        (self.target / "supervisor-installation.json").unlink()
        with self.assertRaises(OSError):
            cleanup.remove(self.config, self.store, "old")
        self.target.rmdir()
        self.target.symlink_to(self.config.data_dir)
        with self.assertRaises(ValueError):
            cleanup.remove(self.config, self.store, "old")

    def test_partial_deletion_can_be_retried_without_touching_data(self):
        self.config.data_dir.mkdir()
        marker = self.config.data_dir / "world"
        marker.write_text("keep")
        with patch(
            "bsm_supervisor.cleanup.shutil.rmtree", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                cleanup.remove(self.config, self.store, "old")
        self.assertFalse(self.target.exists())
        cleanup.remove(self.config, self.store, "old")
        self.assertEqual(self.store.installations(), [])
        self.assertEqual(marker.read_text(), "keep")
