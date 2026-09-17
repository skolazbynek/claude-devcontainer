"""Tests for the mysql wrapper generation in container-init.sh (design
section 6.2): v1 kinds wrap one global MYSQL_DEFAULTS_FILE, ticket containers
get one `mysql-<name>` wrapper per mounted /run/secrets/mysql-<name>.cnf and a
plain `mysql` only when exactly one repo has a config. Same
extract-one-function approach as tests/test_broker_sh.py.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

CONTAINER_INIT_SH = (
    Path(__file__).resolve().parent.parent
    / "imgs" / "claude-devcontainer" / "container-init.sh"
)
ENTRYPOINT_SH = CONTAINER_INIT_SH.parent / "entrypoint-claude-devcontainer.sh"


def _extract_function(name: str) -> str:
    text = CONTAINER_INIT_SH.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {CONTAINER_INIT_SH}"
    return match.group(0)


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


class TestGenerateMysqlWrappers:
    @pytest.fixture
    def fake_mysql(self, tmp_path):
        bindir = tmp_path / "fakebin"
        bindir.mkdir()
        (bindir / "mysql").write_text("#!/bin/bash\n")
        (bindir / "mysql").chmod(0o755)
        return bindir

    def _generate(self, fake_mysql, secrets_dir: Path, bin_dir: Path,
                  defaults_file: str = "") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{fake_mysql}:{env['PATH']}"
        env["MYSQL_DEFAULTS_FILE"] = defaults_file
        script = (
            _extract_function("write_mysql_wrapper")
            + "\n"
            + _extract_function("generate_mysql_wrappers")
            + f"\nset -euo pipefail\ngenerate_mysql_wrappers {str(secrets_dir)!r} {str(bin_dir)!r}\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    def test_per_repo_wrappers_from_mounted_cnfs(self, fake_mysql, tmp_path):
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "mysql-lide-api.cnf").write_text("[client]\n")
        (secrets / "mysql-diskuze-api.cnf").write_text("[client]\n")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        result = self._generate(fake_mysql, secrets, bindir)
        assert result.returncode == 0, result.stderr
        for name in ("lide-api", "diskuze-api"):
            wrapper = bindir / f"mysql-{name}"
            assert wrapper.exists(), f"missing wrapper mysql-{name}"
            assert os.access(wrapper, os.X_OK)
            assert f"--defaults-extra-file={secrets}/mysql-{name}.cnf" in wrapper.read_text()
        # Two repos with configs: no ambiguous plain `mysql`.
        assert not (bindir / "mysql").exists()

    def test_single_repo_also_gets_plain_mysql(self, fake_mysql, tmp_path):
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "mysql-lide-api.cnf").write_text("[client]\n")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        result = self._generate(fake_mysql, secrets, bindir)
        assert result.returncode == 0, result.stderr
        assert (bindir / "mysql-lide-api").exists()
        plain = bindir / "mysql"
        assert plain.exists()
        assert f"--defaults-extra-file={secrets}/mysql-lide-api.cnf" in plain.read_text()

    def test_v1_global_defaults_file_still_wraps_plain_mysql(self, fake_mysql, tmp_path):
        defaults = tmp_path / "mysql.cnf"
        defaults.write_text("[client]\n")
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        bindir = tmp_path / "bin"
        bindir.mkdir()
        result = self._generate(fake_mysql, secrets, bindir, defaults_file=str(defaults))
        assert result.returncode == 0, result.stderr
        assert f"--defaults-extra-file={defaults}" in (bindir / "mysql").read_text()

    def test_no_configs_writes_nothing(self, fake_mysql, tmp_path):
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        bindir = tmp_path / "bin"
        bindir.mkdir()
        result = self._generate(fake_mysql, secrets, bindir)
        assert result.returncode == 0, result.stderr
        assert list(bindir.iterdir()) == []


class TestTicketWarmStartWrapperCleanup:
    """A warm start (`docker stop` then `docker start`) keeps /tmp, so the
    previous boot's generated wrappers in /tmp/bin -- first on PATH -- shadow
    `which claude` / `command -v mysql`, and each regenerated wrapper would
    exec its stale predecessor's path: for claude an infinite self-exec loop
    (`cld claude` hangs). The TICKET_MODE branch must wipe /tmp/bin and
    regenerate before anything resolves a binary. Runs the real entrypoint
    fragments with /tmp and /run/secrets substituted into a sandbox."""

    def _warm_boot(self, tmp_path):
        real = tmp_path / "realbin"
        real.mkdir()
        for prog in ("claude", "mysql"):
            binary = real / prog
            binary.write_text("#!/bin/bash\n")
            binary.chmod(0o755)
        fake_tmp = tmp_path / "faketmp"
        bindir = fake_tmp / "bin"
        bindir.mkdir(parents=True)
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        cnf = secrets / "mysql-lide-api.cnf"
        cnf.write_text("[client]\n")
        # What the previous (cold) boot generated into /tmp/bin: wrappers
        # pointing at the real binaries. On the warm boot they shadow those
        # binaries on PATH.
        stale_claude = bindir / "claude"
        stale_claude.write_text(
            f"#!/bin/bash\nexec {real}/claude --dangerously-skip-permissions \"$@\"\n"
        )
        stale_claude.chmod(0o755)
        for name in ("mysql", "mysql-lide-api"):
            stale = bindir / name
            stale.write_text(
                f"#!/bin/bash\nexec {real}/mysql --defaults-extra-file={cnf} \"$@\"\n"
            )
            stale.chmod(0o755)

        def sub(fragment: str) -> str:
            # Replace /tmp before /run/secrets: the sandbox paths themselves
            # live under pytest's /tmp and must not be rewritten.
            return (fragment
                    .replace("/tmp", str(fake_tmp))
                    .replace("/run/secrets", str(secrets)))

        cleanup = _extract_entrypoint_fragment(
            "rm -f /tmp/cld-ticket-ready", "generate_mysql_wrappers")
        claude_block = _extract_entrypoint_fragment(
            "CLAUDE_BIN=$(which claude)", "chmod +x /tmp/bin/claude")
        assert "flock" in claude_block, "expected the TICKET_MODE claude block"
        script = "\n".join([
            _extract_function("write_mysql_wrapper"),
            _extract_function("generate_mysql_wrappers"),
            f'export PATH="{bindir}:{real}:$PATH"',
            # container-init.sh regenerates mysql wrappers at source time,
            # i.e. before the TICKET_MODE branch -- with the stale wrappers
            # still shadowing PATH.
            f"generate_mysql_wrappers {secrets} {bindir}",
            sub(cleanup),
            sub(claude_block),
        ])
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env=dict(os.environ),
        )
        assert result.returncode == 0, result.stderr
        return real, bindir, cnf

    def test_regenerated_claude_wrapper_execs_the_real_binary(self, tmp_path):
        real, bindir, _ = self._warm_boot(tmp_path)
        wrapper = (bindir / "claude").read_text()
        assert f"exec {real}/claude --dangerously-skip-permissions" in wrapper
        # Self-referential wrapper = the warm-start infinite exec loop.
        assert str(bindir) not in wrapper

    def test_regenerated_mysql_wrappers_exec_the_real_binary(self, tmp_path):
        real, bindir, cnf = self._warm_boot(tmp_path)
        for name in ("mysql", "mysql-lide-api"):
            wrapper = (bindir / name).read_text()
            assert f"exec {real}/mysql --defaults-extra-file={cnf}" in wrapper
            # Wrapping the stale wrapper would double --defaults-extra-file.
            assert wrapper.count("--defaults-extra-file") == 1
            assert str(bindir) not in wrapper
