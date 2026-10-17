"""Exercise host preflight and plans against a fake Proxmox command interface."""

import json
import os
import pty
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProxmoxPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "commands.jsonl"
        template = self.base / "debian-template.tar.zst"
        template.touch()
        program = f"#!{sys.executable}\n" + """import json, os, pathlib, sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'pvesh':
    print('901' if '/cluster/nextid' in args else '[{"vmid": 900, "type": "qemu"}]')
elif name == 'pveversion':
    print('pve-manager/' + os.environ.get('PVE_MAJOR', '9') + '.2.0/test')
elif name == 'pvesm':
    if args[0] == 'path':
        print(os.environ['TEMPLATE_PATH'])
    else:
        print('Name Type Status Total Used Available %')
        print('local-lvm lvmthin active 100 0 100 0%')
        print('local dir active 100 0 100 0%')
elif name == 'pveam':
    if args[0] == 'available':
        print('system debian-12-standard_12.9-1_amd64.tar.zst')
        print('system debian-12-standard_12.10-1_amd64.tar.zst')
        if os.environ.get('DEBIAN13'):
            print('system debian-13-standard_13.1-1_amd64.tar.zst')
else:
    if name == 'pct':
        if args == ['help', 'create']:
            supported = not os.environ.get('NO_NATIVE_ENV')
            print('--env <string>' if supported else '--hostname <string>')
            raise SystemExit(0)
        if os.environ.get('CAPTURE_UMASK'):
            print(format(os.umask(0), '03o'))
            raise SystemExit(0)
        raise SystemExit('pct mutation must not run during dry-run')
"""
        for name in ("pct", "pvesh", "pvesm", "pveam", "pveversion", "ip"):
            executable = self.bin / name
            executable.write_text(program)
            executable.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "COMMAND_LOG": str(self.log),
            "TEMPLATE_PATH": str(template),
        }
        self.script = (
            Path(__file__).resolve().parents[1] / "scripts/provision-proxmox.sh"
        )

    def run_plan(self, *args):
        return subprocess.run(
            ["bash", str(self.script), "--dry-run", *args],
            env=self.environment,
            capture_output=True,
            text=True,
        )

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_pct_uses_normal_guest_permissions_without_changing_host_mask(self):
        program = self.script.read_text().split("pct() {\n", 1)[1].split("\n}\n", 1)[0]
        result = subprocess.run(
            [
                "bash",
                "-c",
                "umask 077; pct() {\n" + program + "\n}; pct create 901; umask",
            ],
            env={**self.environment, "CAPTURE_UMASK": "1"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        masks = result.stdout.splitlines()
        self.assertEqual(int(masks[0], 8), 0o022)
        self.assertEqual(int(masks[1], 8), 0o077)

    def test_defaults_select_latest_template_and_never_mutate(self):
        result = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        create = shlex.split(result.stdout.splitlines()[1])
        self.assertEqual(create[:3], ["pct", "create", "901"])
        self.assertIn("local:vztmpl/debian-12-standard_12.10-1_amd64.tar.zst", create)
        self.assertEqual(create[create.index("--unprivileged") + 1], "1")
        self.assertIn("local-lvm:16", create)
        self.assertIn("BSM_SUPERVISOR_WEB_HOST=0.0.0.0", create)
        self.assertFalse(
            any(
                command[0] == "pct" and command[1:3] != ["help", "create"]
                for command in self.commands()
            )
        )
        self.assertFalse(
            any(
                command[:2] in (["pveam", "update"], ["pveam", "download"])
                for command in self.commands()
            )
        )

    def test_debian13_is_preferred_when_available(self):
        self.environment["DEBIAN13"] = "1"
        result = self.run_plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("debian-13-standard_13.1-1_amd64.tar.zst", result.stdout)

    def test_old_hosts_and_missing_environment_capability_are_rejected(self):
        for overrides in ({"PVE_MAJOR": "8"}, {"NO_NATIVE_ENV": "1"}):
            with self.subTest(overrides=overrides):
                self.environment.update(overrides)
                result = self.run_plan()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("pct create", result.stdout)
                self.environment.pop(next(iter(overrides)))

    def test_default_wizard_uses_storage_menus_in_a_terminal(self):
        executable = self.bin / "whiptail"
        executable.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            " *'Choose setup mode'*) echo default >&2 ;;\n"
            " *'Select rootdir storage'*) echo local-lvm >&2 ;;\n"
            " *) echo local >&2 ;;\nesac\n"
        )
        executable.chmod(0o755)
        master, slave = pty.openpty()
        try:
            result = subprocess.run(
                ["bash", str(self.script), "--interactive", "--dry-run"],
                stdin=slave,
                capture_output=True,
                text=True,
                env=self.environment,
                timeout=10,
            )
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BSM Supervisor", result.stdout)
        self.assertIn("pct create 901", result.stdout)
        self.assertIn("Dry run", result.stdout)

    def test_existing_qemu_id_blocks_container_creation(self):
        result = self.run_plan("--ctid", "900")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing guest", result.stderr)
        self.assertNotIn("pct create", result.stdout)

    def test_custom_network_resources_and_existing_template(self):
        result = self.run_plan(
            "--ctid",
            "777",
            "--template",
            "local:vztmpl/debian.tar.zst",
            "--ip",
            "192.168.10.20/24",
            "--gateway",
            "192.168.10.1",
            "--nameserver",
            "192.168.10.2",
            "--vlan",
            "42",
            "--memory",
            "4096",
            "--disk",
            "64",
            "--onboot",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        create = shlex.split(result.stdout.splitlines()[1])
        self.assertIn("local-lvm:64", create)
        network = create[create.index("--net0") + 1]
        self.assertIn("ip=192.168.10.20/24", network)
        self.assertIn("gw=192.168.10.1,tag=42", network)
        self.assertEqual(create[create.index("--onboot") + 1], "1")
        self.assertFalse(any(command[0] == "pveam" for command in self.commands()))

    def test_invalid_network_resources_and_option_injection_fail(self):
        for args in (
            ("--bridge", "vmbr0,firewall=0"),
            ("--memory", "0"),
            ("--vlan", "4095"),
            ("--ip", "192.168.1.9/24"),
            ("--gateway", "192.168.1.1"),
            ("--ctid", "99"),
            ("--template", "--evil"),
        ):
            with self.subTest(args=args):
                result = self.run_plan(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("pct create", result.stdout)
