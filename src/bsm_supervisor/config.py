"""Load and validate the supervisor's local configuration."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .environment import overrides


@dataclass(frozen=True)
class Config:
    """Paths and the single systemd unit managed by this supervisor."""

    state_dir: Path
    installations_dir: Path
    data_dir: Path
    service_unit: str = "bedrock-server-manager-webui.service"
    service_timeout: int = 240
    bsm_config_dir: Path | None = None
    checkpoint_paths: tuple[Path, ...] = ()
    api_token_file: Path | None = None
    readiness_timeout: int = 60
    bsm_host: str = "127.0.0.1"
    bsm_port: int = 11325
    bsm_db_url: str | None = None
    bsm_log_level: str | None = None

    @property
    def config_dir(self) -> Path:
        """Return BSM's explicit bootstrap/configuration directory."""
        return self.bsm_config_dir or self.data_dir / "config"

    def __post_init__(self) -> None:
        if not isinstance(self.bsm_host, str) or self.bsm_host not in (
            "127.0.0.1",
            "0.0.0.0",
            "::1",
            "::",
        ):
            raise ValueError("bsm_host must be a loopback or wildcard IP address")
        if type(self.bsm_port) is not int or not 1 <= self.bsm_port <= 65535:
            raise ValueError("bsm_port must be an integer from 1 to 65535")
        if self.bsm_db_url is not None and (
            not isinstance(self.bsm_db_url, str)
            or not self.bsm_db_url
            or len(self.bsm_db_url) > 16384
            or not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", self.bsm_db_url)
            or any(ord(char) < 32 or ord(char) == 127 for char in self.bsm_db_url)
        ):
            raise ValueError("bsm_db_url must be a nonempty single-line URL")
        if self.bsm_log_level not in (
            None,
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
            "NOTSET",
        ):
            raise ValueError("Invalid BSM log level")
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
        for path in (self.config_dir, *self.checkpoint_paths):
            if not path.is_absolute() or path.resolve() == Path("/"):
                raise ValueError("Checkpoint paths must be absolute and not the root")
            for private in (self.state_dir.resolve(), self.installations_dir.resolve()):
                resolved_path = path.resolve()
                if (
                    private == resolved_path
                    or private in resolved_path.parents
                    or resolved_path in private.parents
                ):
                    raise ValueError(
                        "Checkpoint paths must not overlap supervisor paths"
                    )
        if self.api_token_file is not None and not self.api_token_file.is_absolute():
            raise ValueError("api_token_file must be absolute")
        if (
            type(self.readiness_timeout) is not int
            or not 1 <= self.readiness_timeout <= 3600
        ):
            raise ValueError("readiness_timeout must be an integer from 1 to 3600")
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
            "bsm_config_dir",
            "checkpoint_paths",
            "api_token_file",
            "readiness_timeout",
            "bsm_host",
            "bsm_port",
            "bsm_db_url",
            "bsm_log_level",
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
        if "api_token_file" in values:
            if not isinstance(values["api_token_file"], str):
                raise ValueError("api_token_file must be an absolute path")
            values["api_token_file"] = Path(values["api_token_file"])
        if "bsm_config_dir" in values:
            if not isinstance(values["bsm_config_dir"], str):
                raise ValueError("bsm_config_dir must be an absolute path")
            values["bsm_config_dir"] = Path(values["bsm_config_dir"])
        if "checkpoint_paths" in values:
            paths = values["checkpoint_paths"]
            if not isinstance(paths, list) or any(
                not isinstance(path, str) or not path for path in paths
            ):
                raise ValueError("checkpoint_paths must be an array of absolute paths")
            values["checkpoint_paths"] = tuple(Path(path) for path in paths)
        if "service_unit" in values and not isinstance(values["service_unit"], str):
            raise ValueError("service_unit must be a string")
        environment = overrides()
        for name, key in (
            ("BSM_DATA_DIR", "data_dir"),
            ("BSM_CONFIG_DIR", "bsm_config_dir"),
        ):
            if name in environment:
                if not environment[name]:
                    raise ValueError(name + " must be a nonempty absolute path")
                values[key] = Path(environment[name])
        if "BSM_DB_URL" in environment:
            values["bsm_db_url"] = environment["BSM_DB_URL"]
        if "BSM_LOG_LEVEL" in environment:
            level = environment["BSM_LOG_LEVEL"].upper()
            values["bsm_log_level"] = {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(
                level, level
            )
        if "HOST" in environment:
            values["bsm_host"] = environment["HOST"]
        if "PORT" in environment and "BSM_SUPERVISOR_WEB_PORT" not in environment:
            environment["BSM_SUPERVISOR_WEB_PORT"] = environment["PORT"]
        if "BSM_SUPERVISOR_WEB_HOST" in environment:
            values["bsm_host"] = environment["BSM_SUPERVISOR_WEB_HOST"]
        if "BSM_SUPERVISOR_WEB_PORT" in environment:
            port = environment["BSM_SUPERVISOR_WEB_PORT"]
            if not port.isascii() or not port.isdecimal():
                raise ValueError("BSM_SUPERVISOR_WEB_PORT must contain an integer")
            values["bsm_port"] = int(port)
        return cls(**values)
