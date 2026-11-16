"""Manage Bedrock Server Manager independently of its application runtime."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bsm-supervisor")
except PackageNotFoundError:
    __version__ = "0+unknown"
