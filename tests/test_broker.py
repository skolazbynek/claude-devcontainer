"""Tests for the broker seam (local daemon on the host vs ssh to the cld broker)."""

import base64
from unittest.mock import patch

import pytest

from cld import broker


def _cp(returncode=0, stdout="", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


@pytest.fixture
def configured(monkeypatch):
    """A container with an endpoint and a mounted key -- i.e. the broker is reachable."""
    monkeypatch.setenv("CLD_BROKER_ENDPOINT", "host.docker.internal:2222")
    monkeypatch.setenv("SESSION_NAME", "cld_master_cld_ab12")
    with patch("cld.broker.Path.exists", return_value=True):
        yield


def _argv_of(run_mock) -> list[str]:
    """Decode the base64(NUL-joined) argv out of the ssh command the client built."""
    payload = run_mock.call_args[0][0][-1].split(" ")[2]
    return [a.decode() for a in base64.b64decode(payload).split(b"\0") if a]


class TestListCldContainers:
    def test_host_uses_local_docker(self):
        ps = _cp(stdout="cld_agent_a\tUp 1 min\n")
        inspect = _cp(stdout="agent|/repo/a\n")
        with patch("cld.broker.in_master_container", return_value=False), \
             patch("cld.broker.subprocess.run", side_effect=[ps, inspect]):
            out = broker.list_cld_containers()
        assert out == [{"name": "cld_agent_a", "kind": "agent", "repo": "/repo/a", "status": "running"}]

    def test_master_uses_broker_and_parses_lines(self, configured):
        lines = _cp(stdout=(
            "cld_agent_a\tagent\t/repo/a\tUp 3 hours\n"
            "cld_master_b_1234\tmaster\t/repo/b\tExited (0) 2 hours ago\n"
        ))
        with patch("cld.broker.in_master_container", return_value=True), \
             patch("cld.broker.subprocess.run", return_value=lines) as run:
            out = broker.list_cld_containers("agent")
        assert run.call_args[0][0][0] == "ssh"
        assert _argv_of(run) == ["agent"]                      # the kind filter
        assert run.call_args[0][0][-1].startswith("list-containers cld_master_cld_ab12 ")
        assert out == [
            {"name": "cld_agent_a", "kind": "agent", "repo": "/repo/a", "status": "running"},
            {"name": "cld_master_b_1234", "kind": "master", "repo": "/repo/b", "status": "stopped"},
        ]

    def test_master_broker_failure_returns_empty(self, configured):
        with patch("cld.broker.in_master_container", return_value=True), \
             patch("cld.broker.subprocess.run", return_value=_cp(returncode=3, stderr="denied")):
            assert broker.list_cld_containers() == []

    def test_malformed_broker_line_skipped(self, configured):
        lines = _cp(stdout="not-enough-fields\ncld_a\tagent\t/r\tUp 1 min\n")
        with patch("cld.broker.in_master_container", return_value=True), \
             patch("cld.broker.subprocess.run", return_value=lines):
            out = broker.list_cld_containers()
        assert out == [{"name": "cld_a", "kind": "agent", "repo": "/r", "status": "running"}]


class TestBrokerAvailable:
    def test_true_with_endpoint_key_and_known_hosts(self, configured):
        assert broker.broker_available() is True

    def test_false_without_a_key(self, monkeypatch):
        monkeypatch.setenv("CLD_BROKER_ENDPOINT", "host.docker.internal:2222")
        with patch("cld.broker.Path.exists", return_value=False):
            assert broker.broker_available() is False

    def test_false_without_known_hosts(self, monkeypatch):
        """run_action always pins the host key, so a missing known_hosts is unusable."""
        monkeypatch.setenv("CLD_BROKER_ENDPOINT", "host.docker.internal:2222")
        with patch("cld.broker.Path.exists", autospec=True,
                   side_effect=lambda self: str(self) == broker.KEY_MOUNT):
            assert broker.broker_available() is False

    def test_false_without_an_endpoint(self, monkeypatch):
        monkeypatch.delenv("CLD_BROKER_ENDPOINT", raising=False)
        with patch("cld.broker.Path.exists", return_value=True):
            assert broker.broker_available() is False


class TestEndpoint:
    @pytest.mark.parametrize("endpoint,expected", [
        ("host.docker.internal:2222", ("zet", "host.docker.internal", "2222")),
        ("someone@myhost:2244", ("someone", "myhost", "2244")),
        ("myhost", ("zet", "myhost", "2222")),
    ])
    def test_split(self, monkeypatch, endpoint, expected):
        monkeypatch.setenv("CLD_BROKER_ENDPOINT", endpoint)
        assert broker._endpoint() == expected


class TestRunAction:
    def test_refuses_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("CLD_BROKER_ENDPOINT", raising=False)
        with patch("cld.broker.subprocess.run") as run:
            result = broker.run_action("run-tests", "-k", "login")
        assert result.returncode == 127
        assert not run.called

    def test_argv_never_becomes_a_command(self, configured):
        """Args are base64(NUL-joined), so a shell metacharacter stays an argument."""
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action("run-tests", "-k", "login; rm -rf /")
        assert _argv_of(run) == ["-k", "login; rm -rf /"]
        assert "rm -rf" not in " ".join(run.call_args[0][0][:-1])

    def test_pins_the_host_key(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp()) as run:
            broker.run_action("run-tests")
        cmd = run.call_args[0][0]
        assert "StrictHostKeyChecking=yes" in cmd
        assert f"UserKnownHostsFile={broker.KNOWN_HOSTS_MOUNT}" in cmd
        assert broker.KEY_MOUNT in cmd


class TestBrokerAgentOp:
    def test_forwards_target_op_and_extra(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=0)) as run:
            rc = broker.broker_agent_op("/repo/y", "start", ["-m", "opus", "-r", "@"])
        assert rc == 0
        assert run.call_args[0][0][-1].startswith("agent cld_master_cld_ab12 ")
        assert _argv_of(run) == ["/repo/y", "start", "-m", "opus", "-r", "@"]
        # lifecycle streams (no capture) so the broker output reaches the user
        assert run.call_args.kwargs["capture_output"] is False

    def test_propagates_exit_code(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=1)):
            assert broker.broker_agent_op("/repo/y", "shutdown") == 1


class TestBrokerTaskAgentOp:
    def test_uses_its_own_action_and_forwards_argv(self, configured):
        """A separate action from `agent`: different op set, and its own argv rules."""
        argv = ["@implementer", "-n", "add-oauth", "-p", "do it", "--peer", "cld_agent_x_y:3"]
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=0)) as run:
            rc = broker.broker_task_agent_op("/repo/y", "start", argv)
        assert rc == 0
        assert run.call_args[0][0][-1].startswith("task-agent cld_master_cld_ab12 ")
        assert _argv_of(run) == ["/repo/y", "start", *argv]
        assert run.call_args.kwargs["capture_output"] is False

    def test_propagates_exit_code(self, configured):
        with patch("cld.broker.subprocess.run", return_value=_cp(returncode=2)):
            assert broker.broker_task_agent_op("/repo/y", "shutdown", ["--all"]) == 2
