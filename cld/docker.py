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
MAILBOX_MOUNT = "/var/cld/mailboxes"

# All RO $HOME mounts are staged under /tmp/host-config/<rel> and copied into
# $HOME by the entrypoint (see copy_host_configs in container-init.sh).
# Allowlist only -- avoid leaking gh/aws/gcloud/etc creds.
_RO_HOME_MOUNT_ROOT = "/tmp/host-config"

# Whitelisted test secrets land here (RO), never the raw .env. See stage_test_env.
_TEST_ENV_MOUNT = "/run/secrets/test.env"

# Host test-broker key + pinned known_hosts land here (RO). See stage_host_broker.
_HOST_BROKER_KEY_MOUNT = "/run/secrets/host-broker-key"
_HOST_BROKER_KNOWN_HOSTS_MOUNT = "/run/secrets/host-broker-known"


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


def run_extra_paths(cld_root: Path) -> list[Path]:
    return [
        cld_root / "imgs/claude-run/entrypoint-claude-run.sh",
        cld_root / "imgs/claude-run/run-system-prompt.md",
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
    MCP stdio servers where stdout = JSON-RPC and must stay clean.
    Returns the content hash of the (now-current) image.
    """
    if in_master_container():
        # Inside a master container we share the host's docker daemon (so its
        # images are already visible) but have no build context -- imgs/ isn't
        # baked into /opt/cld -- and the host owns image building. Never attempt
        # a rebuild here; trust the host-built image, or fail clearly if the
        # host never built it.
        exists = bool(subprocess.run(
            ["docker", "images", "-q", image], capture_output=True, text=True,
        ).stdout.strip())
        if not exists:
            raise RuntimeError(
                f"image '{image}' not found and cannot be built from inside a "
                "master container. Build it on the host first with `cld build`."
            )
        log.debug("inside master; trusting host-built image %s (no rebuild)", image)
        return ""

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


def in_master_container() -> bool:
    """True when the current process is running inside a `cld master` container."""
    return bool(os.environ.get("MASTER_MODE"))


def find_target_repo(cfg: Config) -> Path:
    """Return the host path of the repo to target for a peer launch.

    On the host, delegates to `find_repo_context()` (cwd-walk for .jj/.git).
    Inside master, uses `resolve_master_target(Path.cwd(), cfg)` -- either
    master's own repo (host path from CLD_HOST_PROJECT_DIR) or one of the
    registered `master_targets` entries.
    """
    if in_master_container():
        return Path(resolve_master_target(Path.cwd(), cfg))
    return find_repo_context()[0]


def anchor_env_args(cfg: Config, session: str, revision: str) -> list[str]:
    """Return the docker `-e` args carrying anchor info to a peer container.

    Uniform for host and delegated (inside-master) launches: the host resolves
    the revision to a commit hash when it has a jj view, or leaves it symbolic
    when running inside master (which has no RW view of the target repo). The
    peer entrypoint uses this as the base for ``jj workspace add`` and then
    creates the anchor commit B inside that workspace via ``stage_from_env``.
    See docs/design-anchor-change.md.
    """
    from cld.vcs import get_backend
    from cld.vcs.anchor import resolve_anchor
    from cld.vcs.scratch import encode_scratch_envelope

    scratch = {"session": f"{session}\n".encode()}
    payload = encode_scratch_envelope(scratch)

    if in_master_container():
        hint = revision
    else:
        hint = resolve_anchor(get_backend(), revision)
        log.info("Anchor base: %s", hint[:12])

    return [
        "-e", f"AGENT_REVISION_HINT={hint}",
        "-e", f"AGENT_SCRATCH={payload}",
    ]


def resolve_master_target(cwd: Path, cfg: Config) -> str:
    """From inside a master container, return the host path of the target repo
    selected by *cwd*.

    Resolution:
    - cwd (or ancestor) under ``/workspace/current`` or ``/workspace/origin``
      → master's own repo, host path from ``CLD_HOST_PROJECT_DIR``.
    - cwd (or ancestor) matches an entry in ``MASTER_TARGETS`` (colon-separated)
      → that entry.
    - Otherwise → RuntimeError with a hint about adding the path to
      ``master_targets`` in cld config.

    Only meaningful inside a master container; raises RuntimeError elsewhere.
    """
    if not in_master_container():
        raise RuntimeError("resolve_master_target: not running inside a cld master container")
    cwd = cwd.resolve()

    def _is_within(child: Path, parent: str) -> bool:
        parent_p = Path(parent)
        try:
            child.relative_to(parent_p)
            return True
        except ValueError:
            return False

    if _is_within(cwd, "/workspace/current") or _is_within(cwd, "/workspace/origin"):
        if not cfg.host_project_dir:
            raise RuntimeError(
                "resolve_master_target: cwd is under /workspace/* but CLD_HOST_PROJECT_DIR "
                "is unset -- master container was launched without host-path plumbing"
            )
        return cfg.host_project_dir

    targets = [t for t in os.environ.get("MASTER_TARGETS", "").split(":") if t]
    # Placeholder dirs live at the container mirror of each host target (the
    # devcontainer entrypoint swaps the host-home prefix for $HOME). Translate
    # cwd back to its host path before matching so a cwd under the mirror
    # resolves to the registered host target.
    cwd_host = Path(to_host_path(str(cwd), cfg))
    for entry in targets:
        if _is_within(cwd_host, entry):
            return entry

    raise RuntimeError(
        f"cwd {cwd} is not a registered target in this master. "
        "Add its host path to `master_targets` in cld config and restart master."
    )


def to_host_path(path: str, cfg: Config) -> str:
    """Translate a container-internal path to the corresponding host path.

    Uses ``cfg.host_project_dir`` / ``cfg.host_home`` (populated from the
    ``CLD_HOST_PROJECT_DIR`` / ``CLD_HOST_HOME`` env vars set by the host
    launcher when running inside a container) to map ``/workspace/*`` and
    ``$HOME`` paths back to their host-side locations. No-op on the host.
    """
    if cfg.host_project_dir:
        # /workspace/current is container-ephemeral (no host equivalent under
        # the new layout); only /workspace/origin maps back to the host repo.
        prefix = "/workspace/origin"
        if path.startswith(prefix):
            path = cfg.host_project_dir + path[len(prefix):]
    if cfg.host_home and path.startswith(CONTAINER_HOME):
        path = cfg.host_home + path[len(CONTAINER_HOME):]
    return path


def build_container_args(
    repo_root: Path,
    session_name: str,
    cfg: Config,
    *,
    interactive: bool = False,
    master: bool = False,
    agent: bool = False,
) -> list[str]:
    """Build the base ``docker run`` argument list every launcher needs.

    Sets up security constraints, volume mounts (repo, claude config,
    docker socket, mysql), and environment variables. Devcontainer-only
    mounts (gitconfig, bashrc, nvim) are added by the launcher in cli.py.

    ``master`` and ``agent`` are mutually exclusive persistent-container
    roles (the latter is the headless messaging agent, unrelated to the
    one-shot `cld agent` command); either one adds the ``org.cld.kind``
    label set and mounts the shared mailbox tree.
    """
    if master and agent:
        raise ValueError("master and agent are mutually exclusive roles")

    home = os.path.expanduser("~")
    host_home = to_host_path(home, cfg)
    host_repo_root = to_host_path(str(repo_root), cfg)

    args: list[str] = []

    if interactive:
        args += ["-it"]

    if master or agent:
        kind = "master" if master else "agent"
        args += [
            "--name", session_name,
            "--label", f"org.cld.kind={kind}",
            "--label", f"org.cld.repo-root={host_repo_root}",
            "--label", f"org.cld.session={session_name}",
            "-e", f"{'MASTER_MODE' if master else 'AGENT_MODE'}=1",
        ]
    else:
        args += ["--rm"]

    # Security and resources
    args += [
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

    # SSL CA certificates: internal (Seznam) roots are baked into the base image
    # trust store, so no mount is needed by default. `cfg.ssl_certs_path` is an
    # explicit escape hatch that shadows the baked bundle with a host-supplied
    # dir or PEM file -- opt in only, and it *replaces* rather than merges.
    if cfg.ssl_certs_path:
        ssl_path = Path(cfg.ssl_certs_path)
        log.info("SSL: replacing baked CA bundle with %s (opt-in via ssl_certs_path)", ssl_path)
        if ssl_path.is_dir():
            args += ["-v", f"{ssl_path}:/etc/ssl/certs:ro",
                     "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"]
        else:
            args += ["-v", f"{ssl_path}:/etc/ssl/cert.pem:ro",
                     "-e", "SSL_CERT_FILE=/etc/ssl/cert.pem",
                     "-e", "REQUESTS_CA_BUNDLE=/etc/ssl/cert.pem",
                     "-e", "NODE_EXTRA_CA_CERTS=/etc/ssl/cert.pem"]

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

    # Workspace setup: gitignored files to symlink into workspace
    if cfg.ignore_gitignore:
        workspace_files = ":".join(cfg.ignore_gitignore)
        args += ["-e", f"WORKSPACE_FILES={workspace_files}"]
        log.debug(f"Workspace files to link: {workspace_files}")

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
        log.info("Docker socket mounted (messenger list_agents via docker ps)")
    else:
        log.warning("Docker socket not found, messenger list_agents will be unavailable")

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

    # Test-env secrets (conditional): whitelisted keys extracted host-side from
    # <cfg.pyproject_dir>/.env into a derived container-private file. The raw
    # .env is never mounted. No-op unless test_env_keys is set.
    args += stage_test_env(repo_root, cfg)

    # Host test broker (master only): mount the restricted key + known_hosts and
    # install the host-run wrapper. No-op unless host_broker_key is set.
    if master:
        args += stage_host_broker(cfg)

    # Mailbox tree (master/agent persistent roles only) -- shared RW mount so
    # every master and agent container sees the same mailbox filesystem.
    if master or agent:
        mailbox_root = Path(cfg.mailbox_root).expanduser()
        host_mailbox_root = to_host_path(str(mailbox_root), cfg)
        if host_mailbox_root == str(mailbox_root):
            # Bare host: our own filesystem view already *is* the host view.
            mailbox_root.mkdir(parents=True, exist_ok=True)
        else:
            # Nested (cld running inside another container, sibling-container
            # pattern): the real host path isn't in our filesystem view.
            # Deliberately do NOT reach across the docker socket to create it
            # there -- container isolation from the host is a hard
            # requirement even though the socket happens to be shared for
            # launching sibling containers. If the path doesn't already exist
            # on the real host, `docker run` will auto-create it as root and
            # the non-root container user won't be able to write into it.
            log.warning(
                "Mailbox root %s is outside this process's filesystem view "
                "(nested cld). If it doesn't already exist on the real host, "
                "create it there manually before continuing: "
                "mkdir -p %s && chown %d:%d %s",
                host_mailbox_root, host_mailbox_root, os.getuid(), os.getgid(), host_mailbox_root,
            )
        args += ["-v", f"{host_mailbox_root}:{MAILBOX_MOUNT}:rw"]
        log.info("Mailbox mounted: %s -> %s", host_mailbox_root, MAILBOX_MOUNT)

    # Master-only: publish the registered sibling target paths as an env var
    # so master's entrypoint can materialize them as empty placeholder
    # directories (see docs/design-master-sibling-launch.md). Master gets no
    # bind mount of a sibling repo -- the placeholder just lets `cd <path>`
    # succeed and lets cld-inside-master resolve cwd to the host path.
    # Host paths must exist on the host so the peer's -v mount will succeed
    # later; we fail fast here rather than at peer-launch time.
    if master and cfg.master_targets:
        expanded_targets: list[str] = []
        for entry in cfg.master_targets:
            expanded = os.path.expanduser(entry)
            if not Path(expanded).exists():
                log.error(
                    "master_targets entry does not exist on host: %s "
                    "(expanded from %r). Remove it or create the directory.",
                    expanded, entry,
                )
                sys.exit(1)
            # Peer placeholders are mirrored under the container $HOME (the only
            # writable root), so a target must live under the host home dir.
            if expanded != home and not expanded.startswith(home + os.sep):
                log.error(
                    "master_targets entry is not under your home directory: %s "
                    "(expanded from %r). Placeholders can only be mirrored under "
                    "%s inside master; move the repo under your home dir or "
                    "launch it directly (not via master).",
                    expanded, entry, home,
                )
                sys.exit(1)
            expanded_targets.append(expanded)
            log.info("Master target registered: %s", expanded)
        args += ["-e", "MASTER_TARGETS=" + ":".join(expanded_targets)]

    log.debug("Container args: %s", mask_secrets(repr(args)))
    return args


def master_container_name(repo_root: Path) -> str:
    """Deterministic container name for the master devcontainer of *repo_root*."""
    sha = hashlib.sha1(str(repo_root).encode()).hexdigest()[:8]
    return f"cld_master_{repo_root.name}_{sha}"


def agent_container_name(repo_root: Path) -> str:
    """Deterministic container name for the repo agent of *repo_root*.

    Unlike ``master_container_name`` this skips the sha8 disambiguator: at
    most one agent per repo basename may run host-wide (see design doc Q4).
    """
    return f"cld_agent_{repo_root.name}"


def _docker_status(name: str) -> str:
    """Return 'running', 'stopped', or 'absent' for the named container."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True, text=True,
    )
    log_subprocess(log, ["docker", "inspect", name], result)
    if result.returncode != 0:
        return "absent"
    return "running" if result.stdout.strip() == "running" else "stopped"


def docker_master_status(name: str) -> str:
    return _docker_status(name)


def docker_agent_status(name: str) -> str:
    return _docker_status(name)


def _docker_kind_list(kind: str) -> list[dict]:
    """Return all containers of *kind* ('master' or 'agent') with their org.cld.* labels."""
    label_filter = f"label=org.cld.kind={kind}"
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", label_filter, "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    log_subprocess(log, ["docker", "ps", "-a", "--filter", label_filter], result)
    if result.returncode != 0:
        return []
    containers: list[dict] = []
    for name in result.stdout.strip().splitlines():
        if not name:
            continue
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format",
             '{{index .Labels "org.cld.repo-root"}}|{{index .Labels "org.cld.session"}}'],
            capture_output=True, text=True,
        )
        log_subprocess(log, ["docker", "inspect", name], inspect)
        if inspect.returncode != 0:
            continue
        parts = inspect.stdout.strip().split("|", 1)
        containers.append({
            "name": name,
            "repo_root": parts[0] if parts else "",
            "session": parts[1] if len(parts) > 1 else "",
        })
    return containers


def docker_master_list() -> list[dict]:
    """Return all master containers with their org.cld.* labels."""
    return _docker_kind_list("master")


def docker_agent_list() -> list[dict]:
    """Return all repo agent containers with their org.cld.* labels."""
    return _docker_kind_list("agent")


_CONTAINER_SSH_AUTH_SOCK = "/run/host-ssh-agent.sock"


def stage_ssh_agent(cfg: Config) -> list[str]:
    """Return -v/-e args to forward the host ssh-agent socket into a devcontainer.

    Tri-state on ``cfg.ssh_auth_sock``:
      - ``""`` (explicit) -> disabled, return [].
      - ``None`` (unset)  -> auto-detect from $SSH_AUTH_SOCK.
      - path              -> use that host socket path.
    Never fatal: on any resolution or validation failure, warn and skip.
    """
    if cfg.ssh_auth_sock == "":
        log.debug("SSH agent forward: explicitly disabled via ssh_auth_sock=''")
        return []
    sock = cfg.ssh_auth_sock or os.environ.get("SSH_AUTH_SOCK", "")
    if not sock:
        log.debug("SSH agent forward: no SSH_AUTH_SOCK available -- skipping")
        return []
    if not Path(sock).is_socket():
        log.warning("SSH_AUTH_SOCK=%s is not a socket -- skipping agent forward", sock)
        return []
    host_sock = to_host_path(sock, cfg)
    log.info("SSH agent socket forwarded: %s -> %s", sock, _CONTAINER_SSH_AUTH_SOCK)
    return [
        "-v", f"{host_sock}:{_CONTAINER_SSH_AUTH_SOCK}",
        "-e", f"SSH_AUTH_SOCK={_CONTAINER_SSH_AUTH_SOCK}",
    ]


def stage_host_broker(cfg: Config) -> list[str]:
    """Return docker args wiring a master container to the host test broker.

    Mounts the restricted broker private key (and, if given, the pinned
    known_hosts) RO, adds a host-gateway alias so the container can reach the
    host-side sshd, and sets ``CLD_HOST_BROKER`` so ``container-init.sh`` installs
    the ``host-run`` wrapper. No-op unless ``cfg.host_broker_key`` is set. The
    broker only accepts ``cld_master_*`` sessions, so this is master-only.
    See docs/design-host-test-running.md.
    """
    if not cfg.host_broker_key:
        return []
    key = Path(cfg.host_broker_key).expanduser()
    if not key.is_file():
        log.warning("host_broker_key set but not found: %s", key)
        return []
    host_key = to_host_path(str(key.resolve()), cfg)
    args = [
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{host_key}:{_HOST_BROKER_KEY_MOUNT}:ro",
        "-e", f"CLD_HOST_BROKER={cfg.host_broker_endpoint}",
    ]
    if cfg.host_broker_known_hosts:
        known = Path(cfg.host_broker_known_hosts).expanduser()
        if known.is_file():
            host_known = to_host_path(str(known.resolve()), cfg)
            args += ["-v", f"{host_known}:{_HOST_BROKER_KNOWN_HOSTS_MOUNT}:ro"]
        else:
            log.warning("host_broker_known_hosts set but not found: %s", known)
    else:
        log.warning(
            "host_broker_key set but host_broker_known_hosts is empty -- "
            "host-run's strict host-key check will fail without a pinned known_hosts"
        )
    log.info("Host test broker wired: key -> %s, endpoint %s", _HOST_BROKER_KEY_MOUNT, cfg.host_broker_endpoint)
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


def _shell_single_quote(value: str) -> str:
    """Wrap *value* in single quotes so a POSIX shell sources it verbatim."""
    return "'" + value.replace("'", "'\\''") + "'"


def _filter_env(src: Path, keys: Iterable[str]) -> dict[str, str]:
    """Return the ``KEY=value`` pairs of *src* whose key is in *keys*.

    Parsing mirrors ``config._load_dotenv`` (split on first ``=``, strip
    surrounding whitespace, skip blanks/comments) plus an optional ``export``
    prefix.
    """
    allow = set(keys)
    out: dict[str, str] = {}
    for line in src.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key in allow:
            out[key] = value.strip()
    return out


def _write_derived_test_env(repo_root: Path, values: dict[str, str]) -> Path:
    """Write *values* to a 0600 shell-sourceable file under ~/.cld/test-env.

    One file per repo (overwritten each launch) so orphans stay bounded.
    """
    dest_dir = Path.home() / ".cld" / "test-env"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_dir.chmod(0o700)
    sha = hashlib.sha1(str(repo_root).encode()).hexdigest()[:8]
    dest = dest_dir / f"{repo_root.name}_{sha}.env"
    body = "".join(f"{k}={_shell_single_quote(v)}\n" for k, v in values.items())
    dest.write_text(body)
    dest.chmod(0o600)
    return dest


def stage_test_env(repo_root: Path, cfg: Config) -> list[str]:
    """Return -v/-e args exposing whitelisted test secrets to the container.

    Reads ``<repo_root>/<cfg.pyproject_dir>/.env``, keeps only the keys in
    ``cfg.test_env_keys``, and writes them to a derived 0600 file mounted RO
    at ``/run/secrets/test.env`` with ``TEST_ENV_FILE`` pointing at it. The
    container's ``cldtest`` wrapper sources it into the test subprocess only.
    The raw ``.env`` is never mounted -- unlisted keys never enter the
    container at all. No-op unless ``test_env_keys`` is set.
    """
    if not cfg.test_env_keys:
        return []
    src = repo_root / cfg.pyproject_dir / ".env"
    if not src.is_file():
        log.warning("test_env_keys set but %s not found", src)
        return []
    selected = _filter_env(src, cfg.test_env_keys)
    missing = [k for k in cfg.test_env_keys if k not in selected]
    if missing:
        log.warning("test_env_keys absent from %s: %s", src.name, ", ".join(missing))
    if not selected:
        log.warning("no test_env_keys present in %s -- skipping test-env injection", src)
        return []
    derived = _write_derived_test_env(repo_root, selected)
    log.info("Test-env: injecting %d key(s) into %s", len(selected), _TEST_ENV_MOUNT)
    return [
        "-v", f"{derived}:{_TEST_ENV_MOUNT}:ro",
        "-e", f"TEST_ENV_FILE={_TEST_ENV_MOUNT}",
    ]
