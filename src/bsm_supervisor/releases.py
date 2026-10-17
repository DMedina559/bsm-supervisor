"""Discover official BSM releases using bounded PyPI metadata requests."""

import json
import sys
from dataclasses import asdict, dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

PACKAGE = "bedrock-server-manager"
INDEX = "https://pypi.org/simple"
METADATA = "https://pypi.org/pypi/bedrock-server-manager/json"


class ReleaseError(RuntimeError):
    """Release discovery or installation failed."""


@dataclass(frozen=True)
class Release:
    """An installable published version for the current Python runtime."""

    version: str

    def to_dict(self) -> dict:
        """Return a JSON-compatible release summary."""
        return asdict(self)


def compatible_releases(metadata: dict) -> list[Release]:
    """Filter yanked, source-only, and incompatible releases using PEP 440."""
    python_version = ".".join(map(str, sys.version_info[:3]))
    versions = []
    supported_tags = set(sys_tags())
    for raw_version, files in metadata.get("releases", {}).items():
        try:
            version = Version(raw_version)
        except InvalidVersion:
            continue
        for file in files:
            if file.get("yanked") or file.get("packagetype") != "bdist_wheel":
                continue
            try:
                _, _, _, tags = parse_wheel_filename(file.get("filename", ""))
                if not tags.intersection(supported_tags):
                    continue
            except (InvalidWheelFilename, TypeError):
                continue
            try:
                supported = SpecifierSet(file.get("requires_python") or "")
            except InvalidSpecifier:
                continue
            if supported.contains(python_version, prereleases=True):
                versions.append(Release(str(version)))
                break
    return sorted(versions, key=lambda release: Version(release.version), reverse=True)


def discover() -> list[Release]:
    """Read only the official project's PyPI metadata."""
    request = Request(METADATA, headers={"User-Agent": "bsm-supervisor/0.1"})
    try:
        with urlopen(request, timeout=20) as response:
            if response.url != METADATA:
                raise ReleaseError("Unexpected PyPI metadata redirect")
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise ReleaseError("PyPI metadata exceeds the size limit")
        metadata = json.loads(payload)
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("releases"), dict
        ):
            raise ReleaseError("Invalid PyPI release metadata")
        return compatible_releases(metadata)
    except (OSError, URLError, ValueError, TypeError, AttributeError) as exc:
        raise ReleaseError(
            "Unable to read valid BSM release metadata from PyPI"
        ) from exc


def select(releases: list[Release], selector: str) -> Release:
    """Resolve stable, prerelease, or an exact version to a published release."""
    ordered = sorted(releases, key=lambda item: Version(item.version), reverse=True)
    if selector == "stable":
        matching = [
            item
            for item in ordered
            if not Version(item.version).is_prerelease
            and not Version(item.version).is_devrelease
        ]
    elif selector == "prerelease":
        matching = [item for item in ordered if Version(item.version).is_prerelease]
    else:
        try:
            requested = Version(selector)
        except InvalidVersion as exc:
            raise ReleaseError(
                "Choose stable, prerelease, or an exact version"
            ) from exc
        matching = [item for item in ordered if Version(item.version) == requested]
    if not matching:
        raise ReleaseError(
            "No compatible, non-yanked wheel release matches the selection"
        )
    return matching[0]
