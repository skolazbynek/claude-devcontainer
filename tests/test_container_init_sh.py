"""Tests for the TICKET_MODE warm-start wrapper cleanup in the
claude-devcontainer entrypoint: a warm start (`docker stop` then `docker
start`) keeps /tmp, so the previous boot's generated `claude` wrapper in
/tmp/bin -- first on PATH -- shadows `which claude`, and the freshly
regenerated wrapper would exec its stale predecessor's path, an infinite
self-exec loop. Same extract-one-function approach as tests/test_broker_sh.py.
"""

import os
import re
import subprocess
from pathlib import Path

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
