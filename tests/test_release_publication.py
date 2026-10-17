"""Exercise the release publisher against a fake GitHub CLI without network writes."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PublicationTests(unittest.TestCase):
    def test_existing_release_uploads_and_new_release_is_created(self):
        workflow = Path(".github/workflows/release.yml").read_text()
        # Execute the actual final shell step, rather than duplicating its logic.
        script = workflow.split("      - name: Publish GitHub release\n", 1)[1].split(
            "        run: |\n", 1
        )[1]
        script = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in script.splitlines()
        )
        for existing, moved in ((True, False), (False, False), (True, True)):
            with (
                self.subTest(existing=existing, moved=moved),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / "dist").mkdir()
                (root / "dist/package.whl").write_bytes(b"wheel")
                binary = root / "bin"
                binary.mkdir()
                gh = binary / "gh"
                gh.write_text(f"#!{sys.executable}\n" + """import json, os, sys
with open(os.environ['GH_COMMANDS'],'a') as stream:
    stream.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='api':print(os.environ['REMOTE_SHA'])
if sys.argv[1:3]==['release','view'] and os.environ['EXISTING']=='0':raise SystemExit(1)
""")
                gh.chmod(0o755)
                log = root / "commands"
                environment = dict(
                    os.environ,
                    PATH=str(binary) + os.pathsep + os.environ["PATH"],
                    RELEASE_TAG="v0.1.0b1",
                    RELEASE_SHA="a" * 40,
                    REMOTE_SHA=("b" if moved else "a") * 40,
                    GH_REPO="DMedina559/bsm-supervisor",
                    EXISTING="1" if existing else "0",
                    GH_COMMANDS=str(log),
                )
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                changes = [
                    cmd
                    for cmd in commands
                    if cmd[:2] in (["release", "upload"], ["release", "create"])
                ]
                if moved:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(changes, [])
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(changes), 1)
                    self.assertEqual(changes[0][1], "upload" if existing else "create")
                    self.assertNotIn("edit", [cmd[1] for cmd in commands])
