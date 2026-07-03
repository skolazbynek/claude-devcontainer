"""CLI entry point for cld."""

import calendar
import functools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from cld.agent import agent_workspace_path, launch_agent, launch_review
from cld.chain import ParallelGroup, apply_name_override, chain_state_dir, load_chain, print_chain_report, run_chain, validate_chain
from cld.chain_state import ChainState, StateWriter, write_state, _utcnow_iso
from cld.config import Config
from cld.docker import (
    agent_container_name,
    agent_extra_paths,
    base_extra_paths,
    build_container_args,
    build_session_name,
    devcontainer_extra_paths,
    docker_agent_list,
    docker_agent_status,
    docker_master_list,
    docker_master_status,
    ensure_image,
    find_repo_context,
    find_repo_root,
    master_container_name,
    require_docker,
    stage_home_ro,
    stage_ssh_agent,
    to_host_path,
)
from cld.log import get_logger, setup_logging
from cld.prompts import resolve_prompt_ref
from cld.loop import run_loop
from cld.vcs import get_backend
from cld.vcs.anchor import create_editable_root, read_workspace_anchor, resolve_anchor

log = get_logger(__name__)

app = typer.Typer()


def _handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (RuntimeError, ValueError, subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error("Command failed: %s", e)
            log.debug("traceback:", exc_info=True)
            raise typer.Exit(1)
    return wrapper


def _version_callback(value: bool):
    if value:
        from cld import __version__
        typer.echo(f"cld {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit"),
):
    if ctx.invoked_subcommand is None:
        _run_devcontainer(None, "", "", "", "", None, False, False)


@app.command()
@_handle_errors
def agent(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file, or @<name> to resolve from prompts/"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (appended to task file if both given)"),
):
    """Launch an autonomous Claude agent."""
    task_path: Path | None = None

    if task_file and task_file.startswith("@"):
        cld_root = Path(__file__).resolve().parent.parent
        repo_root = find_repo_root()
        try:
            task_path = resolve_prompt_ref(task_file[1:], repo_root, cld_root)
        except (FileNotFoundError, ValueError) as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)
        log.info("resolved @%s -> %s", task_file[1:], task_path)
    elif task_file:
        task_path = Path(task_file)
        if not task_path.is_file():
            typer.echo(f"Error: Task file not found: {task_file}", err=True)
            raise typer.Exit(1)

    if not task_path and not prompt:
        typer.echo("Error: Provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)

    cfg = Config.from_env()
    setup_logging(cfg)
    log.info(
        "agent: name=%s, model=%s, revision=%s, task_file=%s, prompt=%s",
        name or "<auto>",
        model or "<default>",
        revision or "<default>",
        str(task_path) if task_path else "<none>",
        "<provided>" if prompt else "<none>",
    )

    launch_agent(
        cfg,
        task_file=task_path,
        inline_prompt=prompt or None,
        name=name,
        model=model,
        revision=revision,
    )



def _run_devcontainer(
    task_file: str | None,
    name: str,
    model: str,
    revision: str,
    prompt: str,
    extra_args: list[str] | None,
    master: bool,
    agent: bool,
) -> None:
    """Core devcontainer launch logic shared by the callback and main fallback."""
    require_docker()
    if master and agent:
        typer.echo("Error: --master and --agent are mutually exclusive", err=True)
        raise typer.Exit(1)
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: Task file not found: {task_file}", err=True)
        raise typer.Exit(1)
    cfg = Config.from_env()
    setup_logging(cfg)

    if master:
        _run_persistent_devcontainer("master", task_path, name, model, revision, prompt, cfg)
        return
    if agent:
        _run_persistent_devcontainer("agent", task_path, name, model, revision, prompt, cfg)
        return

    log.info(
        "devcontainer: name=%s, model=%s, revision=%s, task_file=%s, prompt=%s",
        name or "<auto>",
        model or "<default>",
        revision or "<default>",
        str(task_path) if task_path else "<none>",
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

    repo_root, _workspace_rev = find_repo_context()
    session = build_session_name("cld", name)

    vcs = get_backend()
    anchor_hash = resolve_anchor(vcs, revision)
    ws_path = agent_workspace_path(repo_root, session)
    create_editable_root(vcs, anchor_hash, ws_path, session)
    log.info(f"Anchor: {anchor_hash[:12]}")

    args = build_container_args(repo_root, session, cfg, interactive=True)
    host_ws = to_host_path(str(ws_path), cfg)
    args += ["-v", f"{host_ws}:/workspace/current"]
    args += ["-e", "WORKSPACE_PREINITIALIZED=1"]
    args += ["-e", f"AGENT_ANCHOR_HASH={anchor_hash}"]
    if task_path:
        host_task = to_host_path(str(task_path.resolve()), cfg)
        args += ["-v", f"{host_task}:/config/task.md:ro"]
    if prompt:
        args += ["-e", f"AGENT_INLINE_PROMPT={prompt}"]
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
    if extra_args:
        args += extra_args

    log.info("Starting Claude Code in container...")
    print()

    os.execvp("docker", ["docker", "run"] + args)


def _is_workspace_valid(vcs, session: str, ws_path: Path) -> bool:
    """Return True if *ws_path* is currently registered as a workspace in the VCS."""
    if vcs.name == "jj":
        result = vcs.run([
            "--no-pager", "workspace", "list", "--color=never",
            "--ignore-working-copy", "-T", 'name ++ "\\n"',
        ])
        if result.returncode != 0:
            return False
        return session in result.stdout.strip().splitlines()
    else:
        result = vcs.run(["worktree", "list", "--porcelain"])
        if result.returncode != 0:
            return False
        ws_str = str(ws_path.resolve())
        return any(
            line.startswith("worktree ") and line[len("worktree "):].strip() == ws_str
            for line in result.stdout.splitlines()
        )


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
    task_path: Path | None,
    name: str,
    model: str,
    revision: str,
    prompt: str,
    cfg: Config,
) -> None:
    """Master/agent-mode devcontainer: start-or-attach the one persistent container per repo.

    Master attaches an interactive shell; the headless agent role never attaches
    (see docs/design-agent-messaging.md) -- it starts (or confirms it's running)
    and returns.
    """
    if name:
        typer.echo(f"Error: --{role} and -n/--name are mutually exclusive", err=True)
        raise typer.Exit(1)
    if role == "agent" and (task_path or prompt):
        typer.echo(
            "Error: --agent does not take a task file or -p/--prompt "
            "(the repo agent has no first-launch prompt; message it after it starts)",
            err=True,
        )
        raise typer.Exit(1)

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

    repo_root, _ = find_repo_context()
    session = _persistent_container_name(role, repo_root)
    status = _persistent_container_status(role, session)
    log.info("%s devcontainer: name=%s, status=%s", role, session, status)

    if status in ("running", "stopped"):
        if prompt or task_path:
            typer.echo(
                f"Error: --{role} re-attach: -p/--prompt and task_file cannot be used "
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

    # absent — create a new persistent container
    ws_path = agent_workspace_path(repo_root, session)
    vcs = get_backend()

    if ws_path.exists():
        if _is_workspace_valid(vcs, session, ws_path):
            log.info("Orphan workspace found; reusing: %s", ws_path)
            recorded = read_workspace_anchor(repo_root, session)
            if recorded:
                anchor_hash = recorded
                log.info("Restored recorded anchor: %s", anchor_hash[:12])
            else:
                anchor_hash = resolve_anchor(vcs, revision)
                log.warning(
                    "No anchor record for existing workspace; resolving from current @ (may drift): %s",
                    anchor_hash[:12],
                )
        else:
            log.warning("Orphan workspace at %s is stale; removing and recreating", ws_path)
            try:
                vcs.forget_workspace(session, str(ws_path))
            except Exception as e:
                log.warning("Could not deregister stale workspace: %s", e)
            shutil.rmtree(ws_path, ignore_errors=True)
            recorded = read_workspace_anchor(repo_root, session)
            anchor_hash = recorded or resolve_anchor(vcs, revision)
            if recorded:
                log.info("Restored recorded anchor for recreated workspace: %s", anchor_hash[:12])
            create_editable_root(vcs, anchor_hash, ws_path, session)
    else:
        anchor_hash = resolve_anchor(vcs, revision)
        create_editable_root(vcs, anchor_hash, ws_path, session)

    log.info("Anchor: %s", anchor_hash[:12])

    args = build_container_args(
        repo_root, session, cfg, interactive=False,
        master=(role == "master"), agent=(role == "agent"),
    )
    host_ws = to_host_path(str(ws_path), cfg)
    args += ["-v", f"{host_ws}:/workspace/current"]
    args += ["-e", "WORKSPACE_PREINITIALIZED=1"]
    args += ["-e", f"AGENT_ANCHOR_HASH={anchor_hash}"]
    if task_path:
        host_task = to_host_path(str(task_path.resolve()), cfg)
        args += ["-v", f"{host_task}:/config/task.md:ro"]
    if prompt:
        args += ["-e", f"AGENT_INLINE_PROMPT={prompt}"]
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
    typer.echo("  Status: cld devcontainer status --agent")
    typer.echo("  Logs:   cld devcontainer logs --agent")
    typer.echo(f"  Send:   messenger MCP send(to=\"{repo_root.name}\", ...) from another container")


dc_app = typer.Typer(help="Launch or manage the interactive devcontainer.")
app.add_typer(
    dc_app,
    name="devcontainer",
    invoke_without_command=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


@dc_app.callback(invoke_without_command=True)
@_handle_errors
def devcontainer(
    ctx: typer.Context,
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (appended to task file if both given)"),
    master: bool = typer.Option(False, "--master", help="Start or attach to the persistent master devcontainer for this repo"),
    agent: bool = typer.Option(False, "--agent", help="Start the persistent headless repo agent for this repo (see docs/design-agent-messaging.md)"),
):
    """Launch an interactive Claude devcontainer (or manage the master/agent persistent containers)."""
    if ctx.invoked_subcommand is not None:
        return
    # Positional args (task_file, extra docker args) come in via ctx.args
    # because the group uses allow_extra_args=True.
    remaining = list(ctx.args)
    task_file = remaining[0] if remaining else None
    extra_args = remaining[1:] if len(remaining) > 1 else None
    _run_devcontainer(task_file, name, model, revision, prompt, extra_args, master, agent)


@dc_app.command("shutdown")
@_handle_errors
def devcontainer_shutdown(
    all_: bool = typer.Option(False, "--all", help="Stop all containers of this role on this host"),
    agent: bool = typer.Option(False, "--agent", help="Target the repo agent instead of the master"),
):
    """Stop and remove the master (or agent) devcontainer for the current repo (or all with --all)."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    role = "agent" if agent else "master"

    if all_:
        containers = docker_agent_list() if agent else docker_master_list()
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

    repo_root, _ = find_repo_context()
    container_name = _persistent_container_name(role, repo_root)
    if _persistent_container_status(role, container_name) == "absent":
        typer.echo(f"No {role} container found for this repo.")
        return
    if not _shutdown_persistent_container(role, container_name, str(repo_root), container_name):
        raise typer.Exit(1)


@dc_app.command("restart")
@_handle_errors
def devcontainer_restart(
    agent: bool = typer.Option(False, "--agent", help="Restart the repo agent instead of the master"),
):
    """Restart the master (or agent) devcontainer for this repo, picking up cld image/code changes.

    Stops and removes the existing container, then relaunches. The jj/git
    workspace is preserved along with all uncommitted work; only in-container
    state (installed packages, shell history, running processes) is wiped.
    """
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    role = "agent" if agent else "master"

    repo_root, _ = find_repo_context()
    container_name = _persistent_container_name(role, repo_root)
    if _persistent_container_status(role, container_name) == "absent":
        typer.echo(
            f"No {role} container to restart. Start one with: cld devcontainer --{role}",
            err=True,
        )
        raise typer.Exit(1)

    _stop_and_remove_container(container_name)
    _run_persistent_devcontainer(role, None, "", "", "", "", cfg)


@dc_app.command("status")
@_handle_errors
def devcontainer_status(
    agent: bool = typer.Option(False, "--agent", help="Report the repo agent instead of the master"),
):
    """Print status of the persistent master or agent devcontainer for this repo."""
    cfg = Config.from_env()
    setup_logging(cfg)
    role = "agent" if agent else "master"

    repo_root, _ = find_repo_context()
    container_name = _persistent_container_name(role, repo_root)
    docker_status = _persistent_container_status(role, container_name)
    typer.echo(f"{role.capitalize()}: {container_name}")
    typer.echo(f"  Container: {docker_status}")

    if role != "agent":
        return

    state_path = Path(cfg.mailbox_root).expanduser() / container_name / "state.json"
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


@dc_app.command("logs")
@_handle_errors
def devcontainer_logs(
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
    agent: bool = typer.Option(False, "--agent", help="Target the repo agent instead of the master"),
):
    """Tail the master or agent container's log output."""
    require_docker()
    cfg = Config.from_env()
    setup_logging(cfg)
    role = "agent" if agent else "master"

    repo_root, _ = find_repo_context()
    container_name = _persistent_container_name(role, repo_root)
    if _persistent_container_status(role, container_name) == "absent":
        typer.echo(f"No {role} container found for this repo.", err=True)
        raise typer.Exit(1)

    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container_name],
        capture_output=True, text=True,
    )
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, nl=False, err=True)


def _stop_and_remove_container(name: str) -> None:
    """Stop and remove a container. Idempotent."""
    log.info("Stopping container: %s", name)
    subprocess.run(["docker", "stop", name], capture_output=True)
    subprocess.run(["docker", "rm", name], capture_output=True)


def _shutdown_persistent_container(role: str, name: str, repo_root_str: str, session: str) -> bool:
    """Stop, remove, and clean up the workspace for a master/agent container. Returns True on success."""
    _stop_and_remove_container(name)

    repo_root = Path(repo_root_str)
    ws_path = agent_workspace_path(repo_root, session)
    success = True

    if repo_root.exists():
        try:
            vcs = get_backend(repo_root)
            vcs.forget_workspace(session, str(ws_path))
        except Exception as e:
            log.warning("Failed to deregister workspace %s: %s", session, e)
            success = False

    if ws_path.exists():
        shutil.rmtree(ws_path, ignore_errors=True)

    anchor_record = repo_root / ".cld" / "anchors" / session
    if anchor_record.exists():
        anchor_record.unlink()

    typer.echo(f"Stopped and removed {role} container: {name}")
    return success


@app.command()
@_handle_errors
def review(
    feature_branch: str = typer.Argument(help="Feature branch to review"),
    trunk_branch: Optional[str] = typer.Argument(default=None, help="Trunk branch to diff against (auto-detected if omitted)"),
    name: str = typer.Option("", "-n", "--name", help="Session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Claude model"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change -- @ for jj, HEAD for git)"),
):
    """Launch a code review agent."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if trunk_branch is None:
        branches = get_backend().list_branches()
        branch_names = {
            line.strip().lstrip("* ").split(":")[0].split()[0]
            for line in branches.splitlines()
            if line.strip()
        }
        for candidate in cfg.trunk_candidates:
            if candidate in branch_names:
                trunk_branch = candidate
                break
        if trunk_branch is None:
            raise RuntimeError(f"Could not auto-detect trunk branch; none of {list(cfg.trunk_candidates)} found. Pass it explicitly.")
    log.info(
        "review: feature=%s, trunk=%s, model=%s",
        feature_branch,
        trunk_branch or "<auto>",
        model or "<default>",
    )
    launch_review(cfg, feature_branch, trunk_branch, name=name, model=model, revision=revision)


@app.command()
@_handle_errors
def loop(
    task_file: Optional[str] = typer.Argument(None, help="Path to task markdown file"),
    name: str = typer.Option("", "-n", "--name", help="Loop session name suffix"),
    model: str = typer.Option("", "-m", "--model", help="Model for implementer agent"),
    review_model: str = typer.Option("", "--review-model", help="Model for reviewer agent"),
    revision: str = typer.Option("", "-r", "--revision", help="Revision to base workspace on (default: last committed change -- @- for jj, HEAD for git)"),
    max_iterations: int = typer.Option(3, "--max-iterations", help="Hard cap on iterations; loop also stops early on a clean review (no critical/major findings)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline prompt (appended to task file if both given)"),
    approve: bool = typer.Option(False, "--approve", help="Pause after each review for approval (continue/stop/view/edit findings)"),
):
    """Run an automated implement-review loop.

    Each iteration runs an implementer agent, then a reviewer agent. Review
    findings feed into the next implementer. Stops on a clean review, hitting
    --max-iterations, or an agent failure. All iterations land on a single
    'loop_<name>' branch; the final report prints inspection/merge commands.
    """
    if not task_file and not prompt:
        typer.echo("Error: Provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)
    if max_iterations < 1:
        typer.echo("Error: --max-iterations must be at least 1", err=True)
        raise typer.Exit(1)
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: Task file not found: {task_file}", err=True)
        raise typer.Exit(1)

    cfg = Config.from_env()
    setup_logging(cfg)
    log.info(
        "loop: task_file=%s, prompt=%s, model=%s, review_model=%s, max_iterations=%d, approve=%s",
        str(task_path) if task_path else "<none>",
        "<provided>" if prompt else "<none>",
        model or "<default>",
        review_model or "<default>",
        max_iterations,
        approve,
    )
    run_loop(
        cfg,
        task_path,
        inline_prompt=prompt or None,
        name=name,
        model=model,
        review_model=review_model,
        revision=revision,
        max_iterations=max_iterations,
        approve=approve,
    )


@app.command()
@_handle_errors
def build(no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild without cache")):
    """Build base, devcontainer, and agent images (base first)."""
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
        cfg.agent_image,
        cld_root / "imgs/claude-agent/Dockerfile.claude-agent",
        cld_root / "imgs/claude-agent",
        extra_paths=agent_extra_paths(cld_root),
        parent_image=(
            cfg.base_image,
            cld_root / "imgs/claude-base/Dockerfile.claude-base",
            cld_root,
            base_extra_paths(cld_root),
        ),
        force=True, no_cache=no_cache,
    )


def _parse_description(path: Path) -> str:
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line[len("description:"):].strip()
    return ""


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

    items = []
    for path in sorted(prompts_dir.rglob("*.md")):
        rel = path.relative_to(prompts_dir).with_suffix("")
        desc = _parse_description(path)
        items.append((str(rel), desc))

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
    task_file: Optional[str] = typer.Argument(None, help="Initial task file"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline initial prompt"),
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
    task_path = Path(task_file) if task_file else None
    if task_path and not task_path.is_file():
        typer.echo(f"Error: task file not found: {task_file}", err=True)
        raise typer.Exit(1)
    if not task_path and not prompt:
        typer.echo("Error: provide a task file, --prompt, or both", err=True)
        raise typer.Exit(1)

    cfg = Config.from_env()
    setup_logging(cfg)
    log.info(
        "chain run: file=%s, name=%s, model=%s, no_detach=%s",
        chain_file, name or "<auto>", model or "<default>", no_detach,
    )

    if no_detach:
        if model:
            os.environ["CLD_CHAIN_DEFAULT_MODEL"] = model
            cfg = Config.from_env()
        initial = task_path.read_text() if task_path else None
        result = run_chain(
            cfg, chain_path,
            initial_task=initial, inline_prompt=prompt or None,
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
            "task_file": str(task_path.resolve()) if task_path else "",
            "inline_prompt": prompt,
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

    task_text: str | None = None
    task_file_path = state.inputs.get("task_file", "")
    if task_file_path:
        try:
            task_text = Path(task_file_path).read_text()
        except OSError as e:
            log.error("Could not read task file %s: %s", task_file_path, e)
            writer.mark_finished("failed", reason=f"Cannot read task file: {e}")
            raise typer.Exit(1)

    try:
        result = run_chain(
            cfg,
            Path(state.chain_file),
            initial_task=task_text,
            inline_prompt=state.inputs.get("inline_prompt") or None,
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
        started_ago = _format_age(state.started_at)
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


def _format_age(iso_ts: str) -> str:
    try:
        t = time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
        then = calendar.timegm(t)
        secs = int(time.time()) - then
    except Exception:
        return iso_ts
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


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
