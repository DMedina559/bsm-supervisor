"""Verify tag, development, archive and installer version metadata."""

import os
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

from setuptools_scm import get_version


class VersioningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        (self.checkout / ".git_archival.txt").write_bytes(
            Path(".git_archival.txt").read_bytes()
        )
        (self.checkout / ".gitattributes").write_bytes(
            Path(".gitattributes").read_bytes()
        )
        self.git("init", "-q")
        self.git("config", "user.name", "Version tests")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial version")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.checkout), *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
            timeout=15,
        ).stdout.strip()

    def version(self, path=None):
        return get_version(
            root=str(path or self.checkout),
            fallback_root=str(path or self.checkout),
            local_scheme="no-local-version",
        )

    def advance(self):
        path = self.checkout / "change.txt"
        path.write_text("change")
        self.git("add", ".")
        self.git("commit", "-qm", "Development change")

    def archive(self):
        path = self.root / "source.tar"
        self.git("archive", "--format=tar", "--output=" + str(path), "HEAD")
        target = self.root / "archive"
        target.mkdir()
        with tarfile.open(path) as source:
            source.extractall(target, filter="data")
        return target

    def test_stable_tag_and_following_commit_share_git_and_archive_versions(self):
        self.git("tag", "v0.1.0")
        self.assertEqual(self.version(), "0.1.0")
        self.advance()
        self.assertEqual(self.version(), "0.1.1.dev1")
        archive = self.archive()
        self.assertFalse((archive / ".git").exists())
        self.assertEqual(self.version(archive), "0.1.1.dev1")

    def test_prerelease_tag_and_following_commit(self):
        self.git("tag", "-a", "v0.2.0b1", "-m", "Beta")
        self.assertEqual(self.version(), "0.2.0b1")
        self.assertEqual(self.version(self.archive()), "0.2.0b1")
        self.advance()
        self.assertEqual(self.version(), "0.2.0b2.dev1")

    def test_installer_validates_override_without_rewriting_scm_source(self):
        script = Path("scripts/install-lxc.sh").read_text()
        program = script.split("<<'PYVERSION'\n", 1)[1].split("\nPYVERSION\n", 1)[0]
        (self.checkout / "pyproject.toml").write_text("[tool.setuptools_scm]\n")
        module = self.checkout / "src/bsm_supervisor/__init__.py"
        module.parent.mkdir(parents=True)
        module.write_text("# Metadata is read at runtime.\n")
        for version, code in (("0.1.0b1", 0), ("v0.1.0", 1), ("invalid", 1)):
            with self.subTest(version=version):
                result = subprocess.run(
                    [sys.executable, "-I", "-c", program, str(self.checkout), version],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(module.read_text(), "# Metadata is read at runtime.\n")
        self.assertIn(
            "export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSM_SUPERVISOR="
            '"$package_version"',
            script,
        )

    def test_build_configuration_uses_tag_metadata_without_local_suffix(self):
        project = tomllib.loads(Path("pyproject.toml").read_text())
        self.assertIn("version", project["project"]["dynamic"])
        self.assertNotIn("dynamic", project["tool"]["setuptools"])
        self.assertEqual(
            project["tool"]["setuptools_scm"]["local_scheme"], "no-local-version"
        )
        self.assertTrue(
            any(
                item.startswith("setuptools-scm")
                for item in project["build-system"]["requires"]
            )
        )

    def test_proxmox_copy_preserves_checkout_and_archive_versions(self):
        self.git("tag", "v0.1.0")
        self.advance()
        copied = self.root / "filtered"
        copied.mkdir()
        script = str(Path("scripts/archive-version.py").resolve())
        for source in (self.checkout, self.archive()):
            subprocess.run(
                [
                    sys.executable,
                    script,
                    str(source),
                    str(copied / ".git_archival.txt"),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(self.version(copied), "0.1.1.dev1")

    def test_release_workflow_sets_build_version_without_dirtying_source(self):
        workflow = Path(".github/workflows/release.yml").read_text()
        step = workflow.split(
            "- name: Set the package version from the release tag", 1
        )[1]
        program = textwrap.dedent(
            step.split("python - <<'PY'\n", 1)[1].split("          PY\n", 1)[0]
        )
        (self.checkout / "pyproject.toml").write_text("[tool.setuptools_scm]\n")
        output = self.root / "github-env"
        result = subprocess.run(
            [sys.executable, "-I", "-c", program],
            cwd=self.checkout,
            env={**os.environ, "RELEASE_TAG": "v0.2.0b1", "GITHUB_ENV": str(output)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            output.read_text(),
            "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSM_SUPERVISOR=0.2.0b1\n",
        )
        self.assertFalse((self.checkout / "src").exists())
