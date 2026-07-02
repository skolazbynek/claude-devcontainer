"""Application configuration. All runtime tunables live here.

Each Typer command (and MCP tool) constructs ``Config.from_env()`` once at
entry and passes it explicitly down the call chain.

Static structural constants (image-internal paths like CONTAINER_HOME,
mount layouts) stay as module constants in their owning files -- they're
not user-tunable and are coupled to Dockerfile/shell-script invariants.
"""

import logging
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cld.log import _CldFormatter, _LazyStderrHandler

_log = logging.getLogger("cld.config")
if not _log.handlers:
    _h = _LazyStderrHandler()
    _h.setFormatter(_CldFormatter(use_color=False))
    _log.addHandler(_h)
    _log.propagate = False


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


_TOML_KEYS = {
    "base_image",
    "devcontainer_image",
    "agent_image",
    "mysql_config",
    "agent_timeout",
    "poll_interval",
    "debug",
    "home_mounts_always",
    "home_mounts_devcontainer",
    "trunk_candidates",
    "ssl_certs_path",
    "chain_max_parallel",
    "chain_default_model",
    "log_level",
    "log_color",
    "ignore_gitignore",
    "ssh_auth_sock",
    "mailbox_root",
    "agent_max_turns",
    "agent_kickoff_persona",
}


_DEFAULT_CONFIG_TEMPLATE = Path(__file__).parent / "config.default.toml"


def _user_config_path() -> Path:
    return Path.home() / ".config" / "cld" / "config.toml"


def _default_mailbox_root() -> str:
    return str(Path.home() / ".cld" / "mailboxes")


def _ensure_user_config(path: Path) -> None:
    """Copy the default template to ``path`` if it does not exist yet."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_DEFAULT_CONFIG_TEMPLATE, path)
    _log.warning("created default config at %s", path)


def _find_project_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (or cwd) looking for ``.cld.config``.

    Independent of VCS detection so config can be discovered before a backend
    is required (and so a missing VCS does not abort startup).
    """
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / ".cld.config"
        if candidate.is_file():
            return candidate
    return None


def _load_toml(path: Path) -> dict:
    """Read a TOML file, warn on parse errors or unknown keys; return known keys only."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log.warning("failed to read %s: %s", path, e)
        return {}
    unknown = set(data) - _TOML_KEYS
    for key in sorted(unknown):
        _log.warning("unknown key '%s' in %s", key, path)
    return {k: v for k, v in data.items() if k in _TOML_KEYS}


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


def _resolve_ssh_auth_sock(layered: dict) -> str | None:
    """Tri-state resolution: env takes precedence over TOML; absent -> None."""
    if "CLD_SSH_AUTH_SOCK" in os.environ:
        return os.environ["CLD_SSH_AUTH_SOCK"]
    if "ssh_auth_sock" in layered:
        return layered["ssh_auth_sock"]
    return None


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

    # SSL CA certificates path on the host (dir or file).
    # Empty = auto-detect: /etc/ssl/certs (Linux) then /etc/ssl/cert.pem (macOS).
    # Set explicitly to use a custom CA bundle; leave empty to skip if neither found.
    ssl_certs_path: str = ""

    # RO $HOME paths staged into every container (relative to $HOME)
    home_mounts_always: tuple[str, ...] = (
        ".claude.json",
        ".config/anthropic",
        ".config/claude",
        ".config/jj",
    )
    # Additional RO $HOME paths staged only for devcontainer
    home_mounts_devcontainer: tuple[str, ...] = (
        ".gitconfig",
        ".bashrc",
        ".config/nvim",
        ".local/state/nvim",
        ".cache/nvim",
    )

    # Ordered list of branch names tried when auto-detecting trunk for `cld review`
    trunk_candidates: tuple[str, ...] = ("main", "master", "trunk")

    # Set by the host launcher when running inside a container, so Python
    # code (e.g. the orchestrator MCP server) can translate container-side
    # paths back to host paths for sibling -v mounts. Empty on the host.
    host_project_dir: str = ""
    host_home: str = ""

    # Loop tunables
    agent_timeout: int = 1800
    poll_interval: int = 30

    # Chain orchestration tunables
    chain_max_parallel: int = 4
    chain_default_model: str = ""

    # Workspace setup: gitignored files to symlink into workspace
    ignore_gitignore: tuple[str, ...] = ()

    # SSH agent forwarding for devcontainer sessions.
    # None (unset) = auto-detect from $SSH_AUTH_SOCK on the host.
    # "" (explicitly empty) = disable forwarding entirely.
    # "/path/to/socket" = use that host socket path explicitly.
    ssh_auth_sock: str | None = None

    # Inter-container agent messaging (mailboxes + repo agent supervisor)
    mailbox_root: str = _default_mailbox_root()
    agent_max_turns: int = 30
    agent_kickoff_persona: str = "repo-agent"

    # Diagnostics
    debug: bool = False
    log_level: str = "INFO"
    log_color: str = "auto"

    @classmethod
    def from_env(
        cls,
        dotenv: Path | None = None,
        user_config: Path | None = None,
        project_config: Path | None = None,
    ) -> "Config":
        """Build a ``Config`` layering: defaults < user TOML < project TOML < .env < CLD_* env."""
        _load_dotenv(dotenv)
        layered: dict = {}
        up = user_config if user_config is not None else _user_config_path()
        _ensure_user_config(up)
        if up.is_file():
            layered.update(_load_toml(up))
        pp = project_config if project_config is not None else _find_project_config()
        if pp and pp.is_file():
            layered.update(_load_toml(pp))
        return cls(
            base_image=_env_str("CLD_BASE_IMAGE", layered.get("base_image", "claude-base:latest")),
            devcontainer_image=_env_str("CLD_DEVCONTAINER_IMAGE", layered.get("devcontainer_image", "claude-devcontainer:latest")),
            agent_image=_env_str("CLD_AGENT_IMAGE", layered.get("agent_image", "claude-agent:latest")),
            mysql_config=_env_str("CLD_MYSQL_CONFIG", layered.get("mysql_config", "")),
            ssl_certs_path=_env_str("CLD_SSL_CERTS_PATH", layered.get("ssl_certs_path", "")),
            host_project_dir=_env_str("CLD_HOST_PROJECT_DIR"),
            host_home=_env_str("CLD_HOST_HOME"),
            agent_timeout=_env_int("CLD_AGENT_TIMEOUT", int(layered.get("agent_timeout", 1800))),
            poll_interval=_env_int("CLD_POLL_INTERVAL", int(layered.get("poll_interval", 30))),
            debug=_env_bool("CLD_DEBUG", bool(layered.get("debug", False))),
            log_level=_env_str("CLD_LOG_LEVEL", layered.get("log_level", "INFO")),
            log_color=_env_str("CLD_LOG_COLOR", layered.get("log_color", "auto")),
            home_mounts_always=tuple(layered.get("home_mounts_always", (
                ".claude.json", ".config/anthropic", ".config/claude", ".config/jj",
            ))),
            home_mounts_devcontainer=tuple(layered.get("home_mounts_devcontainer", (
                ".gitconfig", ".bashrc", ".config/nvim", ".local/state/nvim", ".cache/nvim",
            ))),
            trunk_candidates=tuple(layered.get("trunk_candidates", ("main", "master", "trunk"))),
            chain_max_parallel=_env_int("CLD_CHAIN_MAX_PARALLEL", int(layered.get("chain_max_parallel", 4))),
            chain_default_model=_env_str("CLD_CHAIN_DEFAULT_MODEL", layered.get("chain_default_model", "")),
            ignore_gitignore=tuple(layered.get("ignore_gitignore", ())),
            ssh_auth_sock=_resolve_ssh_auth_sock(layered),
            mailbox_root=_env_str("CLD_MAILBOX_ROOT", layered.get("mailbox_root", _default_mailbox_root())),
            agent_max_turns=_env_int("CLD_AGENT_MAX_TURNS", int(layered.get("agent_max_turns", 30))),
            agent_kickoff_persona=_env_str("CLD_AGENT_KICKOFF_PERSONA", layered.get("agent_kickoff_persona", "repo-agent")),
        )
