"""Select the supported service adapter and retain public integration helpers."""

from .backends.base import ManagedService as ManagedService
from .backends.base import ServiceError as ServiceError
from .backends.systemd import SystemdService as SystemdService
from .backends.systemd import guard_command as guard_command
from .backends.systemd import managed_command as managed_command
from .backends.systemd import unset_environment as unset_environment
from .config import Config


def service_backend(config: Config) -> ManagedService:
    """Use the supported systemd adapter for the configured BSM instance."""
    return SystemdService(config)


__all__ = [
    "ManagedService",
    "ServiceError",
    "SystemdService",
    "guard_command",
    "managed_command",
    "service_backend",
    "unset_environment",
]
