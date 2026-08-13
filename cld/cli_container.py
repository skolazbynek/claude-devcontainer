"""Container-side CLI: the `cld` the devcontainer image installs.

Only verbs that work inside a container, each wired straight to the seam it uses
-- the host broker for anything needing a docker daemon, the bind-mounted mailbox
tree for anything needing a conversation. Host-only verbs are hidden stubs that
say so instead of failing obscurely. See docs/design-cli-split.md.
"""

import functools
import os
import subprocess
from pathlib import Path
from typing import Optional

import typer

from cld.config import Config
from cld.docker import find_target_repo
from cld.broker import broker_agent_op, broker_available, broker_task_agent_op, run_action
from cld.log import get_logger, setup_logging
from cld.messenger import agents as agents_cmd
from cld.messenger import archive as archive_cmd
from cld.messenger import inbox as inbox_cmd
from cld.messenger import read as read_cmd
from cld.messenger import send as send_cmd
from cld.prompts import list_prompt_items
from cld.task_agent import print_task_agent_transcript, resolve_task_agent

log = get_logger(__name__)

_ANY_ARGS = {"allow_extra_args": True, "ignore_unknown_options": True}

app = typer.Typer(context_settings=_ANY_ARGS)


def _handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            # click's Exit subclasses RuntimeError; without this every deliberate
            # exit code (including a broker's 0) would become 1.
            raise
        except (RuntimeError, ValueError, subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error("Command failed: %s", e)
            log.debug("traceback:", exc_info=True)
            raise typer.Exit(1)
    return wrapper


def _host_only(verb: str) -> None:
    typer.echo(f"host-only: run `{verb}` on the host", err=True)
    raise typer.Exit(2)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """cld inside a container: task-agents, the repo agent, mailbox messaging."""
    if ctx.invoked_subcommand is None:
        _host_only("cld")


@app.command("run", hidden=True, context_settings=_ANY_ARGS)
def run_stub(ctx: typer.Context):
    _host_only("cld run")


@app.command("master", hidden=True, context_settings=_ANY_ARGS)
def master_stub(ctx: typer.Context):
    _host_only("cld master")


@app.command("chain", hidden=True, context_settings=_ANY_ARGS)
def chain_stub(ctx: typer.Context):
    _host_only("cld chain")


@app.command("build", hidden=True, context_settings=_ANY_ARGS)
def build_stub(ctx: typer.Context):
    _host_only("cld build")


# --- Broker dispatch ----------------------------------------------------------


def _dispatch_agent_to_broker(cfg: Config, op: str, extra_args: list[str] | None = None) -> None:
    """Delegate a `cld agent <op>` to the host broker.

    This container has no docker daemon (socket removed by design); the broker runs
    host-side `cld agent <op>` for the cwd-selected target repo and streams its
    output back. Exits with the broker's exit code. See cld/broker.py.
    """
    if not broker_available():
        typer.echo(
            "Error: the host broker is not configured for this container, so `cld agent` "
            "cannot reach the host to launch a sibling agent. Set `broker_key` "
            "(and `broker_known_hosts`) in cld config and restart master.",
            err=True,
        )
        raise typer.Exit(1)
    target = str(find_target_repo(cfg))  # resolve_master_target: cwd -> host path
    log.info("Delegating `cld agent %s` for %s to host broker", op or "start", target)
    raise typer.Exit(broker_agent_op(target, op, extra_args))


def _dispatch_task_agent_to_broker(cfg: Config, op: str, extra_args: list[str]) -> None:
    """Delegate a `cld task-agent <op>` to the host broker.

    Spawning and reaping happen host-side for the cwd-selected target repo. The broker
    stamps `--parent <this master>` on the way through and refuses `--force`, so a
    master reaps only its own fleet and can never override a reap-readiness refusal
    (docs/design-task-agents.md §7).
    """
    if not broker_available():
        typer.echo(
            "Error: the host broker is not configured for this container, so `cld task-agent` "
            "cannot reach the host. Set `broker_key` (and `broker_known_hosts`) "
            "in cld config and restart master. Reading the fleet still works without it: "
            "the messenger's fleet_digest()/read_mailbox() tools and `cld task-agent "
            "transcript` all read the mounted mailbox.",
            err=True,
        )
        raise typer.Exit(1)
    target = str(find_target_repo(cfg))
    log.info("Delegating `cld task-agent %s` for %s to host broker", op, target)
    raise typer.Exit(broker_task_agent_op(target, op, extra_args))


def _task_agent_start_argv(
    refs: list[str], name: str, prompt: str,
    branch: str, model: str, revision: str, peer: list[str],
) -> list[str]:
    """Rebuild `start`'s argv for the broker, which re-parses it host-side.

    Paths are the one argument that cannot cross: `/workspace/current` is
    container-ephemeral and a sibling target is an empty placeholder, so a path that
    resolves here resolves to nothing (or to the wrong file) there. An `@ref` is
    forwarded verbatim precisely so the host resolves it against the *target* repo;
    a real path is read here -- reading its own files is exactly what this container is
    entitled to do -- and folded into the inline text, in order, which reproduces the
    brief the host would have composed. The broker refuses a bare path for the same
    reason (docs/design-prompt-chaining.md §4).
    """
    argv: list[str] = []
    bodies: list[str] = []
    for ref in refs:
        if ref.startswith("@"):
            argv.append(ref)
            continue
        body = Path(ref).read_text().strip()
        if not body:
            raise ValueError(f"prompt file is empty: {ref}")
        bodies.append(body)
    inline = "\n\n".join([*bodies, prompt] if prompt else bodies)
    if name:
        argv += ["-n", name]
    if inline:
        argv += ["-p", inline]
    if branch:
        argv += ["--branch", branch]
    if model:
        argv += ["-m", model]
    if revision:
        argv += ["-r", revision]
    for spec in peer:
        argv += ["--peer", spec]
    return argv


# --- Task-scoped agents -------------------------------------------------------
task_agent_app = typer.Typer(
    help="Task-scoped headless agents: one per task, bounded lifespan (see docs/design-task-agents.md).",
)
app.add_typer(task_agent_app, name="task-agent")


@task_agent_app.command("start")
@_handle_errors
def task_agent_start(
    refs: Optional[list[str]] = typer.Argument(None, help="Prompt refs in order: @<ref> resolved host-side, or a path in this container (folded into -p)"),
    name: str = typer.Option("", "-n", "--name", help="Task slug, kebab-case (default: --branch)"),
    prompt: str = typer.Option("", "-p", "--prompt", help="Inline task description, appended after the refs"),
    branch: str = typer.Option("", "--branch", help="Deliverable branch name (default: the task slug)"),
    model: str = typer.Option("", "-m", "--model", help="Claude model (e.g. opus, sonnet)"),
    revision: str = typer.Option("", "-r", "--revision", help="Anchor revision (default: current change)"),
    peer: list[str] = typer.Option([], "--peer", help="A peer this agent may message: <container-name>[:<hops>]. Repeatable."),
):
    """Spawn a task-scoped agent. Every start creates a new container (no start-or-attach)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    _dispatch_task_agent_to_broker(cfg, "start", _task_agent_start_argv(
        refs or [], name, prompt, branch, model, revision, peer,
    ))


@task_agent_app.command("status")
@_handle_errors
def task_agent_status(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name; omit for the roster"),
):
    """Roster of this master's task-agents, or one agent in detail."""
    cfg = Config.from_env()
    setup_logging(cfg)
    _dispatch_task_agent_to_broker(cfg, "status", [name] if name else [])


@task_agent_app.command("logs")
@_handle_errors
def task_agent_logs(
    name: str = typer.Argument(..., help="Task slug or container name"),
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Tail a task-agent's supervisor log (state + cost), NOT its conversation."""
    cfg = Config.from_env()
    setup_logging(cfg)
    _dispatch_task_agent_to_broker(cfg, "logs", [name, "-n", str(tail)])


@task_agent_app.command("shutdown")
@_handle_errors
def task_agent_shutdown(
    name: Optional[str] = typer.Argument(None, help="Task slug or container name"),
    all_: bool = typer.Option(False, "--all", help="Reap every task-agent in this master's fleet"),
    force: bool = typer.Option(False, "--force", help="Host-only; refused here"),
):
    """Stop and remove a task-agent, forget its session bookmark, archive its mailbox."""
    cfg = Config.from_env()
    setup_logging(cfg)
    if all_ == bool(name):
        typer.echo("Error: pass a task slug/container name, or --all -- not both", err=True)
        raise typer.Exit(1)
    if force:
        # The broker denies it too; refusing here gives the reason instead of an
        # opaque exit code.
        typer.echo(
            "Error: --force is host-only. A master cannot override a reap-readiness "
            "refusal -- a refusal means wrap-up has not finished (or a live peer still "
            "depends on this agent), so drive that to completion instead.",
            err=True,
        )
        raise typer.Exit(1)
    _dispatch_task_agent_to_broker(cfg, "shutdown", [name] if name else ["--all"])


@task_agent_app.command("transcript")
@_handle_errors
def task_agent_transcript(
    name: str = typer.Argument(..., help="Task slug or container name"),
):
    """Print the mailbox conversation: what the agent received and what it sent.

    Needs no host channel: the mailbox tree is bind-mounted, and the name resolver
    falls back to the mailbox view where docker is unavailable.
    """
    cfg = Config.from_env()
    setup_logging(cfg)
    print_task_agent_transcript(cfg, resolve_task_agent(cfg, name))


# --- Persistent repo agent ----------------------------------------------------
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
    """Start the persistent repo agent for the cwd-selected repo. Idempotent per repo."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = Config.from_env()
    setup_logging(cfg)
    extra: list[str] = []
    if model:
        extra += ["-m", model]
    if revision:
        extra += ["-r", revision]
    _dispatch_agent_to_broker(cfg, "start", extra)


@agent_app.command("restart")
@_handle_errors
def agent_restart():
    """Restart the repo agent, picking up image/code changes."""
    _dispatch_agent_to_broker(Config.from_env(), "restart")


@agent_app.command("shutdown")
@_handle_errors
def agent_shutdown(
    all_: bool = typer.Option(False, "--all", help="Stop all agent containers on the host"),
):
    """Stop and remove the repo agent (or all with --all)."""
    _dispatch_agent_to_broker(Config.from_env(), "shutdown", ["--all"] if all_ else None)


@agent_app.command("status")
@_handle_errors
def agent_status():
    """Print status of the repo agent (docker + supervisor phase)."""
    _dispatch_agent_to_broker(Config.from_env(), "status")


@agent_app.command("logs")
@_handle_errors
def agent_logs(
    tail: int = typer.Option(80, "-n", "--tail", help="Number of lines to show"),
):
    """Tail the repo agent's log output (= supervisor stderr)."""
    _dispatch_agent_to_broker(Config.from_env(), "logs", ["-n", str(tail)])


# --- Mailbox messaging --------------------------------------------------------
msg_app = typer.Typer(help="Mailbox messaging with other cld containers.")
app.add_typer(msg_app, name="msg")


@msg_app.command("send")
@_handle_errors
def msg_send(
    to: str = typer.Option(..., "--to", help="Recipient shortname or full container name"),
    subject: str = typer.Option(..., "--subject"),
    body_file: str = typer.Option(..., "--body-file", help="Path to a file containing the message body"),
    expects_reply: bool = typer.Option(
        False, "--expects-reply",
        help="Oblige the recipient to reply; only for a question you cannot proceed without",
    ),
    answers: str = typer.Option("", "--answers", help="Id of the message this one answers"),
):
    """Deliver a message to another container's mailbox."""
    send_cmd.deliver(to, subject, Path(body_file).read_text(),
                     expects_reply=expects_reply, answers=answers)


@msg_app.command("inbox")
@_handle_errors
def msg_inbox(
    all_: bool = typer.Option(False, "--all", help="Include archived messages"),
):
    """List this container's unread messages."""
    inbox_cmd.show(all_)


@msg_app.command("read")
@_handle_errors
def msg_read(msg_id: str = typer.Argument(..., metavar="ID")):
    """Print one message in full (inbox first, then archive)."""
    read_cmd.show(msg_id)


@msg_app.command("archive")
@_handle_errors
def msg_archive(msg_id: str = typer.Argument(..., metavar="ID")):
    """Move a message from this container's inbox to its archive."""
    archive_cmd.move(msg_id)


@msg_app.command("agents")
@_handle_errors
def msg_agents(
    kind: str = typer.Option("", "--kind", help="Restrict to one kind: agent or master"),
):
    """List cld containers that can be messaged."""
    agents_cmd.show(kind or None)


# --- The broker ---------------------------------------------------------------


@app.command(context_settings=_ANY_ARGS)
@_handle_errors
def broker(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="Broker action: run-tests, list-containers, agent, task-agent"),
):
    """Run a host-side action through the cld broker (docs/design-cld-broker.md).

    Everything after the action is forwarded verbatim as that action's argv, e.g.
    `cld broker run-tests -k login -x tests/`. The broker decides what an action may
    do; this is only the client.
    """
    if not broker_available():
        typer.echo(
            "Error: the cld broker is not configured for this container. Set `broker_key` "
            "(and `broker_known_hosts`) in cld config and restart master.",
            err=True,
        )
        raise typer.Exit(1)
    raise typer.Exit(run_action(action, *ctx.args).returncode)


# --- Config-only surfaces -----------------------------------------------------


@app.command()
@_handle_errors
def repos():
    """List host repos this master can launch peer containers against.

    Prints one path per line, tagged 'own' for master's own repo (from
    CLD_HOST_PROJECT_DIR) and 'target' for each entry in `master_targets`.
    """
    cfg = Config.from_env()
    setup_logging(cfg)
    if cfg.host_project_dir:
        typer.echo(f"{cfg.host_project_dir}\town")
    for entry in cfg.master_targets:
        typer.echo(f"{os.path.expanduser(entry)}\ttarget")


@app.command()
@_handle_errors
def prompts():
    """List the prompt templates an @<name> argument accepts."""
    cfg = Config.from_env()
    setup_logging(cfg)
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


if __name__ == "__main__":
    app()
