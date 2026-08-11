"""Tests for the host-docker seam (local daemon vs host broker)."""

from unittest.mock import patch

from cld import host_docker


def _cp(returncode=0, stdout="", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


class TestListCldContainers:
    def test_host_uses_local_docker(self):
        ps = _cp(stdout="cld_agent_a\tUp 1 min\n")
        inspect = _cp(stdout="agent|/repo/a\n")
        with patch("cld.host_docker.in_master_container", return_value=False), \
             patch("cld.host_docker.subprocess.run", side_effect=[ps, inspect]):
            out = host_docker.list_cld_containers()
        assert out == [{"name": "cld_agent_a", "kind": "agent", "repo": "/repo/a", "status": "running"}]

    def test_master_uses_broker_and_parses_lines(self):
        broker = _cp(stdout=(
            "cld_agent_a\tagent\t/repo/a\tUp 3 hours\n"
            "cld_master_b_1234\tmaster\t/repo/b\tExited (0) 2 hours ago\n"
        ))
        with patch("cld.host_docker.in_master_container", return_value=True), \
             patch("cld.host_docker.subprocess.run", return_value=broker) as run:
            out = host_docker.list_cld_containers("agent")
        # dispatched via the host-run wrapper with the kind filter forwarded
        cmd = run.call_args[0][0]
        assert cmd[0].endswith("host-run")
        assert cmd[1:] == ["--action", "list-containers", "agent"]
        assert out == [
            {"name": "cld_agent_a", "kind": "agent", "repo": "/repo/a", "status": "running"},
            {"name": "cld_master_b_1234", "kind": "master", "repo": "/repo/b", "status": "stopped"},
        ]

    def test_master_broker_failure_returns_empty(self):
        with patch("cld.host_docker.in_master_container", return_value=True), \
             patch("cld.host_docker.subprocess.run", return_value=_cp(returncode=3, stderr="denied")):
            assert host_docker.list_cld_containers() == []

    def test_malformed_broker_line_skipped(self):
        broker = _cp(stdout="not-enough-fields\ncld_a\tagent\t/r\tUp 1 min\n")
        with patch("cld.host_docker.in_master_container", return_value=True), \
             patch("cld.host_docker.subprocess.run", return_value=broker):
            out = host_docker.list_cld_containers()
        assert out == [{"name": "cld_a", "kind": "agent", "repo": "/r", "status": "running"}]


class TestBrokerAvailable:
    def test_true_when_wrapper_present(self, tmp_path):
        with patch("cld.host_docker.Path.exists", return_value=True):
            assert host_docker.broker_available() is True

    def test_false_when_absent(self):
        with patch("cld.host_docker.Path.exists", return_value=False), \
             patch("cld.host_docker.shutil.which", return_value=None):
            assert host_docker.broker_available() is False


class TestBrokerAgentOp:
    def test_forwards_target_op_and_extra(self):
        with patch("cld.host_docker.subprocess.run", return_value=_cp(returncode=0)) as run:
            rc = host_docker.broker_agent_op("/repo/y", "start", ["-m", "opus", "-r", "@"])
        assert rc == 0
        cmd = run.call_args[0][0]
        assert cmd[0].endswith("host-run")
        assert cmd[1:] == ["--action", "agent", "/repo/y", "start", "-m", "opus", "-r", "@"]
        # lifecycle streams (no capture) so the broker output reaches the user
        assert run.call_args.kwargs["capture_output"] is False

    def test_propagates_exit_code(self):
        with patch("cld.host_docker.subprocess.run", return_value=_cp(returncode=1)):
            assert host_docker.broker_agent_op("/repo/y", "shutdown") == 1


class TestBrokerTaskAgentOp:
    def test_uses_its_own_action_and_forwards_argv(self):
        """A separate action from `agent`: different op set, and its own argv rules."""
        argv = ["@implementer", "-n", "add-oauth", "-p", "do it", "--peer", "cld_agent_x_y:3"]
        with patch("cld.host_docker.subprocess.run", return_value=_cp(returncode=0)) as run:
            rc = host_docker.broker_task_agent_op("/repo/y", "start", argv)
        assert rc == 0
        cmd = run.call_args[0][0]
        assert cmd[0].endswith("host-run")
        assert cmd[1:] == ["--action", "task-agent", "/repo/y", "start", *argv]
        assert run.call_args.kwargs["capture_output"] is False

    def test_propagates_exit_code(self):
        with patch("cld.host_docker.subprocess.run", return_value=_cp(returncode=2)):
            assert host_docker.broker_task_agent_op("/repo/y", "shutdown", ["--all"]) == 2
