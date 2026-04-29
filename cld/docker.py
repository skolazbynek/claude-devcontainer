"""Container setup: arg building, image management, path translation."""

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from cld.config import Config
from cld.vcs import get_backend

# Static structural constants (Dockerfile- and shell-script-coupled, not user-tunable).
CONTAINER_USER = "claude"
CONTAINER_HOME = f"/home/{CONTAINER_USER}"
WORKSPACE_BASE = "/workspace"

# Host-config dirs staged RO to /tmp/host-config/<rel> in every container.
# Allowlist only -- avoid leaking gh/aws/gcloud/etc creds.
# Entrypoint symlinks each into $HOME so Docker never creates $HOME subdirs as root.
# Nvim is devcontainer-only and lives in cli.py via the RO+copy pattern.
_SHARED_RO_CONFIG_DIRS = (".config/anthropic", ".config/claude", ".config/jj")

_RED = "\033[0;31m"
_GREEN = "\033[0;32m"
_YELLOW = "\033[1;33m"
_NC = "\033[0m"


def log_info(msg: str) -> None:
    print(f"{_GREEN}[INFO]{_NC} {msg}")


def log_warn(msg: str) -> None:
    print(f"{_YELLOW}[WARN]{_NC} {msg}")


def log_error(msg: str) -> None:
    print(f"{_RED}[ERROR]{_NC} {msg}", file=sys.stderr)


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the VCS repository root (jj or git) by walking up from *start*.

    Delegates to the VCS auto-detection layer. Exits on failure.
    """
    try:
        backend = get_backend(start)
        return backend.repo_root
    except RuntimeError as e:
        log_error(str(e))
        sys.exit(1)


def build_session_name(prefix: str, suffix: str = "") -> str:
    """Generate a session name like ``prefix_suffix`` or ``prefix_<random>``."""
    return f"{prefix}_{suffix or secrets.token_hex(3)}"


def require_docker() -> None:
    """Verify the ``docker`` CLI is available, exit otherwise."""
    if not shutil.which("docker"):
        log_error("Docker is not installed.")
        sys.exit(1)


def ensure_image(
    image: str,
    dockerfile: Path,
    context: Path,
    *,
    parent_image: tuple[str, Path, Path] | None = None,
    force: bool = False,
    no_cache: bool = False,
) -> None:
    """Build a Docker image if it does not already exist locally.

    Pass parent_image=(name, dockerfile, context) to ensure a base image is built first.
    Pass force=True to build even if the image already exists.
    Pass no_cache=True to build with --no-cache.
    """
    result = subprocess.run(
        ["docker", "images", "-q", image], capture_output=True, text=True,
    )
    if result.stdout.strip() and not force:
        return
    if parent_image:
        parent_name, parent_dockerfile, parent_context = parent_image
        parent_result = subprocess.run(
            ["docker", "images", "-q", parent_name], capture_output=True, text=True,
        )
        if not parent_result.stdout.strip():
            ensure_image(parent_name, parent_dockerfile, parent_context, force=force, no_cache=no_cache)
    if result.stdout.strip():
        log_info(f"Rebuilding '{image}' (this may take 5+ minutes)...")
    else:
        log_info(f"Image '{image}' not found. Building (this may take 5+ minutes on first run)...")
    cmd = ["docker", "build", "-f", str(dockerfile), "-t", image]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(context))
    subprocess.run(cmd, check=True)
    log_info("Image built successfully.")


def cld_tmpdir(repo_root: Path) -> Path:
    """Return the per-repo temp directory for cld scratch files (creates if missing)."""
    d = repo_root / ".cld"
    d.mkdir(exist_ok=True)
    return d


def to_host_path(path: str, cfg: Config) -> str:
    """Translate a container-internal path to the corresponding host path.

    Uses ``cfg.host_project_dir`` / ``cfg.host_home`` (populated from the
    ``CLD_HOST_PROJECT_DIR`` / ``CLD_HOST_HOME`` env vars set by the host
    launcher when running inside a container) to map ``/workspace/*`` and
    ``$HOME`` paths back to their host-side locations. No-op on the host.
    """
    if cfg.host_project_dir:
        for prefix in ("/workspace/current", "/workspace/origin"):
            if path.startswith(prefix):
                path = cfg.host_project_dir + path[len(prefix):]
                break
    if cfg.host_home and path.startswith(CONTAINER_HOME):
        path = cfg.host_home + path[len(CONTAINER_HOME):]
    return path


def build_container_args(
    repo_root: Path,
    session_name: str,
    cfg: Config,
    *,
    interactive: bool = False,
) -> list[str]:
    """Build the base ``docker run`` argument list every launcher needs.

    Sets up security constraints, volume mounts (repo, claude config,
    docker socket, mysql), and environment variables. Devcontainer-only
    mounts (gitconfig, bashrc, nvim) are added by the launcher in cli.py.
    """
    home = os.path.expanduser("~")
    host_home = to_host_path(home, cfg)
    host_repo_root = to_host_path(str(repo_root), cfg)

    args: list[str] = []

    if interactive:
        args += ["-it"]

    # Security and resources
    args += [
        "--rm",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=2.0",
        "--memory=4g",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", f"HOME={CONTAINER_HOME}",
    ]

    # Workspace (required)
    ssl_certs = Path("/etc/ssl/certs")
    if not ssl_certs.is_dir():
        log_error("/etc/ssl/certs not found -- HTTPS/API calls will fail")
        sys.exit(1)
    args += [
        "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
        "-v", f"{host_repo_root}:{WORKSPACE_BASE}/origin",
        "-w", f"{WORKSPACE_BASE}/current",
        "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt",
    ]

    # Claude session state (required)
    # rw needed for OAuth token refresh and session state writes; tradeoff: agent can both
    # read OAuth tokens and overwrite session state. Consider ro + tmpfs overlay in the future.
    local_claude_dir = Path(home) / ".claude"
    if not local_claude_dir.is_dir():
        log_error(f"{local_claude_dir} not found -- Claude auth and session state unavailable")
        sys.exit(1)
    args += ["-v", f"{host_home}/.claude:{CONTAINER_HOME}/.claude:rw"]

    # Claude config (optional -- host MCP servers won't be available without it)
    local_claude_json = Path(home) / ".claude.json"
    if local_claude_json.is_file():
        args += ["-v", f"{host_home}/.claude.json:/tmp/host-claude.json:ro"]
    else:
        log_warn(f"{local_claude_json} not found -- host MCP servers won't be available in container")

    staged_configs = []
    for config_rel in _SHARED_RO_CONFIG_DIRS:
        local_config_path = Path(home) / config_rel
        if local_config_path.is_dir():
            args += ["-v", f"{host_home}/{config_rel}:/tmp/host-config/{config_rel}:ro"]
            staged_configs.append(config_rel)
        else:
            log_warn(f"{local_config_path} not found -- skipping")
    if staged_configs:
        args += ["-e", f"HOST_CONFIG_DIRS={':'.join(staged_configs)}"]

    # Session
    args += ["-e", f"SESSION_NAME={session_name}"]
    log_info(f"Session name: {session_name}")

    # Docker socket (conditional)
    docker_sock = Path("/var/run/docker.sock")
    if docker_sock.is_socket():
        docker_gid = docker_sock.stat().st_gid
        args += [
            "-v", f"{docker_sock}:{docker_sock}",
            "--group-add", str(docker_gid),
            "-e", f"CLD_HOST_PROJECT_DIR={repo_root}",
            "-e", f"CLD_HOST_HOME={home}",
        ]
        log_info("Docker socket mounted (orchestrator support)")
    else:
        log_warn("Docker socket not found, orchestrator agent lifecycle tools unavailable")

    # MySQL (conditional)
    if cfg.mysql_config:
        mysql_path = Path(cfg.mysql_config)
        if mysql_path.is_file():
            resolved = str(mysql_path.resolve())
            args += [
                "-v", f"{resolved}:/run/secrets/mysql.cnf:ro",
                "-e", "MYSQL_DEFAULTS_FILE=/run/secrets/mysql.cnf",
            ]
            log_info(f"MySQL config mounted from: {resolved}")
        else:
            log_warn(f"CLD_MYSQL_CONFIG set but file not found: {cfg.mysql_config}")

    return args


def mount_home_path(rel_path: str, target: str, cfg: Config) -> list[str]:
    """Mount $HOME/rel_path to target. Returns empty list if source doesn't exist."""
    local_path = Path.home() / rel_path
    if not local_path.exists():
        return []
    host_path = to_host_path(str(local_path.resolve()), cfg)
    return ["-v", f"{host_path}:{target}"]
