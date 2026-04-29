"""Application configuration. All runtime tunables live here.

Each Typer command (and MCP tool) constructs ``Config.from_env()`` once at
entry and passes it explicitly down the call chain.

Static structural constants (image-internal paths like CONTAINER_HOME,
mount layouts) stay as module constants in their owning files -- they're
not user-tunable and are coupled to Dockerfile/shell-script invariants.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _load_dotenv(path: Path | None = None) -> None:
    """Read a .env file and inject its variables into ``os.environ``.

    Limitations: does not handle quoted values, ``export`` prefix, or escape
    sequences. Values are split on the first ``=`` and stripped of surrounding
    whitespace only.
    """
    dotenv = path or Path.cwd() / ".env"
    if not dotenv.is_file():
        return
    for line in dotenv.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key:
            os.environ[key.strip()] = value.strip()


@dataclass(frozen=True)
class Config:
    """All runtime-tunable settings.

    Field defaults apply when the corresponding ``CLD_*`` env var is unset.
    Construct via ``Config.from_env()`` at command entry; tests construct
    directly with kwargs.
    """

    # Docker image names
    base_image: str = "claude-base:latest"
    devcontainer_image: str = "claude-devcontainer:latest"
    agent_image: str = "claude-agent:latest"

    # Optional MySQL credentials (path to a .cnf file on the host)
    mysql_config: str = ""

    # Set by the host launcher when running inside a container, so Python
    # code (e.g. the orchestrator MCP server) can translate container-side
    # paths back to host paths for sibling -v mounts. Empty on the host.
    host_project_dir: str = ""
    host_home: str = ""

    # Loop tunables
    agent_timeout: int = 1800
    poll_interval: int = 30

    # Diagnostics
    debug: bool = False

    @classmethod
    def from_env(cls, dotenv: Path | None = None) -> "Config":
        """Build a ``Config`` from ``CLD_*`` env vars (loading ``.env`` first)."""
        _load_dotenv(dotenv)
        return cls(
            base_image=_env_str("CLD_BASE_IMAGE", "claude-base:latest"),
            devcontainer_image=_env_str("CLD_DEVCONTAINER_IMAGE", "claude-devcontainer:latest"),
            agent_image=_env_str("CLD_AGENT_IMAGE", "claude-agent:latest"),
            mysql_config=_env_str("CLD_MYSQL_CONFIG"),
            host_project_dir=_env_str("CLD_HOST_PROJECT_DIR"),
            host_home=_env_str("CLD_HOST_HOME"),
            agent_timeout=_env_int("CLD_AGENT_TIMEOUT", 1800),
            poll_interval=_env_int("CLD_POLL_INTERVAL", 30),
            debug=_env_bool("CLD_DEBUG"),
        )
