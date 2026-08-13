"""Tests for the container-side CLI (cld/cli_container.py).

The surface is defined by what a container can actually reach: the host broker for
anything needing a docker daemon, the bind-mounted mailbox for anything needing a
conversation. See docs/design-cli-split.md.
"""

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cld.cli_container import app
from cld.config import Config


runner = CliRunner()


def _write_mailbox(root: Path, name: str, *, meta: dict | None = None) -> Path:
    d = root / name
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "archive").mkdir(parents=True, exist_ok=True)
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta))
    return d


def _meta(**over) -> dict:
    base = {
        "parent": "", "task": "wire up oauth login", "persona": "implementer",
        "deliverable_branch": "add-oauth", "anchor": "a" * 40, "peers": {},
        "created_at": "2026-08-12T10:00:00.000000Z",
    }
    base.update(over)
    return base


class TestHostOnlyStubs:
    """Verbs that need a docker daemon say so instead of failing obscurely."""

    @pytest.mark.parametrize("argv", [
        ["run", "task.md"],
        ["master"],
        ["chain", "run", "c.yaml"],
        ["build"],
        [],
    ])
    def test_refused_with_a_host_only_message(self, argv):
        result = runner.invoke(app, argv)
        assert result.exit_code == 2
        assert "host-only" in result.output

    def test_hidden_from_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for verb in ("run", "master", "chain", "build"):
            assert f"│ {verb}" not in result.output
        assert "task-agent" in result.output
        assert "msg" in result.output


class TestTaskAgentDispatch:
    """Lifecycle goes through the broker, which stamps --parent and refuses --force
    (docs/design-task-agents.md §9)."""

    def _broker(self, stack, tmp_path, monkeypatch, available=True):
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(tmp_path / "mailboxes"))
        stack.enter_context(patch("cld.cli_container.broker_available", return_value=available))
        stack.enter_context(patch("cld.cli_container.find_target_repo", return_value=Path("/host/myrepo")))
        return stack.enter_context(patch("cld.cli_container.broker_task_agent_op", return_value=0))

    def test_start_forwards_rebuilt_argv(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, [
                "task-agent", "start", "@personas/implementer", "@some-task",
                "-n", "add-oauth", "-p", "do it",
                "--branch", "oauth", "-m", "opus", "-r", "@-", "--peer", "cld_agent_x_y:3",
            ])
        assert result.exit_code == 0, result.output
        target, verb, argv = op.call_args.args
        assert (target, verb) == ("/host/myrepo", "start")
        assert argv == [
            "@personas/implementer", "@some-task", "-n", "add-oauth", "-p", "do it",
            "--branch", "oauth", "-m", "opus", "-r", "@-", "--peer", "cld_agent_x_y:3",
        ]
        assert "--parent" not in argv          # the broker sets it, host-side

    def test_start_folds_local_files_into_the_prompt_in_order(self, tmp_path, monkeypatch):
        first = tmp_path / "a.md"; first.write_text("# First\n")
        second = tmp_path / "b.md"; second.write_text("# Second\n")
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, [
                "task-agent", "start", "@personas/implementer", str(first), str(second),
                "-n", "s", "-p", "and this",
            ])
        assert result.exit_code == 0, result.output
        argv = op.call_args.args[2]
        assert str(first) not in argv         # a container path means nothing host-side
        assert "@personas/implementer" in argv
        body = argv[argv.index("-p") + 1]
        assert body == "# First\n\n# Second\n\nand this"

    def test_start_forwards_an_at_ref_verbatim(self, tmp_path, monkeypatch):
        """The host must resolve it against the *target* repo, not master's own."""
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, ["task-agent", "start", "@some-task", "-n", "s"])
        assert result.exit_code == 0, result.output
        assert "@some-task" in op.call_args.args[2]

    def test_status_and_logs_dispatch(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            assert runner.invoke(app, ["task-agent", "status"]).exit_code == 0
            assert op.call_args.args[1:] == ("status", [])
            assert runner.invoke(app, ["task-agent", "logs", "slug", "-n", "20"]).exit_code == 0
            assert op.call_args.args[1:] == ("logs", ["slug", "-n", "20"])

    def test_shutdown_dispatches_name_and_all(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            assert runner.invoke(app, ["task-agent", "shutdown", "slug"]).exit_code == 0
            assert op.call_args.args[1:] == ("shutdown", ["slug"])
            assert runner.invoke(app, ["task-agent", "shutdown", "--all"]).exit_code == 0
            assert op.call_args.args[1:] == ("shutdown", ["--all"])

    def test_shutdown_force_refused_locally_with_a_reason(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch)
            result = runner.invoke(app, ["task-agent", "shutdown", "slug", "--force"])
        assert result.exit_code == 1
        assert "host-only" in result.output
        assert "wrap-up has not finished" in result.output
        assert not op.called

    def test_missing_broker_names_what_still_works(self, tmp_path, monkeypatch):
        with ExitStack() as stack:
            op = self._broker(stack, tmp_path, monkeypatch, available=False)
            result = runner.invoke(app, ["task-agent", "shutdown", "slug"])
        assert result.exit_code == 1
        assert "broker_key" in result.output
        assert "transcript" in result.output      # reading the fleet needs no broker
        assert not op.called

    def test_transcript_works_without_the_broker_or_docker(self, tmp_path, monkeypatch):
        root = tmp_path / "mailboxes"
        name = "cld_agent_myrepo_add-oauth"
        d = _write_mailbox(root, name, meta=_meta())
        (d / "archive" / "m1.json").write_text(json.dumps({
            "id": "m1", "from": "cld_master_myrepo_ab12", "to": name,
            "subject": "kick off", "body": "start here", "ts": "2026-08-12T10:00:00Z",
        }))
        monkeypatch.setenv("CLD_MAILBOX_ROOT", str(root))
        with patch("cld.task_agent.docker_task_agent_list", return_value=[]):
            result = runner.invoke(app, ["task-agent", "transcript", "add-oauth"])
        assert result.exit_code == 0, result.output
        assert "kick off" in result.output
        assert "start here" in result.output


class TestAgentDispatch:
    def _op(self, stack, available=True):
        stack.enter_context(patch("cld.cli_container.broker_available", return_value=available))
        stack.enter_context(patch("cld.cli_container.find_target_repo", return_value=Path("/host/myrepo")))
        return stack.enter_context(patch("cld.cli_container.broker_agent_op", return_value=0))

    def test_bare_agent_starts(self):
        with ExitStack() as stack:
            op = self._op(stack)
            result = runner.invoke(app, ["agent", "-m", "opus"])
        assert result.exit_code == 0, result.output
        assert op.call_args.args == ("/host/myrepo", "start", ["-m", "opus"])

    @pytest.mark.parametrize("verb,extra", [
        ("restart", None), ("status", None), ("logs", ["-n", "80"]),
    ])
    def test_subcommands_dispatch(self, verb, extra):
        with ExitStack() as stack:
            op = self._op(stack)
            result = runner.invoke(app, ["agent", verb])
        assert result.exit_code == 0, result.output
        assert op.call_args.args[1] == verb
        assert op.call_args.args[2] == extra

    def test_shutdown_all_forwards_the_flag(self):
        with ExitStack() as stack:
            op = self._op(stack)
            assert runner.invoke(app, ["agent", "shutdown", "--all"]).exit_code == 0
        assert op.call_args.args[2] == ["--all"]

    def test_missing_broker_is_explained(self):
        with ExitStack() as stack:
            op = self._op(stack, available=False)
            result = runner.invoke(app, ["agent", "status"])
        assert result.exit_code == 1
        assert "host broker is not configured" in result.output
        assert not op.called


class TestBrokerExitCodes:
    """typer.Exit subclasses RuntimeError, so _handle_errors has to let it through."""

    def _invoke(self, rc):
        with ExitStack() as stack:
            stack.enter_context(patch("cld.cli_container.broker_available", return_value=True))
            stack.enter_context(patch("cld.cli_container.find_target_repo", return_value=Path("/host/myrepo")))
            stack.enter_context(patch("cld.cli_container.broker_agent_op", return_value=rc))
            return runner.invoke(app, ["agent", "status"])

    def test_zero_stays_zero(self):
        assert self._invoke(0).exit_code == 0

    def test_failure_code_is_propagated(self):
        assert self._invoke(3).exit_code == 3


class TestRepos:
    def test_lists_own_and_targets(self):
        cfg = Config(host_project_dir="/host/side/cld",
                     master_targets=("/host/side/foo", "/host/side/bar"))
        with patch("cld.cli_container.Config.from_env", return_value=cfg):
            result = runner.invoke(app, ["repos"])
        assert result.exit_code == 0, result.output
        assert "/host/side/cld\town" in result.output
        assert "/host/side/foo\ttarget" in result.output
        assert "/host/side/bar\ttarget" in result.output


class TestMsg:
    """The mailbox verbs replace `python3 -m cld.messenger.*`; each calls the same
    module function the MCP server uses."""

    def test_send_reads_the_body_file(self, tmp_path):
        body = tmp_path / "body.md"
        body.write_text("the body\n")
        with patch("cld.cli_container.send_cmd.deliver") as deliver:
            result = runner.invoke(app, [
                "msg", "send", "--to", "cld_agent_x_y", "--subject", "hi",
                "--body-file", str(body), "--expects-reply", "--answers", "m3",
            ])
        assert result.exit_code == 0, result.output
        assert deliver.call_args.args == ("cld_agent_x_y", "hi", "the body\n")
        assert deliver.call_args.kwargs == {"expects_reply": True, "answers": "m3"}

    def test_inbox_all_flag(self):
        with patch("cld.cli_container.inbox_cmd.show") as show:
            assert runner.invoke(app, ["msg", "inbox", "--all"]).exit_code == 0
        assert show.call_args.args == (True,)

    def test_read_takes_an_id(self):
        with patch("cld.cli_container.read_cmd.show") as show:
            assert runner.invoke(app, ["msg", "read", "m1"]).exit_code == 0
        assert show.call_args.args == ("m1",)

    def test_archive_takes_an_id(self):
        with patch("cld.cli_container.archive_cmd.move") as move:
            assert runner.invoke(app, ["msg", "archive", "m1"]).exit_code == 0
        assert move.call_args.args == ("m1",)

    def test_agents_kind_filter_defaults_to_none(self):
        with patch("cld.cli_container.agents_cmd.show") as show:
            assert runner.invoke(app, ["msg", "agents"]).exit_code == 0
            assert show.call_args.args == (None,)
            assert runner.invoke(app, ["msg", "agents", "--kind", "master"]).exit_code == 0
            assert show.call_args.args == ("master",)


class TestPrompts:
    def test_lists_refs_with_descriptions(self, tmp_path):
        items = [("personas/architect", "Designs a solution"), ("todo-agent", "")]
        with patch("cld.cli_container.list_prompt_items", return_value=items):
            result = runner.invoke(app, ["prompts"])
        assert result.exit_code == 0, result.output
        assert "personas/architect" in result.output
        assert "Designs a solution" in result.output
