"""Container-side CLI: the `cld` the devcontainer image installs.

Only verbs that work inside a container, each wired straight to the seam it uses
-- the host broker for anything needing a docker daemon, the bind-mounted mailbox
tree for anything needing a conversation. Host-only verbs are hidden stubs that
say so instead of failing obscurely. See docs/design-cli-split.md.
"""

import os
from pathlib import Path
from typing import Optional

import typer

from cld.cli_msg import handle_errors as _handle_errors, msg_app
from cld.config import Config
from cld.docker import find_target_repo
from cld.broker import broker_available, broker_task_agent_op, run_action
from cld.log import get_logger, setup_logging
from cld.manifest import TicketManifest
from cld.prompts import list_prompt_items
from cld.task_agent import print_task_agent_transcript, resolve_task_agent

log = get_logger(__name__)

_ANY_ARGS = {"allow_extra_args": True, "ignore_unknown_options": True}

app = typer.Typer(context_settings=_ANY_ARGS)


def _host_only(verb: str) -> None:
    typer.echo(f"host-only: run `{verb}` on the host", err=True)
    raise typer.Exit(2)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """cld inside a container: task-agents, mailbox messaging, the broker."""
    if ctx.invoked_subcommand is None:
        _host_only("cld")


@app.command("run", hidden=True, context_settings=_ANY_ARGS)
def run_stub(ctx: typer.Context):
    _host_only("cld run")


@app.command("chain", hidden=True, context_settings=_ANY_ARGS)
def chain_stub(ctx: typer.Context):
    _host_only("cld chain")


@app.command("build", hidden=True, context_settings=_ANY_ARGS)
def build_stub(ctx: typer.Context):
    _host_only("cld build")


# Ticket lifecycle verbs (v2) are host-only: they drive the docker daemon.


@app.command("start", hidden=True, context_settings=_ANY_ARGS)
def start_stub(ctx: typer.Context):
    _host_only("cld start")


@app.command("claude", hidden=True, context_settings=_ANY_ARGS)
def claude_stub(ctx: typer.Context):
    _host_only("cld claude")


@app.command("shell", hidden=True, context_settings=_ANY_ARGS)
def shell_stub(ctx: typer.Context):
    _host_only("cld shell")


@app.command("stop", hidden=True, context_settings=_ANY_ARGS)
def stop_stub(ctx: typer.Context):
    _host_only("cld stop")


@app.command("restart", hidden=True, context_settings=_ANY_ARGS)
def restart_stub(ctx: typer.Context):
    _host_only("cld restart")


@app.command("shutdown", hidden=True, context_settings=_ANY_ARGS)
def shutdown_stub(ctx: typer.Context):
    _host_only("cld shutdown")


@app.command("status", hidden=True, context_settings=_ANY_ARGS)
def status_stub(ctx: typer.Context):
    _host_only("cld status")


@app.command("logs", hidden=True, context_settings=_ANY_ARGS)
def logs_stub(ctx: typer.Context):
    _host_only("cld logs")


# --- Broker dispatch ----------------------------------------------------------


def _dispatch_task_agent_to_broker(cfg: Config, op: str, extra_args: list[str]) -> None:
    """Delegate a `cld task-agent <op>` to the host broker.

    Spawning and reaping happen host-side for the cwd-selected target repo. The broker
    stamps `--parent <this container>` on the way through and refuses `--force`, so a
    caller reaps only its own fleet and can never override a reap-readiness refusal
    (docs/design-task-agents.md §7).
    """
    if not broker_available():
        typer.echo(
            "Error: the host broker is not configured for this container, so `cld task-agent` "
            "cannot reach the host. Set `broker_key` (and `broker_known_hosts`) "
            "in cld config and restart this container. Reading the fleet still works without it: "
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
    shared_anchor: bool = False,
) -> list[str]:
    """Rebuild `start`'s argv for the broker, which re-parses it host-side.

    Paths are the one argument that cannot cross: `/workspace/current` is
    container-ephemeral and a sibling target is an empty placeholder, so a path that
    resolves here resolves to nothing (or to the wrong file) there. An `@ref` is
    forwarded verbatim precisely so the host resolves it against the *target* repo;
    a real path is read here -- reading its own files is exactly what this container is
    entitled to do -- and folded into the inline text. Typed order is only preserved
    among the folded files: host-side the positionals compose first and `-p` is
    appended last, so every local file lands after every `@ref` no matter where it was
    typed. The broker refuses a bare path for the same reason
    (docs/design-prompt-chaining.md §4).
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
    if shared_anchor:
        argv += ["--shared-anchor"]
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
    shared_anchor: bool = typer.Option(
        False, "--shared-anchor",
        help="Anchor directly on -r, sharing reach with its existing descendants "
        "(needs explicit human approval; default is an isolated sibling)",
    ),
):
    """Spawn a task-scoped agent. Every start creates a new container (no start-or-attach)."""
    cfg = Config.from_env()
    setup_logging(cfg)
    _dispatch_task_agent_to_broker(cfg, "start", _task_agent_start_argv(
        refs or [], name, prompt, branch, model, revision, peer, shared_anchor,
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


# --- Mailbox messaging --------------------------------------------------------
app.add_typer(msg_app, name="msg")


# --- The broker ---------------------------------------------------------------


@app.command(context_settings=_ANY_ARGS)
@_handle_errors
def broker(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="Broker action: run-tests, list-containers, task-agent, graphql"),
):
    """Run a host-side action through the cld broker (docs/design-cld-broker.md).

    Everything after the action is forwarded verbatim as that action's argv, e.g.
    `cld broker run-tests -k login -x tests/`. The broker decides what an action may
    do; this is only the client.
    """
    if not broker_available():
        typer.echo(
            "Error: the cld broker is not configured for this container. Set `broker_key` "
            "(and `broker_known_hosts`) in cld config and restart this container.",
            err=True,
        )
        raise typer.Exit(1)
    raise typer.Exit(run_action(action, *ctx.args).returncode)


# --- Config-only surfaces -----------------------------------------------------


@app.command()
@_handle_errors
def repos():
    """List the repos this container works against.

    Ticket container (v2): one line per mounted repo from the launch manifest
    -- name, origin path, workspace path, anchor, mode
    (docs/design-ticket-containers.md section 6.6). The manifest env is
    host-set at launch.

    v1 task-agent: the single repo the container was launched for,
    tagged 'own'. Its host path comes from the host-set CLD_HOST_PROJECT_DIR,
    not from in-container TOML -- `.cld/` is gitignored, so a re-read would
    usually find nothing and disagree with the host.
    """
    cfg = Config.from_env()
    setup_logging(cfg)
    raw_manifest = os.environ.get("CLD_TICKET_MANIFEST", "")
    if raw_manifest:
        manifest = TicketManifest.from_json(raw_manifest)
        for repo in manifest.repos:
            typer.echo(
                f"{repo.name}\t/workspace/origin/{repo.name}"
                f"\t/workspace/{manifest.ticket}/{repo.name}"
                f"\t{repo.anchor_base[:12]}\t{repo.anchor_mode}"
            )
        return
    if cfg.host_project_dir:
        typer.echo(f"{cfg.host_project_dir}\town")


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
