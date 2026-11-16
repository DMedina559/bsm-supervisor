"""Regenerate the shared support embedded in standalone bootstrap scripts."""

import argparse
from pathlib import Path


def sync(root: Path, check: bool = False) -> None:
    shared = (root / "lib/bootstrap.sh").read_text().split("\n", 1)[1]
    for name in ("install-lxc.sh", "provision-proxmox.sh"):
        path = root / name
        original = path.read_text()
        before, rest = original.split("# BEGIN SHARED BOOTSTRAP\n", 1)
        _, after = rest.split("# END SHARED BOOTSTRAP\n", 1)
        updated = (
            before
            + "# BEGIN SHARED BOOTSTRAP\n"
            + shared
            + "# END SHARED BOOTSTRAP\n"
            + after
        )
        if check:
            if original != updated:
                raise SystemExit(f"Regenerate shared bootstrap support: {name}")
        else:
            path.write_text(updated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sync(Path(__file__).resolve().parent, args.check)
