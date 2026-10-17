"""Preserve SCM version metadata when copying package inputs into an LXC."""

import argparse
import shutil
import subprocess
from pathlib import Path


def metadata(source: Path) -> str:
    """Expand commit metadata or retain an already expanded GitHub archive."""
    if (source / ".git").exists():
        if not shutil.which("git"):
            raise ValueError("Git is required to copy a checkout's version metadata")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "log",
                "-1",
                "--format=node: %H%nnode-date: %cI%n"
                "describe-name: %(describe:tags=true,match=v[0-9]*)",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        value = result.stdout
    else:
        value = (source / ".git_archival.txt").read_text()
    if "$Format:" in value or not any(
        line.startswith("describe-name: v") for line in value.splitlines()
    ):
        raise ValueError("Source version metadata needs a reachable release tag")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(metadata(args.source))
