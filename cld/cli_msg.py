"""Mailbox messaging verbs, shared by the host and container CLIs.

`cld msg …` works on both sides: `cld.messenger.identity.resolve_self()` maps a
container to its own mailbox and the host to the cwd repo's master. The error
decorator lives here too, so both apps share exactly one definition.
"""

import functools
import subprocess
from pathlib import Path

import typer

from cld.log import get_logger
from cld.messenger import agents as agents_cmd
from cld.messenger import archive as archive_cmd
from cld.messenger import inbox as inbox_cmd
from cld.messenger import read as read_cmd
from cld.messenger import send as send_cmd


def handle_errors(func):
    # The wrapped command's own logger, so a failure still names the app it came from.
    func_log = get_logger(func.__module__)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            # click's Exit subclasses RuntimeError, so without this it lands in the
            # handler below and every deliberate exit code becomes 1 -- including the 0
            # an in-master broker dispatch raises on success, and including the clean
            # `Error: ...` paths, which also gained a redundant "Command failed: 1" line.
            raise
        except (RuntimeError, ValueError, subprocess.CalledProcessError, OSError) as e:
            # OSError, not FileNotFoundError: a directory or an unreadable file where a
            # prompt ref was expected is the same class of user error as a missing one.
            func_log.error("Command failed: %s", e)
            func_log.debug("traceback:", exc_info=True)
            raise typer.Exit(1)
    return wrapper


msg_app = typer.Typer(help="Mailbox messaging with other cld containers.")


@msg_app.command("send")
@handle_errors
def msg_send(
    to: str = typer.Option(..., "--to", help="Recipient shortname or full container name"),
    subject: str = typer.Option(..., "--subject"),
    body: str = typer.Option("", "--body", help="Message body, inline (mutually exclusive with --body-file)"),
    body_file: str = typer.Option("", "--body-file", help="Path to a file containing the message body"),
    expects_reply: bool = typer.Option(
        False, "--expects-reply",
        help="Oblige the recipient to reply; only for a question you cannot proceed without",
    ),
    answers: str = typer.Option("", "--answers", help="Id of the message this one answers"),
):
    """Deliver a message to another container's mailbox."""
    if bool(body) == bool(body_file):
        typer.echo("Error: provide exactly one of --body or --body-file", err=True)
        raise typer.Exit(1)
    send_cmd.deliver(to, subject, body or Path(body_file).read_text(),
                     expects_reply=expects_reply, answers=answers)


@msg_app.command("inbox")
@handle_errors
def msg_inbox(
    all_: bool = typer.Option(False, "--all", help="Include archived messages"),
):
    """List this container's unread messages."""
    inbox_cmd.show(all_)


@msg_app.command("read")
@handle_errors
def msg_read(msg_id: str = typer.Argument(..., metavar="ID")):
    """Print one message in full (inbox first, then archive)."""
    read_cmd.show(msg_id)


@msg_app.command("archive")
@handle_errors
def msg_archive(msg_id: str = typer.Argument(..., metavar="ID")):
    """Move a message from this container's inbox to its archive."""
    archive_cmd.move(msg_id)


@msg_app.command("agents")
@handle_errors
def msg_agents(
    kind: str = typer.Option("", "--kind", help="Restrict to one kind: agent or master"),
):
    """List cld containers that can be messaged."""
    agents_cmd.show(kind or None)
