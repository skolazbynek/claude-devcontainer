"""Tests for the claude-devcontainer boot scripts: container-init.sh's
broker-dependent setup, and the TICKET_MODE warm-start wrapper cleanup in the
entrypoint. A warm start (`docker stop` then `docker start`) keeps /tmp, so
the previous boot's generated `claude` wrapper in /tmp/bin -- first on PATH --
shadows `which claude`, and the freshly regenerated wrapper would exec its
stale predecessor's path, an infinite self-exec loop. The code under test is
taken from the real scripts and run via bash.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONTAINER_INIT_SH = (
    Path(__file__).resolve().parent.parent
    / "imgs" / "claude-devcontainer" / "container-init.sh"
)
ENTRYPOINT_SH = CONTAINER_INIT_SH.parent / "entrypoint-claude-devcontainer.sh"


def _extract_entrypoint_fragment(start: str, end: str) -> str:
    """First region of the entrypoint from a line containing `start` through
    the next line containing `end`, inclusive. First match wins, so the
    TICKET_MODE blocks (which precede the v1 flow) are the ones extracted."""
    text = ENTRYPOINT_SH.read_text()
    match = re.search(
        rf"^[ \t]*{re.escape(start)}[^\n]*$.*?^[ \t]*{re.escape(end)}[^\n]*$",
        text, re.MULTILINE | re.DOTALL,
    )
    assert match, f"fragment {start!r}..{end!r} not found in {ENTRYPOINT_SH}"
    return match.group(0)


class TestTicketWarmStartWrapperCleanup:
    """A warm start (`docker stop` then `docker start`) keeps /tmp, so the
    previous boot's generated wrapper in /tmp/bin -- first on PATH -- shadows
    `which claude`, and the regenerated wrapper would exec its stale
    predecessor's path: an infinite self-exec loop (`cld claude` hangs). The
    TICKET_MODE branch must wipe /tmp/bin before anything resolves a binary.
    Runs the real entrypoint fragment with /tmp substituted into a sandbox."""

    def _warm_boot(self, tmp_path):
        real = tmp_path / "realbin"
        real.mkdir()
        binary = real / "claude"
        binary.write_text("#!/bin/bash\n")
        binary.chmod(0o755)
        fake_tmp = tmp_path / "faketmp"
        bindir = fake_tmp / "bin"
        bindir.mkdir(parents=True)
        # What the previous (cold) boot generated into /tmp/bin: a wrapper
        # pointing at the real binary. On the warm boot it shadows that
        # binary on PATH.
        stale_claude = bindir / "claude"
        stale_claude.write_text(
            f"#!/bin/bash\nexec {real}/claude --dangerously-skip-permissions \"$@\"\n"
        )
        stale_claude.chmod(0o755)

        def sub(fragment: str) -> str:
            return fragment.replace("/tmp", str(fake_tmp))

        cleanup = _extract_entrypoint_fragment(
            "rm -f /tmp/cld-ticket-ready", "rm -f /tmp/bin/*")
        claude_block = _extract_entrypoint_fragment(
            "CLAUDE_BIN=$(which claude)", "chmod +x /tmp/bin/claude")
        assert "flock" in claude_block, "expected the TICKET_MODE claude block"
        script = "\n".join([
            f'export PATH="{bindir}:{real}:$PATH"',
            sub(cleanup),
            sub(claude_block),
        ])
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env=dict(os.environ),
        )
        assert result.returncode == 0, result.stderr
        return real, bindir

    def test_regenerated_claude_wrapper_execs_the_real_binary(self, tmp_path):
        real, bindir = self._warm_boot(tmp_path)
        wrapper = (bindir / "claude").read_text()
        assert f"exec {real}/claude --dangerously-skip-permissions" in wrapper
        # Self-referential wrapper = the warm-start infinite exec loop.
        assert str(bindir) not in wrapper


def _source_container_init(tmp_path, body: str, broker_wired: bool, known_hosts: bool = True) -> None:
    """Source the real container-init.sh with /tmp and the broker secrets
    mounts moved into a sandbox, then run *body*."""
    fake_tmp = tmp_path / "faketmp"
    fake_tmp.mkdir(exist_ok=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    if broker_wired:
        (secrets / "broker-key").write_text("key\n")
        if known_hosts:
            (secrets / "broker-known-hosts").write_text("[host]:2222 ssh-ed25519 AAAA\n")
    # /tmp first: the sandbox paths substituted in may themselves be under /tmp.
    init = (CONTAINER_INIT_SH.read_text()
            .replace("/tmp", str(fake_tmp))
            .replace("/run/secrets", str(secrets)))
    script = tmp_path / "container-init.sh"
    script.write_text(init)
    run_env = {**os.environ, "HOME": str(tmp_path / "home")}
    run_env.pop("CLD_BROKER_ENDPOINT", None)
    if broker_wired:
        run_env["CLD_BROKER_ENDPOINT"] = "host.docker.internal:2222"
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(["bash", "-c", f". {script}\n{body}"], capture_output=True, text=True, env=run_env)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
class TestGraphqlMcpBuiltIn:
    """graphql-tester is only a broker client, so every broker-wired container
    gets it -- whether or not the host's ~/.claude.json registers it."""

    def _servers(self, tmp_path, broker_wired: bool, host_servers: dict) -> dict:
        host_config = tmp_path / "faketmp" / "host-config"
        host_config.mkdir(parents=True)
        (host_config / ".claude.json").write_text(json.dumps({"mcpServers": host_servers}))
        _source_container_init(tmp_path, "build_claude_config", broker_wired)
        return json.loads((tmp_path / "home" / ".claude.json").read_text())["mcpServers"]

    def test_added_when_the_broker_is_wired(self, tmp_path):
        servers = self._servers(tmp_path, broker_wired=True, host_servers={})
        assert servers["graphql-tester"]["args"] == ["/opt/cld/cld/mcp/graphql.py"]

    def test_absent_without_broker_or_host_entry(self, tmp_path):
        assert "graphql-tester" not in self._servers(tmp_path, broker_wired=False, host_servers={})

    def test_host_entry_rewritten_to_the_container_path(self, tmp_path):
        host = {"graphql-tester": {"type": "stdio", "command": "/host/run-graphql.sh", "args": []}}
        servers = self._servers(tmp_path, broker_wired=False, host_servers=host)
        assert servers["graphql-tester"]["args"] == ["/opt/cld/cld/mcp/graphql.py"]


class TestTicketClaudeMdBrokerSection:
    """The boot-written ticket CLAUDE.md points a fresh session at the broker
    for tests and the live server -- only where the broker is wired."""

    def _claude_md(self, tmp_path, broker_wired: bool, known_hosts: bool = True) -> str:
        root = tmp_path / "ticket"
        root.mkdir()
        _source_container_init(tmp_path, f"write_ticket_claude_md {root} t '| cld | abc | isolated | jj |\n'",
                               broker_wired, known_hosts)
        return (root / "CLAUDE.md").read_text()

    def test_section_when_wired(self, tmp_path):
        text = self._claude_md(tmp_path, broker_wired=True)
        assert "cld broker run-tests" in text
        assert "broker-graphql-server" in text

    def test_no_section_without_broker(self, tmp_path):
        text = self._claude_md(tmp_path, broker_wired=False)
        assert "Anchor contract" in text
        assert "cld broker" not in text

    def test_no_section_without_known_hosts(self, tmp_path):
        """The client pins the host key, so a key alone is not a usable broker."""
        assert "cld broker" not in self._claude_md(tmp_path, broker_wired=True, known_hosts=False)
