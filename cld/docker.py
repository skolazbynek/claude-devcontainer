"""Container setup: arg building, image management, path translation."""

import hashlib
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from cld.config import Config
from cld.log import get_logger, log_subprocess, mask_secrets
from cld.vcs import get_backend

log = get_logger(__name__)

# Static structural constants (Dockerfile- and shell-script-coupled, not user-tunable).
CONTAINER_USER = "claude"
CONTAINER_HOME = f"/home/{CONTAINER_USER}"
WORKSPACE_BASE = "/workspace"

# All RO $HOME mounts are staged under /tmp/host-config/<rel> and copied into
# $HOME by the entrypoint (see copy_host_configs in container-init.sh).
# Allowlist only -- avoid leaking gh/aws/gcloud/etc creds.
_RO_HOME_MOUNT_ROOT = "/tmp/host-config"


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the VCS repository root (jj or git) by walking up from *start*.

    Delegates to the VCS auto-detection layer. Exits on failure.
    """
    try:
        backend = get_backend(start)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)
    return backend.repo_root


def find_repo_context(start: Path | None = None) -> tuple[Path, str]:
    """Return (repo_root, workspace_revision_hint).

    workspace_revision_hint is non-empty when invoked from a secondary jj workspace
    or git worktree, set to the appropriate revision so the container starts from
    the caller's current working copy rather than the main workspace default.
    """
    try:
        backend = get_backend(start)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)
    log.info("Repository: %s (VCS: %s)", backend.repo_root, backend.name)
    return backend.repo_root, backend.workspace_revision


def build_session_name(prefix: str, suffix: str = "") -> str:
    """Generate a session name like ``prefix_suffix`` or ``prefix_<random>``."""
    return f"{prefix}_{suffix or secrets.token_hex(3)}"


def require_docker() -> None:
    """Verify the ``docker`` CLI is available, exit otherwise."""
    if not shutil.which("docker"):
        log.error("Docker is not installed.")
        sys.exit(1)


CONTENT_HASH_LABEL = "org.cld.content-hash"

_HASH_IGNORE_PARTS = {"__pycache__", ".git", ".jj", ".venv", "node_modules"}


def _hash_ignored(p: Path) -> bool:
    return any(part in _HASH_IGNORE_PARTS or part.endswith(".pyc") for part in p.parts)


def _hash_walk(p: Path) -> Iterable[Path]:
    if p.is_file():
        yield p
        return
    for entry in sorted(p.rglob("*")):
        if entry.is_file() and not _hash_ignored(entry):
            yield entry


def _content_hash(paths: list[Path], parent_hash: str | None) -> str:
    """Deterministic content hash over the given files/dirs and an optional parent hash."""
    h = hashlib.sha256()
    if parent_hash:
        h.update(b"parent:" + parent_hash.encode() + b"\n")
    for p in sorted(paths):
        # Use relpath under p.parent so the path component is stable across machines.
        for entry in _hash_walk(p):
            rel = entry.relative_to(p.parent).as_posix()
            h.update(f"{rel}\0".encode())
            h.update(entry.read_bytes())
            h.update(b"\0")
    hexdigest_short = h.hexdigest()[:16]
    log.debug(
        "computed content hash %s (parent=%s)",
        hexdigest_short,
        parent_hash[:8] if parent_hash else "<root>",
    )
    return hexdigest_short


def _image_label(image: str, label: str) -> str:
    """Read a Docker label off an image. Empty string if image or label is missing."""
    cmd = ["docker", "inspect", "--format", f'{{{{ index .Config.Labels "{label}" }}}}', image]
    result = subprocess.run(cmd, capture_output=True, text=True)
    log_subprocess(log, cmd, result)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def base_extra_paths(cld_root: Path) -> list[Path]:
    return [
        cld_root / "imgs/claude-devcontainer/container-init.sh",
        cld_root / "imgs/claude-devcontainer/vcs-lib.sh",
        cld_root / "cld",
        cld_root / "prompts",
    ]


def devcontainer_extra_paths(cld_root: Path) -> list[Path]:
    return [cld_root / "imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh"]


def agent_extra_paths(cld_root: Path) -> list[Path]:
    return [
        cld_root / "imgs/claude-agent/entrypoint-claude-agent.sh",
        cld_root / "imgs/claude-agent/agent-system-prompt.md",
    ]


def ensure_image(
    image: str,
    dockerfile: Path,
    context: Path,
    *,
    extra_paths: list[Path] | None = None,
    parent_image: tuple[str, Path, Path, list[Path]] | None = None,
    force: bool = False,
    no_cache: bool = False,
    quiet: bool = False,
) -> str:
    """Build a Docker image if it's missing or its baked content has drifted from source.

    Stamps every build with a `CONTENT_HASH_LABEL` Docker label whose value hashes the
    Dockerfile + every path in `extra_paths` (recursively, sorted, ignoring caches/VCS).
    Rebuilds when the existing image's label doesn't match the recomputed hash.

    Pass parent_image=(name, dockerfile, context, extra_paths) to ensure a base image
    is built first; the parent's hash is folded into this image's hash so a base
    rebuild propagates.
    Pass force=True to always build. Pass no_cache=True to build with --no-cache.
    Pass quiet=True to capture docker build output (logged at INFO line-by-line)
    instead of streaming to the inherited stdout. Required when running under
    MCP stdio (orchestrator) where stdout = JSON-RPC and must stay clean.
    Returns the content hash of the (now-current) image.
    """
    parent_hash: str | None = None
    if parent_image:
        parent_name, parent_dockerfile, parent_context, parent_extras = parent_image
        parent_hash = ensure_image(
            parent_name, parent_dockerfile, parent_context,
            extra_paths=parent_extras, force=force, no_cache=no_cache, quiet=quiet,
        )

    expected = _content_hash([dockerfile] + (extra_paths or []), parent_hash)

    exists = bool(subprocess.run(
        ["docker", "images", "-q", image], capture_output=True, text=True,
    ).stdout.strip())
    existing = _image_label(image, CONTENT_HASH_LABEL) if exists else ""

    if exists and not force and existing == expected:
        return expected

    log.debug(
        "Hash check: existing=%s expected=%s",
        existing[:8] if existing else "<missing>",
        expected[:8],
    )
    if force:
        log.info(f"Rebuilding '{image}' (forced, hash {expected[:8]})...")
    elif not exists:
        log.info(f"Image '{image}' not found. Building (hash {expected[:8]}, may take 5+ minutes)...")
    elif not existing:
        log.info(f"Rebuilding '{image}' (no content-hash label; hash {expected[:8]})...")
    else:
        log.info(f"Rebuilding '{image}' (stale: {existing[:8]} -> {expected[:8]})...")

    cmd = ["docker", "build", "-f", str(dockerfile), "-t", image,
           "--label", f"{CONTENT_HASH_LABEL}={expected}"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(context))
    log.info("Running docker build for %s (this may take several minutes)", image)
    if quiet:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log.info("docker build: %s", line.rstrip())
        rc = proc.wait()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
    else:
        subprocess.run(cmd, check=True)
    log.info("docker build for %s succeeded", image)
    return expected


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
    args += [
        "-v", f"{host_repo_root}:{WORKSPACE_BASE}/origin",
        "-w", f"{WORKSPACE_BASE}/current",
    ]

    # SSL CA certificates -- optional, container has its own bundle as fallback.
    _SSL_CANDIDATES = ["/etc/ssl/certs", "/etc/ssl/cert.pem"]
    if cfg.ssl_certs_path:
        log.debug("SSL: using configured ssl_certs_path=%s", cfg.ssl_certs_path)
        ssl_src: str | None = cfg.ssl_certs_path
    else:
        ssl_src = None
        for candidate in _SSL_CANDIDATES:
            present = Path(candidate).exists()
            log.debug("SSL candidate %s: present=%s", candidate, present)
            if present and ssl_src is None:
                ssl_src = candidate
    if ssl_src:
        ssl_path = Path(ssl_src)
        if ssl_path.is_dir():
            args += ["-v", f"{ssl_src}:/etc/ssl/certs:ro",
                     "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"]
        else:
            args += ["-v", f"{ssl_src}:/etc/ssl/cert.pem:ro",
                     "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/cert.pem"]
    else:
        log.warning("No host SSL CA bundle found -- container will use its own ca-certificates")

    # Claude session state (required)
    # rw needed for OAuth token refresh and session state writes; tradeoff: agent can both
    # read OAuth tokens and overwrite session state. Consider ro + tmpfs overlay in the future.
    local_claude_dir = Path(home) / ".claude"
    if not local_claude_dir.is_dir():
        log.error(f"{local_claude_dir} not found -- Claude auth and session state unavailable")
        sys.exit(1)
    args += ["-v", f"{host_home}/.claude:{CONTAINER_HOME}/.claude:rw"]

    # RO $HOME mounts: all staged under /tmp/host-config/<rel>, then copied
    # into $HOME by the entrypoint. Devcontainer-only entries are added by cli.py.
    for rel in cfg.home_mounts_always:
        log.debug("home_mounts_always: attempting ~/%s", rel)
        mnt = stage_home_ro(rel, cfg)
        if mnt:
            args += mnt
        else:
            log.warning(f"~/{rel} not found -- skipping")

    # Session
    args += ["-e", f"SESSION_NAME={session_name}"]
    log.info(f"Session name: {session_name}")

    # Docker socket (conditional)
    docker_sock = Path("/var/run/docker.sock")
    sock_present = docker_sock.is_socket()
    log.debug("Docker socket probe: path=%s found=%s", docker_sock, sock_present)
    if sock_present:
        docker_gid = docker_sock.stat().st_gid
        args += [
            "-v", f"{docker_sock}:{docker_sock}",
            "--group-add", str(docker_gid),
            "-e", f"CLD_HOST_PROJECT_DIR={repo_root}",
            "-e", f"CLD_HOST_HOME={home}",
        ]
        log.info("Docker socket mounted (orchestrator support)")
    else:
        log.warning("Docker socket not found, orchestrator agent lifecycle tools unavailable")

    # MySQL (conditional)
    if cfg.mysql_config:
        mysql_path = Path(cfg.mysql_config)
        mysql_exists = mysql_path.is_file()
        log.debug("MySQL config probe: path=%s exists=%s", mysql_path, mysql_exists)
        if mysql_exists:
            resolved = str(mysql_path.resolve())
            args += [
                "-v", f"{resolved}:/run/secrets/mysql.cnf:ro",
                "-e", "MYSQL_DEFAULTS_FILE=/run/secrets/mysql.cnf",
            ]
            log.info(f"MySQL config mounted from: {resolved}")
        else:
            log.warning(f"CLD_MYSQL_CONFIG set but file not found: {cfg.mysql_config}")

    log.debug("Container args: %s", mask_secrets(repr(args)))
    return args


def stage_home_ro(rel_path: str, cfg: Config) -> list[str]:
    """Stage ``$HOME/<rel_path>`` RO under ``/tmp/host-config/<rel_path>``.

    Returns the ``["-v", ...]`` arg pair, or ``[]`` if the source doesn't exist.
    The entrypoint copies the staged tree into ``$HOME`` (see ``copy_host_configs``).
    """
    local_path = Path.home() / rel_path
    if not local_path.exists():
        return []
    host_path = to_host_path(str(local_path.resolve()), cfg)
    return ["-v", f"{host_path}:{_RO_HOME_MOUNT_ROOT}/{rel_path}:ro"]
