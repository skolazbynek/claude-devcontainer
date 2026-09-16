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


def _extract_function(name: str) -> str:
    text = CONTAINER_INIT_SH.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {CONTAINER_INIT_SH}"
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
