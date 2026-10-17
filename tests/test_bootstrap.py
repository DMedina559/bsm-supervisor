"""Run the standalone installer's embedded archive validation without root changes."""

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


class BootstrapArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        # Exercise the exact embedded program: the downloaded shell script must
        # remain standalone before the supervisor package has been installed.
        installer = Path(__file__).resolve().parents[1] / "scripts/install-lxc.sh"
        self.program = (
            installer.read_text()
            .split("python3 - \"$work\" <<'PY'\n", 1)[1]
            .split("\nPY\n", 1)[0]
        )

    def archive(self, extra=None):
        with tarfile.open(self.work / "source.tar.gz", "w:gz") as archive:
            entry = tarfile.TarInfo("repo/pyproject.toml")
            contents = b"[project]\nname = 'bsm-supervisor'\n"
            entry.size = len(contents)
            archive.addfile(entry, io.BytesIO(contents))
            if extra is not None:
                archive.addfile(extra)

    def run_extractor(self):
        return subprocess.run(
            [sys.executable, "-I", "-c", self.program, str(self.work)],
            capture_output=True,
            text=True,
        )

    def test_normal_source_archive_is_extracted(self):
        self.archive()
        result = self.run_extractor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.work / "source-path").read_text(), str(self.work / "source/repo")
        )
        self.assertTrue((self.work / "source/repo/pyproject.toml").is_file())

    def test_traversal_is_rejected_before_any_extraction(self):
        self.archive(tarfile.TarInfo("../../escaped"))
        result = self.run_extractor()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe source archive", result.stderr)
        self.assertFalse((self.base / "escaped").exists())
        self.assertFalse((self.work / "source").exists())

    def test_symlinks_hardlinks_and_devices_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                entry = tarfile.TarInfo("repo/escape")
                entry.type = kind
                entry.linkname = "/etc/passwd"
                self.archive(entry)
                result = self.run_extractor()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsafe source archive", result.stderr)
                self.assertFalse((self.work / "source").exists())
