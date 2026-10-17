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
        self.revision = "a" * 40
        self.checkout_name = "DMedina559-bsm-supervisor-" + self.revision[:7]
        # Exercise the exact embedded program: the downloaded shell script must
        # remain standalone before the supervisor package has been installed.
        installer = Path(__file__).resolve().parents[1] / "scripts/install-lxc.sh"
        self.program = (
            (
                installer.read_text()
                + (installer.parent / "lib/install-systemd.sh").read_text()
            )
            .split("<<'PYSOURCE'\n", 1)[1]
            .split("\nPYSOURCE\n", 1)[0]
        )

    def archive(self, extra=None):
        with tarfile.open(self.work / "source.tar.gz", "w:gz") as archive:
            entry = tarfile.TarInfo(self.checkout_name + "/pyproject.toml")
            contents = b"[project]\nname = 'bsm-supervisor'\n"
            entry.size = len(contents)
            archive.addfile(entry, io.BytesIO(contents))
            if extra is not None:
                archive.addfile(extra)

    def run_extractor(self):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                self.program,
                str(self.work),
                self.revision,
                "dev",
            ],
            capture_output=True,
            text=True,
        )

    def test_normal_source_archive_is_extracted(self):
        self.archive()
        result = self.run_extractor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.work / "source-path").read_text(),
            str(self.work / "source" / self.checkout_name),
        )
        self.assertTrue(
            (self.work / "source" / self.checkout_name / "pyproject.toml").is_file()
        )

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


class BootstrapLauncherTests(unittest.TestCase):
    def test_launcher_supports_repeat_install_and_preserves_unrelated_commands(self):
        installer = Path(__file__).resolve().parents[1] / "scripts/install-lxc.sh"
        function = (
            (
                installer.read_text()
                + (installer.parent / "lib/install-systemd.sh").read_text()
            )
            .split("publish_launcher() {\n", 1)[1]
            .split("\n}\n", 1)[0]
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bsm-supervisor"
            program = (
                "set -e; publish_launcher() {\n"
                + function
                + '\n}; publish_launcher "$1"'
            )
            for _ in range(2):
                result = subprocess.run(
                    ["bash", "-c", program, "test", str(destination)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    str(destination.readlink()),
                    "/opt/bsm-supervisor/current/bin/bsm-supervisor",
                )
            destination.unlink()
            destination.write_text("existing command")
            result = subprocess.run(
                ["bash", "-c", program, "test", str(destination)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_text(), "existing command")


class BootstrapProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        installer = Path(__file__).resolve().parents[1] / "scripts/install-lxc.sh"
        self.program = (
            (
                installer.read_text()
                + (installer.parent / "lib/install-systemd.sh").read_text()
            )
            .split("<<'PYREVISION'\n", 1)[1]
            .split("\nPYREVISION\n", 1)[0]
        )
        self.target = self.base / "environment"
        self.target.mkdir()

    def record(self, source):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                self.program,
                str(source),
                str(self.target),
                "dev",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import json

        return json.loads((self.target / "supervisor-installation.json").read_text())

    def test_remote_archive_records_exact_revision(self):
        source = self.base / ("repo-" + "a" * 40)
        source.mkdir()
        record = self.record(source)
        self.assertEqual(record["revision"], "a" * 40)
        self.assertEqual(record["source"], "github")

    def test_filtered_bootstrap_preserves_host_provenance(self):
        import json

        source = self.base / "filtered"
        source.mkdir()
        expected = {
            "source": "github",
            "revision": "b" * 40,
            "ref": "dev",
            "dirty": None,
        }
        (source / "supervisor-source.json").write_text(json.dumps(expected))
        self.assertEqual(self.record(source), expected)

    def test_plain_source_copy_reports_unknown_revision(self):
        source = self.base / "local"
        source.mkdir()
        self.assertIsNone(self.record(source)["revision"])


class PublicDownloadTests(unittest.TestCase):
    def test_public_download_uses_no_token_and_missing_public_ref_is_not_auth(self):
        import os

        for name in ("install-lxc.sh", "provision-proxmox.sh"):
            with self.subTest(script=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                binary = root / "bin"
                binary.mkdir()
                curl = binary / "curl"
                curl.write_text(f"#!{sys.executable}\n" + """import os, pathlib, sys
args=sys.argv[1:]
url=next(value for value in args if value.startswith('https://'))
available = os.environ.get('DOWNLOAD_FAIL') != '1'
status='200' if available or url.endswith('/bsm-supervisor') else '404'
if '-o' in args and status=='200':
    pathlib.Path(args[args.index('-o')+1]).write_text('public archive')
if '--write-out' in args: print(status,end='')
raise SystemExit(0 if status=='200' else 22)
""")
                curl.chmod(0o755)
                (root / "auth").touch(mode=0o600)
                source = (
                    Path(__file__).resolve().parents[1] / "scripts" / name
                ).read_text()
                function = source.split("    github_download() {\n", 1)[1].split(
                    "\n    }\n", 1
                )[0]
                url = (
                    "https://api.github.com/repos/DMedina559/bsm-supervisor/tarball/ref"
                )
                program = (
                    "set -e; work=$1; github_download() {\n"
                    + function
                    + f'\n}}; github_download "{url}" "$work/archive"'
                )
                environment = dict(
                    os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"]
                )
                result = subprocess.run(
                    ["bash", "-c", program, "test", str(root)],
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / "archive").read_text(), "public archive")
                environment["DOWNLOAD_FAIL"] = "1"
                result = subprocess.run(
                    ["bash", "-c", program, "test", str(root)],
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Public repository resource not found", result.stderr)
                self.assertNotIn("GitHub token for private", result.stderr)


class ReleaseLauncherVersionTests(unittest.TestCase):
    def test_overriding_pinned_ref_drops_implicit_release_version(self):
        for name in ("install-lxc.sh", "provision-proxmox.sh"):
            with self.subTest(script=name):
                text = (
                    Path(__file__).resolve().parents[1] / "scripts" / name
                ).read_text()
                text = text.replace(
                    "ref=latest\n", "ref=" + "a" * 40 + "\n", 1
                ).replace("package_version=\n", "package_version=0.1.0\n", 1)
                prefix = text.split("[[ $EUID == 0 ]]", 1)[0]
                resolution = next(
                    line
                    for line in text.splitlines()
                    if line.startswith('if [[ $ref != "$initial_ref" ]]')
                )
                program = (
                    prefix + "\n" + resolution + '\nprintf "%s" "$package_version"'
                )
                for flags, expected in (
                    ([], "0.1.0"),
                    (["--ref", "dev"], ""),
                    (["--ref", "dev", "--package-version", "0.2.0b1"], "0.2.0b1"),
                ):
                    result = subprocess.run(
                        ["bash", "-c", program, "test", *flags],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)
