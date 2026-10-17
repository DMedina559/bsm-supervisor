"""Install code safely under the console's private umask."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_releases as fixtures

from bsm_supervisor.install import install, run_command
from bsm_supervisor.installation_permissions import create_root, service_readable
from bsm_supervisor.releases import Release


class InstallationPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_code_is_readable_without_changing_credentials_links_or_write_bits(self):
        target = self.root / "environment"
        target.mkdir(mode=0o700)
        (target / "bin").mkdir(mode=0o700)
        entry = target / "bin/bedrock-server-manager"
        entry.write_text("#!/usr/bin/python3\n")
        entry.chmod(0o700)
        module = target / "module.py"
        module.write_text("# package code\n")
        module.chmod(0o600)
        metadata = target / "supervisor-installation.json"
        metadata.write_text("{}")
        metadata.chmod(0o600)
        outside = self.root / "private-token"
        outside.write_text("a private credential")
        outside.chmod(0o600)
        (target / "external").symlink_to(outside)
        service_readable(target)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(entry.stat().st_mode & 0o777, 0o755)
        self.assertEqual(module.stat().st_mode & 0o777, 0o644)
        self.assertEqual(metadata.stat().st_mode & 0o777, 0o600)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o600)
        self.assertTrue((target / "external").is_symlink())

    def test_new_directories_ignore_private_umask_and_preserve_existing_parent(self):
        mask = os.umask(0o077)
        try:
            target = self.root / "new-parent/environments"
            create_root(target)
        finally:
            os.umask(mask)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_install_children_use_public_code_umask_without_changing_parent(self):
        target = self.root / "child-code"
        log = self.root / "install.log"
        mask = os.umask(0o077)
        try:
            run_command(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    (
                        "from pathlib import Path; import sys; "
                        "Path(sys.argv[1]).write_text('code')"
                    ),
                    str(target),
                ],
                log,
            )
            private = self.root / "parent-private"
            private.touch()
        finally:
            os.umask(mask)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)


class EnvironmentInstallPermissionsTests(unittest.TestCase):
    setUp = fixtures.InstallationTests.setUp
    fake_command = fixtures.InstallationTests.fake_command

    def test_pypi_install_and_reuse_repair_service_access_under_private_umask(self):
        mask = os.umask(0o077)
        try:
            with patch(
                "bsm_supervisor.install.run_command", side_effect=self.fake_command
            ):
                result = install(self.config, self.store, Release("4.0.0b7"))
            target = Path(result["path"])
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual((target / "bin").stat().st_mode & 0o777, 0o755)
            self.assertEqual(
                (target / "supervisor-installation.json").stat().st_mode & 0o777, 0o600
            )
            target.chmod(0o700)
            (target / "bin").chmod(0o700)
            with patch("bsm_supervisor.install.run_command"):
                reused = install(self.config, self.store, Release("4.0.0b7"))
            self.assertTrue(reused["reused"])
            self.assertEqual((target / "bin").stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        finally:
            os.umask(mask)
