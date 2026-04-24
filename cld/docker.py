"""Container setup: arg building, image management, path translation."""

import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

from cld.vcs import get_backend

CONTAINER_USER = "claude"
CONTAINER_HOME = f"/home/{CONTAINER_USER}"
WORKSPACE_BASE = "/workspace"

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
    return f"{prefix}_{suffix or random.randint(10000, 99999)}"


def load_dotenv(path: Path | None = None) -> None:
    """Read a .env file and inject its variables into the current process environment."""
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


def require_docker() -> None:
    """Verify the ``docker`` CLI is available, exit otherwise."""
    if not shutil.which("docker"):
        log_error("Docker is not installed.")
        sys.exit(1)


def ensure_image(image: str, dockerfile: Path, context: Path) -> None:
    """Build a Docker image if it does not already exist locally."""
    result = subprocess.run(
        ["docker", "images", "-q", image], capture_output=True, text=True,
    )
    if result.stdout.strip():
        return
    log_info(f"Image '{image}' not found. Building...")
    subprocess.run(
        ["docker", "build", "-f", str(dockerfile), "-t", image, str(context)],
        check=True,
    )
    log_info("Image built successfully.")


def _to_host_path(path: str) -> str:
    """Translate a container-internal path to the corresponding host path.

    Uses HOST_PROJECT_DIR and HOST_HOME env vars set during container launch
    to map /workspace/* and $HOME paths back to their host-side locations.
    """
    host_project = os.environ.get("HOST_PROJECT_DIR", "")
    host_home = os.environ.get("HOST_HOME", "")
    if host_project:
        for prefix in ("/workspace/current", "/workspace/origin"):
            if path.startswith(prefix):
                path = host_project + path[len(prefix):]
                break
    if host_home:
        home = os.path.expanduser("~")
        if path.startswith(home):
            path = host_home + path[len(home):]
    return path


def build_container_args(
    repo_root: Path,
    session_name: str,
    *,
    interactive: bool = False,
) -> list[str]:
    """Build the base ``docker run`` argument list every launcher needs.

    Sets up security constraints, volume mounts (repo, claude config, neovim,
    docker socket, mysql), and environment variables.
    """
    home = os.path.expanduser("~")
    host_home = _to_host_path(home)
    host_repo_root = _to_host_path(str(repo_root))

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

    # OAuth tokens / config (optional)
    local_config_dir = Path(home) / ".config"
    if local_config_dir.is_dir():
        args += ["-v", f"{host_home}/.config:{CONTAINER_HOME}/.config:ro"]
    else:
        log_warn(f"{local_config_dir} not found -- OAuth tokens won't be available, Claude may require re-authentication")

    # Neovim data/state/cache (share plugins, mason, treesitter parsers with host)
    for rel in (".local/share/nvim", ".local/state/nvim", ".cache/nvim"):
        local_path = Path(home) / rel
        if local_path.is_dir():
            args += ["-v", f"{host_home}/{rel}:{CONTAINER_HOME}/{rel}:rw"]

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
            "-e", f"HOST_PROJECT_DIR={repo_root}",
            "-e", f"HOST_HOME={home}",
        ]
        log_info("Docker socket mounted (orchestrator support)")
    else:
        log_warn("Docker socket not found, orchestrator agent lifecycle tools unavailable")

    # MySQL (conditional)
    mysql_config = os.environ.get("MYSQL_CONFIG", "")
    if mysql_config:
        mysql_path = Path(mysql_config)
        if mysql_path.is_file():
            resolved = str(mysql_path.resolve())
            args += [
                "-v", f"{resolved}:/run/secrets/mysql.cnf:ro",
                "-e", "MYSQL_DEFAULTS_FILE=/run/secrets/mysql.cnf",
            ]
            log_info(f"MySQL config mounted from: {resolved}")
        else:
            log_warn(f"MYSQL_CONFIG set but file not found: {mysql_config}")

    return args


def mount_home_path(rel_path: str, target: str) -> list[str]:
    """Mount $HOME/rel_path to target. Returns empty list if source doesn't exist."""
    local_path = Path.home() / rel_path
    if not local_path.exists():
        return []
    host_path = _to_host_path(str(local_path.resolve()))
    return ["-v", f"{host_path}:{target}"]


def run_container(args: list[str], image: str, *, detach: bool = False) -> str:
    """Run docker container. Returns container ID when detached, empty string otherwise."""
    cmd = ["docker", "run"]
    if detach:
        cmd.append("--detach")
    cmd += args
    cmd.append(image)

    if detach:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log_error(f"Failed to start container: {result.stderr.strip()}")
            sys.exit(1)
        return result.stdout.strip()
    else:
        os.execvp("docker", cmd)
        return ""  # unreachable
