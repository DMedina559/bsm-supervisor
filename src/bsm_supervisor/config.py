"""Load and validate the supervisor's local configuration."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Paths and the single systemd unit managed by this supervisor."""

    state_dir: Path
    installations_dir: Path
    data_dir: Path
    service_unit: str = "bedrock-server-manager-webui.service"
    service_timeout: int = 240

    def __post_init__(self) -> None:
        paths = (self.state_dir, self.installations_dir, self.data_dir)
        for path in paths:
            if not path.is_absolute():
                raise ValueError("All configured paths must be absolute")
        resolved = [path.resolve() for path in paths]
        for index, left in enumerate(resolved):
            for right in resolved[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise ValueError(
                        "State, installations, and data paths must not overlap"
                    )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.service", self.service_unit):
            raise ValueError("service_unit must be a plain systemd service name")
        if (
            type(self.service_timeout) is not int
            or not 1 <= self.service_timeout <= 3600
        ):
            raise ValueError("service_timeout must be an integer from 1 to 3600")

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Read a TOML file, rejecting unknown keys and invalid values."""
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        allowed = {
            "state_dir",
            "installations_dir",
            "data_dir",
            "service_unit",
            "service_timeout",
        }
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(
                f"Unknown configuration keys: {', '.join(sorted(unknown))}"
            )
        for key in ("state_dir", "installations_dir", "data_dir"):
            value = values.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{key} must be a nonempty absolute path")
            values[key] = Path(value)
        if "service_unit" in values and not isinstance(values["service_unit"], str):
            raise ValueError("service_unit must be a string")
        return cls(**values)
