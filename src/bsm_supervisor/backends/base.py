"""Service operations consumed by supervisor workflows."""

from typing import Protocol


class ServiceError(RuntimeError):
    """A managed service operation failed or could not be verified."""


class ManagedService(Protocol):
    """Control one configured service without selecting a platform in callers.

    Status retains the established managed-service property contract. Unit
    installation, timer management and process identity verification are separate
    Linux/systemd integrations.
    """

    def status(self) -> dict[str, str]:
        """Return observed service state, process identity and pending work."""
        ...

    def control(self, action: str) -> None:
        """Start, stop or restart the configured managed service."""
        ...

    def reset_failed(self) -> None:
        """Clear a confirmed stopped failure before another start."""
        ...
