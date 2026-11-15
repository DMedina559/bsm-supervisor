"""Release build and verification helpers for bsm-supervisor."""

import argparse
import hashlib
from importlib.metadata import version as dist_version
import json
import os
from pathlib import Path
import re
import tomllib

from packaging.version import Version

EXPECTED_ASSETS = (
    "index.html",
    "app.js",
    "admin.js",
    "style.css",
    "frontend/tokens.css",
    "frontend/base.css",
    "frontend/buttons.css",
    "frontend/forms.css",
    "frontend/icon-192.png",
    "frontend/favicon-96.png",
    "frontend/LICENSE",
    "frontend/SOURCE.json",
)


def prepare_version(release_tag: str) -> None:
    version = str(Version(release_tag.removeprefix("v")))
    if release_tag != f"v{version}":
        raise SystemExit("Use a normalized Python version tag such as v0.1.0b1")

    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as stream:
            stream.write(f"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BSM_SUPERVISOR={version}\n")

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    if "setuptools_scm" not in project.get("tool", {}):
        # Preserve repair builds of releases predating SCM versioning.
        init_path = Path("src/bsm_supervisor/__init__.py")
        content, count = re.subn(
            r'^__version__ = "[^"]+"$',
            f'__version__ = "{version}"',
            init_path.read_text(encoding="utf-8"),
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise SystemExit("Missing package version declaration")
        init_path.write_text(content, encoding="utf-8")


def verify_wheel(release_tag: str) -> None:
    import bsm_supervisor

    expected_version = release_tag[1:]
    actual_version = dist_version("bsm-supervisor")
    if actual_version != expected_version:
        raise SystemExit(f"Expected version {expected_version}, got {actual_version}")

    assets_root = Path(bsm_supervisor.__file__).parent / "assets"
    for name in EXPECTED_ASSETS:
        asset = assets_root / name
        if not asset.is_file():
            raise SystemExit(f"Missing packaged asset: {asset}")


def package_assets(release_tag: str, release_sha: str) -> None:
    root = Path("dist")
    for name in ("install-lxc.sh", "provision-proxmox.sh"):
        path = root / name
        content = path.read_text(encoding="utf-8")
        content = content.replace("ref=latest\n", f"ref={release_sha}\n", 1)
        content = content.replace("package_version=\n", f"package_version={release_tag[1:]}\n", 1)
        path.write_text(content, encoding="utf-8")

    (root / "release.json").write_text(
        json.dumps({"tag": release_tag, "commit": release_sha}, indent=2),
        encoding="utf-8",
    )
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(root.iterdir())
    )
    (root / "SHA256SUMS").write_text(checksums, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Release helpers for bsm-supervisor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prep = subparsers.add_parser("prepare-version")
    prep.add_argument("tag")

    verify = subparsers.add_parser("verify-wheel")
    verify.add_argument("tag")

    pkg = subparsers.add_parser("package-assets")
    pkg.add_argument("tag")
    pkg.add_argument("sha")

    args = parser.parse_args()
    if args.command == "prepare-version":
        prepare_version(args.tag)
    elif args.command == "verify-wheel":
        verify_wheel(args.tag)
    elif args.command == "package-assets":
        package_assets(args.tag, args.sha)


if __name__ == "__main__":
    main()
