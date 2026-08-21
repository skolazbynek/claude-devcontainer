"""Container setup: arg building, image management, path translation."""

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
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

# Broker key + pinned known_hosts land here (RO). See stage_broker.
_BROKER_KEY_MOUNT = "/run/secrets/broker-key"
_BROKER_KNOWN_HOSTS_MOUNT = "/run/secrets/broker-known-hosts"


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
        # No docker daemon inside master (socket removed). Image building is the
        # host's job, and container launches from inside master are delegated to
        # the host broker (which runs host-side `cld`, ensuring images there), so
        # this should never be reached. Fail clearly if it somehow is.
        raise RuntimeError(
            f"image '{image}' cannot be ensured from inside a master container "
            "(no docker daemon). Container launches are delegated to the host "
            "broker; build images on the host with `cld build`."
        )

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
    """True when running inside a `cld master`, or the bare ephemeral devcontainer.

    The name predates the bare devcontainer getting hub capability (broker reach,
    sibling-target resolution, mailbox); it now checks ``HUB_MODE``, which is set
    for both, rather than ``MASTER_MODE`` alone, which stays master-only (it also
    drives entrypoint boot behavior -- idle sleep vs. dropping to bash -- that must
    NOT apply to the ephemeral devcontainer).
    """
    return bool(os.environ.get("HUB_MODE"))


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


def anchor_env_args(cfg: Config, session: str, revision: str, brief: str = "") -> list[str]:
    """Return the docker `-e` args carrying anchor info to a peer container.

    The host resolves the base revision to a commit hash from its jj view; the
    peer entrypoint uses it as the base for ``jj workspace add`` and then creates
    the anchor commit B inside that workspace via ``stage_from_env``. See
    docs/design-anchor-change.md. Only ever called host-side now: launches from
    inside master are delegated to the host broker (see cld/broker.py), so
    the container itself never builds a peer's args.
    """
    from cld.vcs import get_backend
    from cld.vcs.anchor import resolve_anchor
    from cld.vcs.scratch import encode_scratch_envelope

    scratch = {"session": f"{session}\n".encode()}
    # The composed brief rides in the same envelope, so it lands in anchor commit B as
    # .cld-run/brief.md: one channel instead of the old /config/{persona,task}.md mounts
    # plus AGENT_INLINE_PROMPT, and readable by the agent mid-task. See
    # docs/design-prompt-chaining.md §3.
    if brief:
        scratch["brief.md"] = brief.encode()
    payload = encode_scratch_envelope(scratch)

    hint = resolve_anchor(get_backend(), revision)
    log.info("Anchor base: %s", hint[:12])

    return [
        "-e", f"AGENT_REVISION_HINT={hint}",
        "-e", f"AGENT_SCRATCH={payload}",
    ]


def resolve_master_target(cwd: Path, cfg: Config) -> str:
    """From inside a master or bare-devcontainer "hub", return the host path of
    the target repo selected by *cwd*.

    Resolution:
    - cwd (or ancestor) under ``/workspace/current`` or ``/workspace/origin``
      → the hub's own repo, host path from ``CLD_HOST_PROJECT_DIR``.
    - cwd (or ancestor) matches an entry in ``MASTER_TARGETS`` (colon-separated)
      → that entry.
    - Otherwise → RuntimeError with a hint about adding the path to
      ``master_targets`` in cld config.

    Only meaningful inside a hub container (``in_master_container()``); raises
    RuntimeError elsewhere.
    """
    if not in_master_container():
        raise RuntimeError("resolve_master_target: not running inside a cld master or hub-capable devcontainer")
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


@dataclass(frozen=True)
class TaskAgentSpec:
    """Immutable spawn facts for one task-scoped agent (docs/design-task-agents.md).

    ``parent_master`` is empty when a human launched the agent directly on the
    host: it is the label a master's fleet operations key on, so attributing an
    unrequested agent to a master would hand it authority to reap one it never
    spawned. ``peers`` maps each allowed peer's full container name to that
    edge's hop budget (D28).
    """

    slug: str
    parent_master: str = ""
    deliverable_branch: str = ""
    peers: dict[str, int] = field(default_factory=dict)

    def peers_env(self) -> str:
        """Encode ``peers`` for the container: ``name:hops`` pairs, comma-separated.

        Comma rather than the repo's usual colon-separated list convention,
        because ``:`` is the name/budget delimiter inside each pair.
        """
        return ",".join(f"{name}:{hops}" for name, hops in sorted(self.peers.items()))


def parse_peers_env(value: str) -> dict[str, int]:
    """Inverse of ``TaskAgentSpec.peers_env`` -- read by the supervisor in the container."""
    peers: dict[str, int] = {}
    for segment in value.split(","):
        segment = segment.strip()
        if not segment:
            continue
        name, _, hops = segment.partition(":")
        if not name or not hops.isdigit():
            raise ValueError(f"malformed peer spec {segment!r}: expected '<name>:<hops>'")
        peers[name] = int(hops)
    return peers


def build_container_args(
    repo_root: Path,
    session_name: str,
    cfg: Config,
    *,
    interactive: bool = False,
    master: bool = False,
    agent: bool = False,
    task_agent: TaskAgentSpec | None = None,
) -> list[str]:
    """Build the base ``docker run`` argument list every launcher needs.

    Sets up security constraints, volume mounts (repo, claude config,
    mysql), and environment variables. No docker socket is mounted (see the
    note in the body). Devcontainer-only
    mounts (gitconfig, bashrc, nvim) are added by the launcher in cli.py.

    ``master``, ``agent`` and ``task_agent`` are mutually exclusive
    persistent-container roles (``agent`` is the headless messaging agent,
    unrelated to the one-shot `cld agent` command; ``task_agent`` is the
    task-scoped one); any of them adds the ``org.cld.kind`` label set and
    mounts the shared mailbox tree. When none of them is set and
    ``interactive`` is true (the bare ``cld`` devcontainer), the container
    still gets a name, ``org.cld.kind=devcontainer`` labels, the broker mount
    and the mailbox mount -- an ephemeral, single-user stand-in for
    `cld master` that can spawn and message its own fleet, just with no
    persistent bookmark/state to reattach to once it exits.
    """
    if sum((master, agent, bool(task_agent))) > 1:
        raise ValueError("master, agent and task_agent are mutually exclusive roles")

    home = os.path.expanduser("~")
    host_home = to_host_path(home, cfg)
    host_repo_root = to_host_path(str(repo_root), cfg)

    # The bare ephemeral devcontainer (`cld`, interactive, no persistent role):
    # a single-user, throwaway `cld master` in every capability that matters
    # (broker reach in particular) except that it never outlives the session.
    # It still needs a name + the org.cld.* labels so the broker can identify
    # it and resolve its repo root -- see broker/cld-broker.sh.
    bare_devcontainer = interactive and not (master or agent or task_agent)

    args: list[str] = []

    if interactive:
        args += ["-it"]

    if master or agent or task_agent:
        kind = "master" if master else "task-agent" if task_agent else "agent"
        args += [
            "--name", session_name,
            "--label", f"org.cld.kind={kind}",
            "--label", f"org.cld.repo-root={host_repo_root}",
            "--label", f"org.cld.session={session_name}",
            "-e", f"{'MASTER_MODE' if master else 'AGENT_MODE'}=1",
        ]
        if master:
            # HUB_MODE is the capability flag `in_master_container()` actually
            # checks (sibling-target resolution, broker dispatch): true for
            # master and, below, the bare devcontainer -- unlike MASTER_MODE,
            # which also drives entrypoint boot behavior (idle sleep vs bash)
            # and must stay master-only.
            args += ["-e", "HUB_MODE=1"]
        if task_agent:
            # TASK_AGENT_MODE modifies the AGENT_MODE branch (same mailbox
            # precondition, readiness sentinel and supervisor exec) rather than
            # being a fourth mode. Labels are host-set, so the cap and the
            # own-fleet check can trust them; the env vars are what the
            # in-container supervisor turns into meta.json.
            args += [
                "--label", f"org.cld.task={task_agent.slug}",
                "--label", f"org.cld.parent-master={task_agent.parent_master}",
                "-e", "TASK_AGENT_MODE=1",
                "-e", f"AGENT_TASK_SLUG={task_agent.slug}",
                "-e", f"AGENT_PARENT_MASTER={task_agent.parent_master}",
                "-e", f"AGENT_DELIVERABLE_BRANCH={task_agent.deliverable_branch}",
                "-e", f"AGENT_PEERS={task_agent.peers_env()}",
                # In-container Config.from_env() sees no host user TOML, so the
                # operator's configured budgets have to be passed in.
                "-e", f"CLD_PEER_ABSOLUTE_LIMIT={cfg.peer_absolute_limit}",
                "-e", f"CLD_ROOT_ASK_LIMIT={cfg.root_ask_limit}",
                "-e", f"CLD_AGENT_MAX_TURNS={cfg.agent_max_turns}",
            ]
    else:
        args += ["--rm"]
        if bare_devcontainer:
            args += [
                "--name", session_name,
                "--label", "org.cld.kind=devcontainer",
                "--label", f"org.cld.repo-root={host_repo_root}",
                "--label", f"org.cld.session={session_name}",
                "-e", "HUB_MODE=1",
            ]

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

    # Host-path plumbing: lets in-container code translate /workspace/* and
    # $HOME back to host paths (path translation, sibling-target resolution).
    # Always set -- these used to ride along with the docker socket mount, which
    # has since been removed (the host channel is now the broker; see
    # cld/broker.py and docs/design-cld-broker.md).
    args += [
        "-e", f"CLD_HOST_PROJECT_DIR={host_repo_root}",
        "-e", f"CLD_HOST_HOME={host_home}",
    ]

    # Workspace setup: gitignored files to symlink into workspace
    if cfg.ignore_gitignore:
        workspace_files = ":".join(cfg.ignore_gitignore)
        args += ["-e", f"WORKSPACE_FILES={workspace_files}"]
        log.debug(f"Workspace files to link: {workspace_files}")

    # No docker socket is mounted into any container (it was equivalent to host
    # root). In-container docker needs -- peer enumeration and sibling `cld
    # agent` launches from inside master -- go through the host broker over SSH
    # (see cld/broker.py, broker/cld-broker.sh). The broker key is mounted for
    # persistent roles (master, agent, task-agent) and the bare ephemeral
    # devcontainer by stage_broker below; the
    # `agent`/`task-agent` launcher actions stay master-only regardless, gated
    # by the org.cld.targets label (only master carries it, see master_targets
    # below), not by broker reachability.

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

    # Host test broker (persistent roles, plus the bare ephemeral devcontainer):
    # mount the restricted key + known_hosts and make the broker reachable.
    # No-op unless broker_key is set. Agents and task-agents get this too so
    # they can run `cld broker run-tests`, but their personas instruct them to
    # only invoke it with explicit per-run authorization from their master --
    # see prompts/personas/agent.md and prompts/personas/task-agent.md. The
    # bare devcontainer has no such persona gate: it's the interactive user's
    # own throwaway session, same trust level as a `cld master` shell.
    if master or agent or task_agent or bare_devcontainer:
        args += stage_broker(cfg)

    # Mailbox tree -- shared RW mount so every master, agent, task-agent and
    # bare devcontainer container sees the same mailbox filesystem. The bare
    # devcontainer needs its own mailbox to spawn agents/task-agents and get
    # `cld msg` / the messenger MCP's send()/list_inbox() working, same as master.
    if master or agent or task_agent or bare_devcontainer:
        mailbox_root = Path(cfg.mailbox_root).expanduser()
        host_mailbox_root = to_host_path(str(mailbox_root), cfg)
        if host_mailbox_root == str(mailbox_root):
            # Bare host: our own filesystem view already *is* the host view.
            mailbox_root.mkdir(parents=True, exist_ok=True)
        else:
            # Nested (cld running inside another container): the real host path
            # isn't in our filesystem view, and with no docker socket mounted
            # there is no way (nor any wish) to reach across to the host to
            # create it -- container isolation from the host is a hard
            # requirement. If the path doesn't already exist on the real host,
            # `docker run` will auto-create it as root and the non-root
            # container user won't be able to write into it.
            log.warning(
                "Mailbox root %s is outside this process's filesystem view "
                "(nested cld). If it doesn't already exist on the real host, "
                "create it there manually before continuing: "
                "mkdir -p %s && chown %d:%d %s",
                host_mailbox_root, host_mailbox_root, os.getuid(), os.getgid(), host_mailbox_root,
            )
        args += ["-v", f"{host_mailbox_root}:{MAILBOX_MOUNT}:rw"]
        log.info("Mailbox mounted: %s -> %s", host_mailbox_root, MAILBOX_MOUNT)

    # Hub roles only (master, bare devcontainer): publish the registered
    # sibling target paths as an env var so the entrypoint can materialize them
    # as empty placeholder directories (see docs/design-master-sibling-launch.md).
    # Neither gets a bind mount of a sibling repo -- the placeholder just lets
    # `cd <path>` succeed and lets cld-inside-the-container resolve cwd to the
    # host path. Host paths must exist on the host so the peer's -v mount will
    # succeed later; we fail fast here rather than at peer-launch time.
    if (master or bare_devcontainer) and cfg.master_targets:
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
        joined = ":".join(expanded_targets)
        args += ["-e", f"MASTER_TARGETS={joined}"]
        # Host-set, immutable allowlist the broker validates sibling `cld agent`
        # launches against (a container can rewrite its MASTER_TARGETS env but
        # not this label). See action_agent in broker/cld-broker.sh.
        args += ["--label", f"org.cld.targets={joined}"]

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


_TASK_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def task_agent_container_name(repo_root: Path, slug: str, suffix: int = 0) -> str:
    """Container name for a task-scoped agent: ``cld_agent_<repo>_<slug>[-<suffix>]``.

    The ``cld_agent_`` prefix is deliberately shared with the repo agent -- the
    ``org.cld.kind`` label is the discriminator, not the name (see
    docs/design-task-agents.md D5). *suffix* > 1 is the collision disambiguator.
    """
    if not _TASK_SLUG_RE.match(slug):
        raise ValueError(
            f"invalid task slug {slug!r}: expected kebab-case (lowercase letters, "
            "digits and dashes, starting with a letter or digit)"
        )
    base = f"cld_agent_{repo_root.name}_{slug}"
    return f"{base}-{suffix}" if suffix > 1 else base


def allocate_task_agent_name(repo_root: Path, slug: str) -> str:
    """Return the first task-agent name for *slug* that no container holds yet.

    Docker is the liveness ground truth here: an archived mailbox from a reaped
    agent must not block its slug from being reused (§4). The master should still
    prefer a fresh slug so names stay meaningful.
    """
    suffix = 0
    while True:
        name = task_agent_container_name(repo_root, slug, suffix)
        if _docker_status(name) == "absent":
            return name
        log.info("task-agent name %s is taken; trying the next suffix", name)
        suffix = max(suffix, 1) + 1


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


def docker_task_agent_status(name: str) -> str:
    return _docker_status(name)


# `docker inspect` exposes a container's labels at .Config.Labels; there is no
# top-level .Labels (that's a `docker ps --format` field) and referencing a
# missing *field* fails the whole template, which would silently drop every
# container. A missing label *key* is fine -- index on a map yields "".
_INSPECT_LABELS = (
    ("repo_root", "org.cld.repo-root"),
    ("session", "org.cld.session"),
    ("kind", "org.cld.kind"),
    ("parent", "org.cld.parent-master"),
    ("task", "org.cld.task"),
)
_INSPECT_FORMAT = "|".join(f'{{{{index .Config.Labels "{label}"}}}}' for _, label in _INSPECT_LABELS)


def _docker_kind_list(kind: str, *, running_only: bool = False) -> list[dict]:
    """Return containers of *kind* ('master', 'agent', 'task-agent') with their org.cld.* labels.

    Records are ``{name, repo_root, session, kind, parent, task}``; the last two are
    empty for roles that don't set them. ``running_only`` filters docker-side --
    the task-agent cap counts running containers only, and `docker ps -a` would
    otherwise let stopped corpses refuse a spawn (see docs/design-task-agents.md §9).
    """
    filters = ["--filter", f"label=org.cld.kind={kind}"]
    if running_only:
        filters += ["--filter", "status=running"]
    result = subprocess.run(
        ["docker", "ps", "-a", *filters, "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    log_subprocess(log, ["docker", "ps", "-a", *filters], result)
    if result.returncode != 0:
        return []
    keys = [key for key, _ in _INSPECT_LABELS]
    containers: list[dict] = []
    for name in result.stdout.strip().splitlines():
        if not name:
            continue
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format", _INSPECT_FORMAT],
            capture_output=True, text=True,
        )
        log_subprocess(log, ["docker", "inspect", name], inspect)
        if inspect.returncode != 0:
            continue
        values = (inspect.stdout.strip().split("|") + [""] * len(keys))[:len(keys)]
        containers.append({"name": name, **dict(zip(keys, values, strict=True))})
    return containers


def docker_master_list() -> list[dict]:
    """Return all master containers with their org.cld.* labels."""
    return _docker_kind_list("master")


def docker_agent_list() -> list[dict]:
    """Return all repo agent containers with their org.cld.* labels."""
    return _docker_kind_list("agent")


def docker_task_agent_list(*, running_only: bool = False) -> list[dict]:
    """Return all task-agent containers with their org.cld.* labels."""
    return _docker_kind_list("task-agent", running_only=running_only)


def assert_task_agent_capacity(cfg: Config, parent_master: str) -> None:
    """Raise if *parent_master* already runs ``cfg.max_task_agents`` task-agents.

    Running containers only -- stopped corpses must not refuse a spawn. The empty
    parent (a human-launched agent with no master) is counted as its own group:
    the cap exists to bound shared jj-store contention, which doesn't care who
    spawned what. See docs/design-task-agents.md §9.
    """
    running = [c for c in docker_task_agent_list(running_only=True) if c["parent"] == parent_master]
    if len(running) < cfg.max_task_agents:
        return
    listed = ", ".join(f"{c['name']} ({c['task'] or 'no task'})" for c in running)
    raise RuntimeError(
        f"task-agent cap reached for {parent_master or 'host-launched agents (no master)'}: "
        f"{len(running)}/{cfg.max_task_agents} running [{listed}]. Reap one with "
        "`cld task-agent shutdown <name>`, or raise max_task_agents / CLD_MAX_TASK_AGENTS."
    )


def resolve_task_agent_anchor(cfg: Config, repo_root: Path, revision: str) -> str:
    """Resolve *revision* to a commit hash, refusing an anchor inside a live agent's stack.

    A live agent may squash or rebase its own stack at any moment, so anchoring on
    it pins the new agent to a base its owner has since revised -- silently, and
    unfixable without re-anchoring from scratch. Teardown is the "finished" signal,
    so the refusal names the owning agent and asks for it to be reaped first (§8, §9).

    Scoped to task-agents running in *repo_root*, not to one master's fleet: the
    hazard is store-level, so another master's live agent is just as dangerous.
    jj-only -- peer-side anchor staging has no git equivalent. Anchoring on the
    shared base still passes, since a live agent's anchor is a *child* of it.
    """
    from cld.messenger import mailbox
    from cld.vcs import get_backend
    from cld.vcs.anchor import resolve_anchor

    vcs = get_backend(repo_root)
    anchor = resolve_anchor(vcs, revision)
    if vcs.name != "jj":
        log.debug("live-stack anchor check skipped: %s backend has no equivalent", vcs.name)
        return anchor

    host_repo = to_host_path(str(repo_root), cfg)
    mailbox_root = Path(cfg.mailbox_root).expanduser()
    live: dict[str, str] = {}
    for c in docker_task_agent_list(running_only=True):
        if c["repo_root"] != host_repo:
            continue
        meta = mailbox.read_meta(mailbox_root, c["name"]) or {}
        if meta.get("anchor"):
            live[meta["anchor"]] = c["name"]
    if not live:
        return anchor

    descendants = " | ".join(f"{a}::" for a in live)
    probe = vcs.run(["log", "-r", f"{anchor} & ({descendants})", "--no-graph", "-T", "commit_id", "-n", "1"])
    if probe.returncode != 0:
        # Our own bookkeeping (a meta.json anchor no longer in the store) must not
        # block a legitimate spawn -- warn and let it through.
        log.warning(
            "could not evaluate the live-stack anchor check against %s: %s",
            ", ".join(f"{name}@{a[:12]}" for a, name in live.items()), (probe.stderr or "").strip(),
        )
        return anchor
    if not probe.stdout.strip():
        return anchor

    # Only on refusal, and bounded by the cap: find which agent owns the stack.
    owner, owner_anchor = next(iter(live.items()))[::-1]
    for live_anchor, name in live.items():
        hit = vcs.run(["log", "-r", f"{anchor} & {live_anchor}::", "--no-graph", "-T", "commit_id", "-n", "1"])
        if hit.returncode == 0 and hit.stdout.strip():
            owner, owner_anchor = name, live_anchor
            break
    raise RuntimeError(
        f"refusing to anchor on {anchor[:12]}: it is inside the live stack of task-agent "
        f"{owner} (anchor {owner_anchor[:12]}). A live agent can still rewrite that stack. "
        f"Reap it first (`cld task-agent shutdown {owner}`) -- teardown is what makes its "
        "deliverable branch safe to anchor on -- or anchor on the shared base instead."
    )


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


def stage_broker(cfg: Config) -> list[str]:
    """Return docker args wiring a container to the cld broker.

    Mounts the restricted broker private key (and, if given, the pinned
    known_hosts) RO, adds a host-gateway alias so the container can reach the
    host-side sshd, and sets ``CLD_BROKER_ENDPOINT`` so the in-container client
    (``cld broker <action>``) can reach it. No-op unless ``cfg.broker_key`` is set.
    Called for master, agent, task-agent and the bare ephemeral devcontainer
    (see the call site in ``build_container_args``); the broker's sshd accepts
    any ``cld_*`` session and resolves its role from the ``org.cld.kind`` label
    set at launch, not from the name -- see ``broker/cld-broker.sh``. See
    docs/design-cld-broker.md.
    """
    if not cfg.broker_key:
        return []
    key = Path(cfg.broker_key).expanduser()
    if not key.is_file():
        log.warning("broker_key set but not found: %s", key)
        return []
    host_key = to_host_path(str(key.resolve()), cfg)
    args = [
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{host_key}:{_BROKER_KEY_MOUNT}:ro",
        "-e", f"CLD_BROKER_ENDPOINT={cfg.broker_endpoint}",
    ]
    if cfg.broker_known_hosts:
        known = Path(cfg.broker_known_hosts).expanduser()
        if known.is_file():
            host_known = to_host_path(str(known.resolve()), cfg)
            args += ["-v", f"{host_known}:{_BROKER_KNOWN_HOSTS_MOUNT}:ro"]
        else:
            log.warning("broker_known_hosts set but not found: %s", known)
    else:
        log.warning(
            "broker_key set but broker_known_hosts is empty -- "
            "cld broker run-tests's strict host-key check will fail without a pinned known_hosts"
        )
    log.info("Host test broker wired: key -> %s, endpoint %s", _BROKER_KEY_MOUNT, cfg.broker_endpoint)
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
