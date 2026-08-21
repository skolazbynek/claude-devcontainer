"""CLI entry point for cld."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from cld.chain import ParallelGroup, apply_name_override, chain_state_dir, load_chain, print_chain_report, run_chain, validate_chain
from cld.chain_state import ChainState, StateWriter, write_state, _utcnow_iso
from cld.config import Config
from cld.docker import (
    TaskAgentSpec,
    agent_container_name,
    allocate_task_agent_name,
    anchor_env_args,
    assert_task_agent_capacity,
    base_extra_paths,
    build_container_args,
    build_session_name,
    devcontainer_extra_paths,
    docker_agent_list,
    docker_agent_status,
    docker_master_list,
    docker_master_status,
    docker_task_agent_list,
    docker_task_agent_status,
    ensure_image,
    find_repo_root,
    find_target_repo,
    master_container_name,
    require_docker,
    resolve_task_agent_anchor,
    run_extra_paths,
    stage_home_ro,
    stage_ssh_agent,
    to_host_path,
)
from cld.agent_runtime import format_age
from cld.bridge import daemon as bridge_daemon
from cld.bridge.mattermost import build_bridge, run_bridge
from cld.cli_msg import handle_errors as _handle_errors, msg_app
from cld.messenger import mailbox
from cld.run import launch_run
from cld.log import get_logger, setup_logging
from cld.prompts import compose_brief, list_prompt_items, resolve_prompt_args
from cld.task_agent import (
    format_peers,
    known_task_agent_names,
    mailbox_root,
    parse_peer_specs,
    print_task_agent_detail,
    print_task_agent_roster,
    print_task_agent_transcript,
    resolve_task_agent,
    task_agent_parent,
    task_agent_record,
    task_agent_rows,
)
from cld.vcs import get_backend
from cld.vcs.anchor import resolve_anchor

log = get_logger(__name__)

app = typer.Typer()


def _reject_in_container() -> None:
    """Refuse `python3 -m cld` inside a container: this app needs a docker daemon.

    The container surface is its own app (cld/cli_container.py), installed as `cld`
    in the devcontainer image -- see docs/design-cli-split.md.
    """
    if os.environ.get("HUB_MODE") or os.environ.get("AGENT_MODE"):
        typer.echo(
            "Error: this is the host cld, which needs a docker daemon. Inside a "
            "container run `cld` instead (task-agent, agent, msg, repos, prompts).",
            err=True,
        )
        raise typer.Exit(1)


def _version_callback(value: bool):
    if value:
        from cld import __version__
        typer.echo(f"cld {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
@_handle_errors
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt for the session"),
):
    """Launch an ephemeral interactive Claude devcontainer (default; no subcommand).

    Takes `-p` only, no prompt refs: click resolves a group callback's first positional
    as a subcommand name, so `cld @personas/x` can only ever be "No such command".
    Refs live on the real commands -- `cld run`, `cld task-agent start`, `cld chain run`.
    """
    _reject_in_container()
    if ctx.invoked_subcommand is not None:
        return
    _run_devcontainer(name, model, revision, prompt)


@app.command()
@_handle_errors
def run(
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs in order: @<ref> from prompts/, or a file path"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline task description, appended after the refs"),
):
    """Launch a one-shot autonomous Claude agent (headless, --rm, commits to a branch)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    cld_root = Path(__file__).resolve().parent.parent
    brief, _ = _compose_from_args(refs or [], prompt, find_repo_root(), cld_root)

    log.info(
        "run: name=%s, model=%s, revision=%s, refs=%s, prompt=%s",
        name or "<auto>",
        model or "<default>",
        revision or "<default>",
        ", ".join(refs or []) or "<none>",
        "<provided>" if prompt else "<none>",
    )

    launch_run(cfg, brief, name=name, model=model, revision=revision)


def _compose_from_args(
    refs: list[str], prompt: str, repo_root: Path, cld_root: Path
) -> tuple[str, list[tuple[Path, str]]]:
    """Resolve refs and compose the brief, turning input errors into clean exits.

    Returns the brief and the resolved `(path, kind)` refs, so a caller that needs the
    kinds (the persona a task-agent displays) does not resolve the same list twice.
    """
    try:
        resolved = resolve_prompt_args(refs, repo_root, cld_root)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    for path, kind in resolved:
        log.info("prompt %s: %s", kind, path)
    if not resolved and not prompt:
        typer.echo("Error: Provide at least one prompt ref, --prompt, or both", err=True)
        raise typer.Exit(1)
    return compose_brief([p for p, _ in resolved], prompt), resolved


def _run_devcontainer(
    name: str,
    model: str,
    revision: str,
    prompt: str,
) -> None:
    """Ephemeral interactive devcontainer launch. Persistent master/agent live in their own sub-apps."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)

    log.info(
        "devcontainer: name=%s, model=%s, revision=%s, prompt=%s",
        name or "<auto>",
        model or "<default>",
        revision or "<default>",
        "<provided>" if prompt else "<none>",
    )

    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.devcontainer_image,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        extra_paths=devcontainer_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
    )

    session = build_session_name("cld", name)
    repo_root = find_target_repo(cfg)

    brief = compose_brief([], prompt) if prompt else ""

    args = build_container_args(repo_root, session, cfg, interactive=True)
    args += anchor_env_args(cfg, session, revision, brief=brief)
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]

    skipped = []
    for rel in cfg.home_mounts_devcontainer:
        mnt = stage_home_ro(rel, cfg)
        if mnt:
            args += mnt
        else:
            skipped.append(rel)

    if skipped:
        log.warning(f"Optional host paths not found (skipped): {', '.join(skipped)}")

    args += stage_ssh_agent(cfg)

    args += [cfg.devcontainer_image]

    log.info("Starting Claude Code in container...")
    print()

    os.execvp("docker", ["docker", "run"] + args)


def _wait_for_container_ready(name: str, sentinel: str, timeout: int = 60) -> bool:
    """Poll container until *sentinel* file exists inside it. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "exec", name, "test", "-f", sentinel],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(1)
    return False


def _persistent_container_name(role: str, repo_root: Path) -> str:
    return master_container_name(repo_root) if role == "master" else agent_container_name(repo_root)


def _persistent_container_status(role: str, name: str) -> str:
    return docker_master_status(name) if role == "master" else docker_agent_status(name)


_READY_SENTINEL = {"master": "/tmp/cld-master-ready", "agent": "/tmp/cld-agent-ready"}


def _run_persistent_devcontainer(
    role: str,
    brief: str,
    name: str,
    model: str,
    revision: str,
    cfg: Config,
) -> None:
    """Master/agent-mode devcontainer: start-or-attach the one persistent container per repo.

    Master attaches an interactive shell; the headless agent role never attaches
    (see docs/design-agent-messaging.md) -- it starts (or confirms it's running)
    and returns.
    """
    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.devcontainer_image,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        extra_paths=devcontainer_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
    )

    repo_root = find_target_repo(cfg)
    session = _persistent_container_name(role, repo_root)
    status = _persistent_container_status(role, session)
    log.info("%s devcontainer: name=%s, status=%s", role, session, status)

    if status in ("running", "stopped"):
        if brief:
            typer.echo(
                f"Error: --{role} re-attach: prompt refs and -p/--prompt cannot be used "
                "when the container already exists (prompt was consumed at first launch)",
                err=True,
            )
            raise typer.Exit(1)
        if revision:
            log.warning("--%s: re-attaching to existing container; -r/--revision ignored", role)
        if status == "stopped":
            log.info("Starting stopped %s container: %s", role, session)
            subprocess.run(["docker", "start", session], check=True)
        if role == "master":
            log.info("Attaching to master container: %s", session)
            os.execvp("docker", ["docker", "exec", "-it", session, "/bin/bash", "-l"])
        typer.echo(f"Agent '{session}' is running. Message it via the messenger MCP's send() tool.")
        return

    # absent — create a new persistent container. The container entrypoint
    # itself creates the ephemeral workspace at /workspace/current on top of
    # the anchor B commit. On a subsequent restart the bookmark `<session>`
    # already exists in the origin store; the entrypoint detects it and
    # reattaches without re-staging an anchor, so this path only runs on the
    # very first launch. Inside master, `anchor_env_args` emits an unresolved
    # revision hint + scratch envelope so the peer's entrypoint does the
    # anchor work locally (master has no RW view of a sibling target).
    args = build_container_args(
        repo_root, session, cfg, interactive=False,
        master=(role == "master"), agent=(role == "agent"),
    )
    args += anchor_env_args(cfg, session, revision, brief=brief)
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]

    skipped = []
    for rel in cfg.home_mounts_devcontainer:
        mnt = stage_home_ro(rel, cfg)
        if mnt:
            args += mnt
        else:
            skipped.append(rel)
    if skipped:
        log.warning("Optional host paths not found (skipped): %s", ", ".join(skipped))

    args += stage_ssh_agent(cfg)

    args += [cfg.devcontainer_image]

    log.info("Starting %s devcontainer (detached)...", role)
    subprocess.run(["docker", "run", "-d"] + args, check=True)

    log.info("Waiting for container to be ready...")
    if not _wait_for_container_ready(session, _READY_SENTINEL[role]):
        typer.echo(
            f"Error: {role.capitalize()} container '{session}' did not become ready within 60 s. "
            "Check: docker logs " + session,
            err=True,
        )
        raise typer.Exit(1)

    if role == "master":
        log.info("Master container ready; attaching...")
        os.execvp("docker", ["docker", "exec", "-it", session, "/bin/bash", "-l"])

    typer.echo(f"Agent '{session}' started for {repo_root}.")
    typer.echo("  Status: cld agent status")
    typer.echo("  Logs:   cld agent logs")
    typer.echo(f"  Send:   messenger MCP send(to=\"{repo_root.name}\", ...) from another container")


def _do_shutdown(role: str, all_: bool) -> None:
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    if all_:
        containers = docker_agent_list() if role == "agent" else docker_master_list()
        if not containers:
            typer.echo(f"No {role} containers found.")
            return
        failed = False
        for c in containers:
            if not _shutdown_persistent_container(role, c["name"], c["repo_root"], c["session"]):
                failed = True
        if failed:
            raise typer.Exit(1)
        return
    repo_root = find_target_repo(cfg)
    container_name = _persistent_container_name(role, repo_root)
    if _persistent_container_status(role, container_name) == "absent":
        typer.echo(f"No {role} container found for this repo.")
        return
    if not _shutdown_persistent_container(role, container_name, str(repo_root), container_name):
        raise typer.Exit(1)


def _do_restart(role: str) -> None:
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    repo_root = find_target_repo(cfg)
    container_name = _persistent_container_name(role, repo_root)
    if _persistent_container_status(role, container_name) == "absent":
        typer.echo(f"No {role} container to restart. Start one with: cld {role}", err=True)
        raise typer.Exit(1)
    # Bypasses _shutdown_persistent_container so the session bookmark
    # survives; the container entrypoint reattaches at its tip.
    _stop_and_remove_container(container_name, restart=True)
    _run_persistent_devcontainer(role, "", "", "", "", cfg)


def _do_status(role: str) -> None:
    cfg = Config.from_env()
    setup_logging(cfg)
    repo_root = find_target_repo(cfg)
    container_name = _persistent_container_name(role, repo_root)
    docker_status = _persistent_container_status(role, container_name)
    typer.echo(f"{role.capitalize()}: {container_name}")
    typer.echo(f"  Container: {docker_status}")
    if role != "agent":
        return
    # Host-only path now: `cld agent status` from inside master is delegated to
    # the broker, which runs this on the host against the real mailbox_root.
    mailbox_root = Path(cfg.mailbox_root).expanduser()
    state_path = mailbox_root / container_name / "state.json"
    if not state_path.is_file():
        typer.echo("  Supervisor state: unavailable (not started yet, or mailbox_root misconfigured)")
        return
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        typer.echo(f"  Supervisor state: unreadable ({e})")
        return
    typer.echo(f"  Phase:       {state.get('phase')}")
    typer.echo(f"  Session ID:  {state.get('session_id')}")
    typer.echo(f"  Messages:    {state.get('msg_count')}")
    typer.echo(f"  Cost so far: ${state.get('cost_usd_total', 0.0):.4f}")
    current = state.get("current")
    if current:
        typer.echo(f"  Processing:  {current.get('subject')} (from {current.get('from')}, id {current.get('id')})")


def _do_logs(role: str, tail: int) -> None:
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    repo_root = find_target_repo(cfg)
    container_name = _persistent_container_name(role, repo_root)
    if _persistent_container_status(role, container_name) == "absent":
        typer.echo(f"No {role} container found for this repo.", err=True)
        raise typer.Exit(1)
    _docker_logs(container_name, tail)


def _docker_logs(name: str, tail: int) -> None:
    """Print the tail of a container's log. Supervisor output arrives on stderr."""
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), name],
        capture_output=True, text=True,
    )
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, nl=False, err=True)


# --- Persistent master (interactive, per-repo) --------------------------------
master_app = typer.Typer(
    help="Persistent per-repo interactive devcontainer (start-or-attach).",
    invoke_without_command=True,
)
app.add_typer(master_app, name="master")


@master_app.callback(invoke_without_command=True)
@_handle_errors
def master(
    ctx: typer.Context,
    model: str = typer.Option("", "-m", "--model", help="Claude model (first launch only)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (first launch only)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="First-launch prompt (refused on re-attach)"),
):
    """Start (or attach to) the persistent master devcontainer for this repo.

    Like bare `cld`, this takes `-p` only -- a group callback never sees positionals.
    """
    if ctx.invoked_subcommand is not None:
        return
    cfg = Config.from_env()
    setup_logging(cfg)
    brief = compose_brief([], prompt) if prompt else ""
    _run_persistent_devcontainer("master", brief, "", model, revision, cfg)


@master_app.command("restart")
@_handle_errors
def master_restart():
    """Restart the master devcontainer for this repo, picking up image/code changes."""
    _do_restart("master")


@master_app.command("shutdown")
@_handle_errors
def master_shutdown(
    all_: bool = typer.Option(False, "--all", help="Stop all master containers on this host"),
):
    """Stop and remove the master devcontainer for this repo (or all with --all)."""
    _do_shutdown("master", all_)


@master_app.command("status")
@_handle_errors
def master_status():
    """Print status of the master devcontainer for this repo."""
    _do_status("master")


@master_app.command("logs")
@_handle_errors
def master_logs(
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Tail the master container's log output."""
    _do_logs("master", tail)


# --- Persistent repo agent (headless, mailbox-driven, per-repo) ---------------
agent_app = typer.Typer(
    help="Persistent per-repo headless Claude agent (mailbox-driven; see docs/design-agent-messaging.md).",
    invoke_without_command=True,
)
app.add_typer(agent_app, name="agent")


@agent_app.callback(invoke_without_command=True)
@_handle_errors
def agent(
    ctx: typer.Context,
    model: str = typer.Option("", "-m", "--model", help="Claude model (first launch only)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (first launch only)"),
):
    """Start the persistent repo agent for this repo. Idempotent per repo."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = Config.from_env()
    setup_logging(cfg)
    _run_persistent_devcontainer("agent", "", "", model, revision, cfg)


@agent_app.command("restart")
@_handle_errors
def agent_restart():
    """Restart the repo agent for this repo, picking up image/code changes."""
    _do_restart("agent")


@agent_app.command("shutdown")
@_handle_errors
def agent_shutdown(
    all_: bool = typer.Option(False, "--all", help="Stop all agent containers on this host"),
):
    """Stop and remove the repo agent for this repo (or all with --all)."""
    _do_shutdown("agent", all_)


@agent_app.command("status")
@_handle_errors
def agent_status():
    """Print status of the repo agent for this repo (docker + supervisor phase)."""
    _do_status("agent")


@agent_app.command("logs")
@_handle_errors
def agent_logs(
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Tail the repo agent's log output (= supervisor stderr)."""
    _do_logs("agent", tail)


def _stop_and_remove_container(name: str, *, restart: bool = False) -> None:
    """Stop and remove a container. Idempotent.

    restart=True stops via SIGUSR1 so the container keeps its session bookmark
    for the fresh container to reattach, waiting briefly for a clean exit and
    falling back to SIGKILL (which also skips the forget) past the grace
    period. A plain stop (SIGTERM) lets the container forget its bookmark.
    """
    log.info("Stopping container: %s", name)
    if restart:
        subprocess.run(["docker", "kill", "--signal=SIGUSR1", name], capture_output=True)
        try:
            subprocess.run(["docker", "wait", name], capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            subprocess.run(["docker", "wait", name], capture_output=True)
    else:
        subprocess.run(["docker", "stop", name], capture_output=True)
    subprocess.run(["docker", "rm", name], capture_output=True)


def _shutdown_persistent_container(role: str, name: str, repo_root_str: str, session: str) -> bool:
    """Stop and remove a master/agent container; end the session's lifecycle.

    `docker rm` drops the ephemeral workspace. Forgetting the bookmark
    `<session>` in the origin's jj store makes the next `cld <role>` launch
    a fresh lifecycle (honoring `-r/--revision` again). Committed work and
    op-log snapshots remain in the store, reachable by change ID -- see
    `jj log -r 'heads(all())'`. `cld <role> restart` bypasses this function
    (calls `_stop_and_remove_container` directly) so restart preserves the
    bookmark and reattaches.
    """
    _stop_and_remove_container(name)
    _forget_session_state(repo_root_str, session)
    typer.echo(f"Stopped and removed {role} container: {name}")
    return True


def _forget_session_state(repo_root_str: str, session: str) -> None:
    """Drop the session's bookmark and workspace registration from the origin's jj store.

    Best-effort: both entries are independent (bookmark = named commit
    pointer, workspace = registered working-copy path). If we leave the
    workspace behind, the next `cld <role>` launch takes the "first launch"
    path (no bookmark) and its `jj workspace add --name <session>` fails
    with "Workspace named X already exists", leaving /workspace/current empty.
    """
    repo_root = Path(repo_root_str)
    if not repo_root.is_dir():
        log.warning(
            "Cannot clean up session state for %s: repo_root %s no longer exists. "
            "Recover manually with: cd <repo> && jj bookmark forget %s && jj workspace forget %s",
            session, repo_root, session, session,
        )
        return
    try:
        backend = get_backend(repo_root)
    except RuntimeError as e:
        log.warning(
            "Cannot clean up session state for %s in %s: %s. "
            "Recover manually with: cd %s && jj bookmark forget %s && jj workspace forget %s",
            session, repo_root, e, repo_root, session, session,
        )
        return
    if backend.name != "jj":
        return
    for cmd in (["bookmark", "forget", session], ["workspace", "forget", session]):
        result = backend.run(cmd)
        if result.returncode != 0:
            log.warning(
                "jj %s failed (rc=%d): %s. "
                "Next `cld` launch may reattach to stale state; "
                "recover with: cd %s && jj %s",
                " ".join(cmd), result.returncode, result.stderr.strip(),
                repo_root, " ".join(cmd),
            )


# --- Task-scoped agents (headless, many per repo, master-owned lifecycle) -----
task_agent_app = typer.Typer(
    help="Task-scoped headless agents: one per task, bounded lifespan (see docs/design-task-agents.md).",
)
app.add_typer(task_agent_app, name="task-agent")

# Reap-readiness check 1 waits this long for an in-flight turn to finish before
# refusing (docs/design-task-agents.md §7 "waits briefly"). Matches `docker stop`'s
# grace period: enough for a turn that is about to end, not enough to block on one
# that just started.
_REAP_WAIT_SECONDS = 10


@task_agent_app.command("start")
@_handle_errors
def task_agent_start(
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs in order: @<ref> from prompts/ (e.g. @personas/implementer), or a file path"),
    name: str = typer.Option("", "-n", "--name", help="Task slug, kebab-case (default: --branch)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline task description, appended after the refs"),
    branch: str = typer.Option("", "--branch", help="Deliverable branch name (default: the task slug)"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change)"),
    peer: list[str] = typer.Option([], "--peer", help="A peer this agent may message: <container-name>[:<hops>]. Repeatable."),
    parent: str = typer.Option("", "--parent", hidden=True, help="Owning master session (set by the host broker)"),
):
    """Spawn a task-scoped agent. Every start creates a new container (no start-or-attach)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    require_docker()

    cld_root = Path(__file__).resolve().parent.parent
    repo_root = find_target_repo(cfg)
    brief, resolved = _compose_from_args(refs or [], prompt, repo_root, cld_root)
    # Display only: the roster's Persona column and the launch banner. A task-agent with
    # no persona ref is coherent -- the lifecycle preamble is always layered in.
    role = next((p.stem for p, kind in resolved if kind == "persona"), "")
    slug = name or branch
    if not slug:
        typer.echo(
            "Error: a task slug is required -- pass -n/--name <slug> (short, kebab-case, "
            "naming the task) or --branch, which it falls back to",
            err=True,
        )
        raise typer.Exit(1)
    branch = branch or slug
    peers = parse_peer_specs(peer, cfg.peer_absolute_limit)

    # Validates the slug shape, and settles the name before the refusals so every
    # input error surfaces ahead of them. Allocation reserves nothing -- it only
    # probes docker for a free name.
    session = allocate_task_agent_name(repo_root, slug)
    if session in peers:
        raise ValueError(f"--peer {session} names this agent itself")
    unknown = sorted(set(peers) - known_task_agent_names(cfg))
    if unknown:
        # Not a refusal: the master owns the graph, and only an already-spawned
        # agent can be named, so a name this host hasn't seen is usually a typo
        # -- which its first send() would surface anyway (§7).
        log.warning(
            "peer(s) not known to this host: %s. Peers are addressed by full container "
            "name; check `cld task-agent status`.", ", ".join(unknown),
        )

    # Both refusals are host-side, before anything is built or spawned: the cap
    # counts running siblings, the anchor check reads the origin store (§9).
    assert_task_agent_capacity(cfg, parent)
    anchor = resolve_task_agent_anchor(cfg, repo_root, revision)

    ensure_image(
        cfg.devcontainer_image,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        extra_paths=devcontainer_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
    )

    log.info(
        "task-agent: name=%s, task=%s, persona=%s, branch=%s, anchor=%s, peers=%s, parent=%s",
        session, slug, role or "<none>", branch, anchor[:12],
        ",".join(f"{p}:{h}" for p, h in sorted(peers.items())) or "none",
        parent or "<none>",
    )

    args = build_container_args(
        repo_root, session, cfg,
        task_agent=TaskAgentSpec(
            slug=slug, parent_master=parent, deliverable_branch=branch, peers=peers,
        ),
    )
    # The already-resolved anchor, not `revision`: the launch must pin exactly the
    # commit the live-stack refusal inspected. The brief rides in the same envelope.
    args += anchor_env_args(cfg, session, anchor, brief=brief)
    args += ["-e", f"AGENT_PERSONA={role}"]
    if model:
        args += ["-e", f"AGENT_MODEL={model}"]

    skipped = []
    for rel in cfg.home_mounts_devcontainer:
        mnt = stage_home_ro(rel, cfg)
        if mnt:
            args += mnt
        else:
            skipped.append(rel)
    if skipped:
        log.warning("Optional host paths not found (skipped): %s", ", ".join(skipped))

    args += stage_ssh_agent(cfg)
    args += [cfg.devcontainer_image]

    log.info("Starting task-agent (detached)...")
    subprocess.run(["docker", "run", "-d"] + args, check=True)

    # The handle every read verb takes, which is not always the slug: a live-name
    # collision appends a suffix, and the hints have to point at *this* agent.
    handle = session.rsplit("_", 1)[-1]

    if not _wait_for_container_ready(session, _READY_SENTINEL["agent"]):
        typer.echo(
            f"Error: task-agent '{session}' did not become ready within 60 s. "
            f"It is still running -- check: cld task-agent logs {handle}",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Task-agent '{session}' started for {repo_root}.")
    typer.echo(f"  Task:       {slug}")
    typer.echo(f"  Persona:    {role or '-'}")
    typer.echo(f"  Branch:     {branch}")
    typer.echo(f"  Anchor:     {anchor[:12]}")
    typer.echo(f"  Peers:      {format_peers(peers)}")
    typer.echo(f"  Status:     cld task-agent status {handle}")
    typer.echo(f"  Logs:       cld task-agent logs {handle}")
    typer.echo(f"  Transcript: cld task-agent transcript {handle}")
    typer.echo(f"  Send:       messenger MCP send(to=\"{session}\", ...)")


@task_agent_app.command("status")
@_handle_errors
def task_agent_status(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name; omit for the roster"),
    parent: str = typer.Option("", "--parent", hidden=True, help="Scope the roster to this master's fleet (set by the host broker)"),
):
    """Roster of every task-agent on this host, or one agent in detail."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if name:
        print_task_agent_detail(cfg, resolve_task_agent(cfg, name))
        return
    print_task_agent_roster(task_agent_rows(cfg, parent))


@task_agent_app.command("logs")
@_handle_errors
def task_agent_logs(
    name: str = typer.Argument(..., help="Task slug or container name"),
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Tail a task-agent's supervisor log (state + cost), NOT its conversation."""
    cfg = Config.from_env()
    setup_logging(cfg)
    require_docker()
    resolved = resolve_task_agent(cfg, name)
    if docker_task_agent_status(resolved) == "absent":
        typer.echo(
            f"Error: container {resolved} is gone, so its log is gone with it. "
            f"The conversation survives: cld task-agent transcript {name}",
            err=True,
        )
        raise typer.Exit(1)
    _docker_logs(resolved, tail)


@task_agent_app.command("transcript")
@_handle_errors
def task_agent_transcript(
    name: str = typer.Argument(..., help="Task slug or container name"),
):
    """Print the mailbox conversation: what the agent received and what it sent.

    Reads the mailbox, so it keeps working after a reap -- the container's log does not.
    """
    cfg = Config.from_env()
    setup_logging(cfg)
    print_task_agent_transcript(cfg, resolve_task_agent(cfg, name))


def _assert_reap_ready(cfg: Config, name: str, *, parent: str) -> None:
    """The three reap-readiness checks (§7). Raises RuntimeError naming the refusal.

    Ordered so the one that *waits* runs last -- no point spending
    ``_REAP_WAIT_SECONDS`` on an in-flight turn only to refuse for another reason.
    All three are filesystem or label reads; there is deliberately **no** store-side
    "did the squash happen" test, because that is §9's verification and the master
    already did it before asking (D2b).
    """
    root = mailbox_root(cfg)

    # 3. Own fleet only. An empty *parent* is the human on the host, who has full
    # authority; only a master-initiated reap (via the broker) passes a value.
    if parent:
        owner = task_agent_parent(cfg, name)
        if owner != parent:
            raise RuntimeError(
                f"refusing to reap {name}: its parent master is {owner or '<none>'}, "
                f"not {parent}. A master reaps only its own fleet."
            )

    # 2. Not a live peer. Reaping mid-exchange silently breaks the dependent's
    # exactly-one-reply guarantee, which the dead supervisor can no longer honor.
    running = {c["name"] for c in docker_task_agent_list(running_only=True)}
    dependents = sorted(
        m["name"] for m in mailbox.list_fleet(root)
        if m["name"] != name and m["name"] in running and name in (m.get("peers") or {})
    )
    if dependents:
        raise RuntimeError(
            f"refusing to reap {name}: it is a live peer of {', '.join(dependents)}. "
            "Reap them first, or wait for the exchange to land."
        )

    # 1. Not mid-turn. The work itself survives (watchman snapshots), but the reply
    # its sender is waiting for does not.
    deadline = time.time() + _REAP_WAIT_SECONDS
    while True:
        state = mailbox.read_state(root, name) or {}
        if state.get("phase") != "processing":
            return
        if time.time() >= deadline:
            current = state.get("current") or {}
            raise RuntimeError(
                f"refusing to reap {name}: it is mid-turn on '{current.get('subject')}' "
                f"from {current.get('from')} (since {current.get('started_at')}). "
                "Wait for it to reply, or override with --force."
            )
        time.sleep(1)


def _task_agent_repo_root(cfg: Config, name: str) -> str:
    """Host path of the repo a task-agent runs against; "" when it can't be determined.

    Normally the container's own label. Once the container is gone there is no label
    left (``state.json``'s ``repo_root`` is the in-container path), so fall back to
    the cwd's repo: forgetting a bookmark that isn't there is a no-op, so a wrong
    guess is harmless, and the mailbox archive happens either way.
    """
    rec = task_agent_record(cfg, name)
    if rec:
        return rec["repo_root"]
    try:
        return str(find_target_repo(cfg))
    except RuntimeError:
        return ""


def _reap_task_agent(cfg: Config, name: str, *, parent: str, force: bool) -> None:
    """Stop, remove, then the caller-side cleanup (D22). Every step idempotent."""
    if not force:
        _assert_reap_ready(cfg, name, parent=parent)
    repo_root = _task_agent_repo_root(cfg, name)
    _stop_and_remove_container(name)
    # The session bookmark, never the deliverable branch: different strings with
    # different lifetimes (D8). Caller-side because the supervisor is normally
    # SIGKILLed mid-turn, so its own last act doesn't run (D22).
    if repo_root:
        _forget_session_state(repo_root, name)
    else:
        log.warning(
            "could not determine the repo for %s, so its session bookmark stays in place. "
            "Recover with: cd <repo> && jj bookmark forget %s && jj workspace forget %s",
            name, name, name,
        )
    mailbox.archive_mailbox(mailbox_root(cfg), name)
    typer.echo(f"Reaped task-agent: {name}")


def _reap_all_task_agents(cfg: Config, *, parent: str, force: bool) -> None:
    """Reap every task-agent, repeating while any pass makes progress.

    Check 2 is order-dependent: with an A->B peer edge, reaping B is refused while A
    lives but allowed once A is gone, so one arbitrary-order pass would strand B. A
    mutual A<->B pair still refuses both -- correctly; that is what --force is for.
    Mailboxes whose container is already gone are swept too, since they are the
    orphans this is meant to clear.
    """
    # Label first, meta.json for names whose container is already gone -- same
    # precedence as task_agent_parent, resolved once for the whole sweep.
    owners = {c["name"]: c["parent"] for c in docker_task_agent_list()}
    for m in mailbox.list_fleet(mailbox_root(cfg)):
        owners.setdefault(m["name"], m.get("parent", ""))
    targets = [n for n, owner in owners.items() if not parent or owner == parent]
    if not targets:
        typer.echo("No task-agents found.")
        return

    remaining = sorted(targets)
    refused: dict[str, str] = {}
    while remaining:
        stuck: list[str] = []
        refused = {}
        for name in remaining:
            try:
                _reap_task_agent(cfg, name, parent=parent, force=force)
            except RuntimeError as e:
                stuck.append(name)
                refused[name] = str(e)
        if len(stuck) == len(remaining):
            break
        remaining = stuck

    for name in remaining:
        log.error("%s", refused[name])
    if remaining:
        typer.echo(
            f"{len(remaining)} task-agent(s) refused; add --force to override.", err=True
        )
        raise typer.Exit(1)


@task_agent_app.command("shutdown")
@_handle_errors
def task_agent_shutdown(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name"),
    all_: bool = typer.Option(False, "--all", help="Reap every task-agent on this host"),
    force: bool = typer.Option(False, "--force", help="Override a reap-readiness refusal (host-only)"),
    parent: str = typer.Option("", "--parent", hidden=True, help="Restrict to this master's fleet (set by the host broker)"),
):
    """Stop and remove a task-agent, forget its session bookmark, archive its mailbox."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if all_ == bool(name):
        typer.echo("Error: pass a task slug/container name, or --all -- not both", err=True)
        raise typer.Exit(1)
    require_docker()
    if name:
        _reap_task_agent(cfg, resolve_task_agent(cfg, name), parent=parent, force=force)
        return
    _reap_all_task_agents(cfg, parent=parent, force=force)


# --- Mailbox messaging (shared with the container CLI) ------------------------
app.add_typer(msg_app, name="msg")


# --- Chat bridges -------------------------------------------------------------
bridge_app = typer.Typer(
    help="Chat bridges over the mailbox transport (host-only).",
    no_args_is_help=True,
)
app.add_typer(bridge_app, name="bridge")


@bridge_app.command("mattermost")
@_handle_errors
def bridge_mattermost(
    once: bool = typer.Option(False, "--once", help="Run a single tick and exit"),
):
    """Carry a private Mattermost channel to and from the fleet's mailboxes (foreground)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    run_bridge(cfg, once=once)


@bridge_app.command("start")
@_handle_errors
def bridge_start():
    """Start the bridge detached, logging to ~/.cld/bridge/mattermost.log."""
    cfg = Config.from_env()
    setup_logging(cfg)
    # Validate here, in the caller's terminal: a bad token or channel should not
    # be something you discover later by reading a log file.
    build_bridge(cfg)
    typer.echo(f"started (pid {bridge_daemon.start()}), logging to {bridge_daemon.log_file()}")


@bridge_app.command("stop")
@_handle_errors
def bridge_stop():
    """Stop the detached bridge."""
    pid = bridge_daemon.stop()
    typer.echo(f"stopped (pid {pid})" if pid else "not running")


@bridge_app.command("restart")
@_handle_errors
def bridge_restart():
    """Stop and start the detached bridge."""
    bridge_daemon.stop()
    cfg = Config.from_env()
    setup_logging(cfg)
    build_bridge(cfg)
    typer.echo(f"started (pid {bridge_daemon.start()})")


@bridge_app.command("status")
@_handle_errors
def bridge_status():
    """Whether the bridge is up, and where its channel and mailbox are."""
    cfg = Config.from_env()
    pid = bridge_daemon.running_pid()
    typer.echo(f"running (pid {pid})" if pid else "stopped")
    typer.echo(f"  channel: {cfg.mattermost_channel_id or '<unset>'}")
    typer.echo(f"  mailbox: {Path(cfg.mailbox_root).expanduser() / 'mattermost'}")
    typer.echo(f"  log:     {bridge_daemon.log_file()}")


@bridge_app.command("logs")
@_handle_errors
def bridge_logs(tail: int = typer.Option(40, "-n", "--tail", help="Number of lines to show")):
    """Tail the detached bridge's log."""
    typer.echo(bridge_daemon.tail_log(tail))


@app.command()
@_handle_errors
def build(no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild without cache")):
    """Build base, devcontainer, and run images (base first)."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("build: no_cache=%s", no_cache)
    cld_root = Path(__file__).resolve().parent.parent
    ensure_image(
        cfg.base_image,
        cld_root / "imgs/claude-base/Dockerfile.claude-base",
        cld_root,
        extra_paths=base_extra_paths(cld_root),
        force=True, no_cache=no_cache,
    )
    ensure_image(
        cfg.devcontainer_image,
        cld_root / "imgs/claude-devcontainer/Dockerfile.claude-devcontainer",
        cld_root,
        extra_paths=devcontainer_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        force=True, no_cache=no_cache,
    )
    ensure_image(
        cfg.run_image,
        cld_root / "imgs/claude-run/Dockerfile.claude-run",
        cld_root / "imgs/claude-run",
        extra_paths=run_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        force=True, no_cache=no_cache,
    )


@app.command()
def prompts():
    """List available prompt templates with descriptions."""
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("prompts list")
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    if not prompts_dir.exists():
        typer.echo("No prompts directory found.", err=True)
        raise typer.Exit(1)

    items = list_prompt_items(prompts_dir)
    if not items:
        typer.echo("No prompts found.")
        return

    width = max(len(name) for name, _ in items)
    for name, desc in items:
        typer.echo(f"  {name:<{width}}  {desc}")


chain_app = typer.Typer(help="Multi-agent chain orchestrator.")
app.add_typer(chain_app, name="chain")


@chain_app.command("run")
@_handle_errors
def chain_run(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs for the initial task, in order: @<ref> or a file path"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline initial task description, appended after the refs"),
    name: str = typer.Option("", "-n", "--name", help="Chain session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Override default model"),
    revision: str = typer.Option("", "-r", "--revision", help="Starting revision"),
    no_detach: bool = typer.Option(
        False, "--no-detach", "--foreground",
        help="Run synchronously in the foreground (do not detach).",
    ),
):
    """Run a multi-agent chain end to end (detached by default)."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    cld_root = Path(__file__).resolve().parent.parent
    brief, _ = _compose_from_args(refs or [], prompt, find_repo_root(), cld_root)

    log.info(
        "chain run: file=%s, name=%s, model=%s, no_detach=%s",
        chain_file, name or "<auto>", model or "<default>", no_detach,
    )

    if no_detach:
        if model:
            os.environ["CLD_CHAIN_DEFAULT_MODEL"] = model
            cfg = Config.from_env()
        result = run_chain(
            cfg, chain_path, initial_task=brief,
            revision=revision, name_suffix=name,
        )
        print_chain_report(result, get_backend())
        raise typer.Exit(0 if result.success else 1)

    # --- Detached mode ---
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)
    chain = apply_name_override(chain, name)

    # Pin the anchor in the foreground (like `cld agent`) so it tracks where the
    # user is at invocation, not where the detached child happens to boot.
    # Resolve before GC so a bad -r errors without disturbing any prior archive.
    anchor_hash = resolve_anchor(get_backend(), revision)
    log.info("Chain anchor: %s", anchor_hash[:12])

    state_dir = chain_state_dir(repo_root, chain.name)
    _gc_or_refuse(state_dir, chain.name)
    state_dir.mkdir(parents=True, exist_ok=True)

    initial_state = ChainState(
        schema_version=1,
        kind="chain",
        chain_name=chain.name,
        chain_session=f"chain_{chain.name}",
        chain_branch=f"chain_{chain.name}",
        chain_file=str(chain_path.resolve()),
        anchor_hash=anchor_hash,
        pid=0,
        started_at=_utcnow_iso(),
        finished_at=None,
        log_file=str(state_dir / "chain.log"),
        status="running",
        total_steps=len(chain.steps),
        current_index=0,
        current_kind="pending",
        current_step_name="",
        current_step_sessions=[],
        completed_steps=[],
        total_cost_usd=0.0,
        failure_reason="",
        inputs={
            "brief": brief,
            "revision": revision,
            "model": model,
            "name": name,
        },
    )
    write_state(state_dir / "state.json", initial_state.to_dict())

    child_env = os.environ.copy()
    if model:
        child_env["CLD_CHAIN_DEFAULT_MODEL"] = model

    pid = _spawn_chain_runner(state_dir, repo_root, child_env)
    (state_dir / "pid").write_text(f"{pid}\n")

    # Record the child pid immediately so `cld chain status` doesn't briefly
    # report the chain as stale (pid 0) during the window before the child boots.
    initial_state.pid = pid
    write_state(state_dir / "state.json", initial_state.to_dict())

    _print_chain_launch_banner(chain.name, pid, state_dir)


def _spawn_chain_runner(state_dir: Path, repo_root: Path, child_env: dict) -> int:
    """Detach the background chain runner; return its pid."""
    log_fh = open(state_dir / "chain.log", "ab", buffering=0)
    child = subprocess.Popen(
        [sys.executable, "-m", "cld", "chain", "_chain-runner", str(state_dir)],
        stdout=log_fh, stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(repo_root),
        env=child_env,
    )
    log_fh.close()
    return child.pid


@chain_app.command("_chain-runner", hidden=True)
def _chain_runner(
    state_dir: str = typer.Argument(...),
):
    """Internal: execute a previously-staged chain run."""
    state_dir_path = Path(state_dir)
    state = ChainState.load(state_dir_path / "state.json")

    cfg = Config.from_env()
    setup_logging(cfg)
    writer = StateWriter(state_dir_path / "state.json", state)

    writer.update(pid=os.getpid())

    model_override = state.inputs.get("model", "")
    if model_override:
        os.environ["CLD_CHAIN_DEFAULT_MODEL"] = model_override
        cfg = Config.from_env()

    try:
        result = run_chain(
            cfg,
            Path(state.chain_file),
            initial_task=state.inputs.get("brief", ""),
            revision=state.inputs.get("revision", ""),
            name_suffix=state.inputs.get("name", ""),
            state_writer=writer,
            anchor_hash=state.anchor_hash,
        )
        final_status = "success" if result.success else "failed"
        reason = result.failure_reason if not result.success else ""
        try:
            writer.mark_finished(final_status, reason=reason)
        except Exception as write_err:
            log.error("Failed to write final chain state: %s", write_err)
        print_chain_report(result, get_backend())
        raise typer.Exit(0 if result.success else 1)
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        writer.mark_finished("interrupted", reason="SIGINT")
        raise
    except BaseException as e:
        writer.mark_finished("failed", reason=f"{type(e).__name__}: {e}")
        raise


@chain_app.command("validate")
@_handle_errors
def chain_validate(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
):
    """Parse and validate a chain file. Exits 0 on success, 1 on any error."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain validate: %s", chain_file)
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)
    typer.echo(f"OK: '{chain.name}' has {len(chain.steps)} top-level step(s)")


@chain_app.command("list")
@_handle_errors
def chain_list():
    """List available chain definitions."""
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain list")
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent

    seen: set[str] = set()
    items: list[tuple[str, str, str]] = []  # (source, name, description)

    for source, base in (("workspace", repo_root), ("builtin", cld_root)):
        chains_dir = base / "chains"
        if not chains_dir.is_dir():
            continue
        for path in sorted(chains_dir.glob("*.y*ml")):
            try:
                chain = load_chain(path)
            except ValueError as e:
                items.append((source, path.name, f"[invalid: {e}]"))
                continue
            if chain.name in seen:
                continue
            seen.add(chain.name)
            items.append((source, chain.name, chain.description or ""))

    if not items:
        typer.echo("No chains found.")
        return
    name_w = max(len(it[1]) for it in items)
    src_w = max(len(it[0]) for it in items)
    for src, name, desc in items:
        typer.echo(f"  [{src:<{src_w}}]  {name:<{name_w}}  {desc}")


@chain_app.command("dry-run")
@_handle_errors
def chain_dry_run(
    chain_file: str = typer.Argument(..., help="Path to chain YAML file or @<name>"),
):
    """Print the execution plan without launching agents."""
    chain_path = _resolve_chain_path(chain_file)
    if not chain_path.is_file():
        typer.echo(f"Error: chain file not found: {chain_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)
    log.info("chain dry-run: %s", chain_file)
    repo_root = find_repo_root()
    cld_root = Path(__file__).resolve().parent.parent
    chain = load_chain(chain_path)
    validate_chain(chain, repo_root, cld_root)

    typer.echo(f"Chain: {chain.name}")
    if chain.description:
        typer.echo(f"  {chain.description}")
    typer.echo(f"  Default model: {chain.defaults.model}")
    typer.echo()
    typer.echo(f"Plan ({len(chain.steps)} top-level item{'s' if len(chain.steps) != 1 else ''}):")
    for i, item in enumerate(chain.steps, start=1):
        if isinstance(item, ParallelGroup):
            typer.echo(f"  {i}. PARALLEL ({len(item.siblings)} sibling(s)):")
            for s in item.siblings:
                model = s.model or chain.defaults.model
                typer.echo(f"       - {s.name} (persona={s.persona}, model={model})")
        else:
            model = item.model or chain.defaults.model
            typer.echo(f"  {i}. {item.name} (persona={item.persona}, model={model})")


@chain_app.command("status")
@_handle_errors
def chain_status(
    all_: bool = typer.Option(False, "-a", "--all", help="Include terminated chains."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
    watch: float = typer.Option(0.0, "-w", "--watch", help="Refresh every N seconds; 0 = single shot."),
):
    """List status of running (and optionally all) chains in this repo."""
    _render_status(all_, json_out, watch)


@chain_app.command("ps", hidden=True)
@_handle_errors
def chain_ps(
    all_: bool = typer.Option(False, "-a", "--all", help="Include terminated chains."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON."),
    watch: float = typer.Option(0.0, "-w", "--watch", help="Refresh every N seconds; 0 = single shot."),
):
    """Alias for 'cld chain status'."""
    _render_status(all_, json_out, watch)


def _render_status(all_: bool, json_out: bool, watch: float) -> None:
    cfg = Config.from_env()
    setup_logging(cfg)
    repo_root = find_repo_root()
    chains_dir = repo_root / ".cld" / "chains"

    def render_once() -> None:
        rows = _collect_chain_rows(chains_dir, include_terminal=all_)
        if json_out:
            typer.echo(json.dumps(rows, indent=2))
            return
        _print_status_table(rows)

    if not watch:
        render_once()
        return

    while True:
        if sys.stdout.isatty():
            os.system("clear")
        render_once()
        time.sleep(watch)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _collect_chain_rows(chains_dir: Path, include_terminal: bool) -> list[dict]:
    rows = []
    if not chains_dir.is_dir():
        return rows
    for state_file in sorted(chains_dir.glob("*/state.json")):
        if state_file.parent.name.endswith(".prev"):
            continue  # archived previous run, not a current chain
        try:
            state = ChainState.load(state_file)
        except Exception:
            continue
        display_status = state.status
        if state.status == "running" and not _is_pid_alive(state.pid):
            display_status = "stale"
        if not include_terminal and display_status not in ("running", "stale"):
            continue
        started_ago = format_age(state.started_at)
        stage = f"{state.current_index + 1}/{state.total_steps}" if state.status == "running" else "-"
        rows.append({
            "name": state.chain_name,
            "stage": stage,
            "current": state.current_step_name or "-",
            "status": display_status,
            "started": started_ago,
            "cost_usd": state.total_cost_usd,
            "pid": state.pid,
            "log_file": state.log_file,
        })
    return rows


def _print_status_table(rows: list[dict]) -> None:
    if not rows:
        typer.echo("No chains found.")
        return
    headers = ("NAME", "STAGE", "CURRENT", "STATUS", "STARTED", "COST")
    name_w = max(len(headers[0]), *(len(r["name"]) for r in rows))
    stage_w = max(len(headers[1]), *(len(r["stage"]) for r in rows))
    cur_w = max(len(headers[2]), *(len(r["current"]) for r in rows))
    status_w = max(len(headers[3]), *(len(r["status"]) for r in rows))
    started_w = max(len(headers[4]), *(len(r["started"]) for r in rows))
    typer.echo(
        f"{'NAME':<{name_w}}  {'STAGE':<{stage_w}}  {'CURRENT':<{cur_w}}  "
        f"{'STATUS':<{status_w}}  {'STARTED':<{started_w}}  COST"
    )
    for r in rows:
        typer.echo(
            f"{r['name']:<{name_w}}  {r['stage']:<{stage_w}}  {r['current']:<{cur_w}}  "
            f"{r['status']:<{status_w}}  {r['started']:<{started_w}}  ${r['cost_usd']:.2f}"
        )


def _gc_or_refuse(state_dir: Path, chain_name: str) -> None:
    """Handle an existing state dir: refuse if running+alive, GC if terminal/stale."""
    state_file = state_dir / "state.json"
    if not state_file.exists():
        return
    try:
        state = ChainState.load(state_file)
    except Exception:
        # Corrupt state: rename and proceed
        _rename_to_prev(state_dir)
        return
    if state.status == "running" and _is_pid_alive(state.pid):
        raise RuntimeError(
            f"Chain '{chain_name}' is already running (pid {state.pid}). "
            f"Use 'cld chain status' to inspect or kill it first."
        )
    _rename_to_prev(state_dir)


def _rename_to_prev(state_dir: Path) -> None:
    prev = state_dir.parent / (state_dir.name + ".prev")
    if prev.exists():
        shutil.rmtree(prev)
    state_dir.rename(prev)


def _print_chain_launch_banner(chain_name: str, pid: int, state_dir: Path) -> None:
    log_file = state_dir / "chain.log"
    typer.echo(f"\nChain '{chain_name}' detached.")
    typer.echo(f"  Runner PID:       {pid}")
    typer.echo(f"  Log file:         {log_file}")
    typer.echo(f"  Follow progress:  tail -f {log_file}")
    typer.echo(f"  Status:           cld chain status")
    typer.echo(f"  Wait:             while kill -0 {pid} 2>/dev/null; do sleep 5; done")


def _resolve_chain_path(arg: str) -> Path:
    """Resolve `@name` shortcut or treat as relative/absolute path."""
    if arg.startswith("@"):
        short = arg[1:]
        if not short.endswith((".yaml", ".yml")):
            short = f"{short}.yaml"
        repo_root = find_repo_root()
        cld_root = Path(__file__).resolve().parent.parent
        for base in (repo_root, cld_root):
            cand = base / "chains" / short
            if cand.is_file():
                return cand
        return repo_root / "chains" / short
    return Path(arg)


if __name__ == "__main__":
    app()
