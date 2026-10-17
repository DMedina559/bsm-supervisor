"""Shared operator service actions with optional application readiness."""

from . import runtime
from .activation import selection
from .config import Config
from .integration import require_managed
from .service import ServiceError, SystemdService
from .store import Store


def control(config: Config, store: Store, action: str, wait: bool = False) -> None:
    """Validate before service control and retain uncertain startup outcomes."""
    with store.lock():
        service = SystemdService(config)
        current = selection(config, store) if action in ("start", "restart") else None
        if action in ("start", "restart"):
            if current is None:
                raise ValueError(
                    "No BSM installation selected. Run installations, then "
                    "activate INSTALLATION_ID --offline-confirmed while stopped"
                )
            require_managed(config, service.status())
        with store.mutation("service:" + action, preserve_on_error=True):
            try:
                service.control(action)
                if current is not None:
                    if wait:
                        runtime.wait_ready(config, current["version"])
                    else:
                        state = service.status()
                        if state.get("ActiveState") != "active" or state.get(
                            "MainPID"
                        ) in (None, "", "0"):
                            raise ServiceError("BSM did not remain running")
            except (OSError, ValueError, ServiceError, runtime.RuntimeErrorBase) as exc:
                raise ServiceError(
                    f"{exc}. Inspect status, history, and "
                    f"journalctl -u {config.service_unit} -n 60 --no-pager"
                ) from exc
