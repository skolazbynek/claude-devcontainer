"""Tests for select shell functions in broker/cld-broker.sh, run directly via
bash rather than through the Python broker client (cld/broker.py, covered in
test_broker.py) -- these two are pure-bash logic bugs (a URL-parsing SSRF
bypass, a set -euo pipefail abort on no-match grep) that a mocked-subprocess
Python test can't exercise. Each test sources just the one function under
test out of the real script file, so it breaks if and only if that function's
behavior changes -- not a general shell test harness, just subprocess calls.
"""

import re
import subprocess
from pathlib import Path

import pytest

BROKER_SH = Path(__file__).resolve().parent.parent / "broker" / "cld-broker.sh"


def _extract_function(name: str) -> str:
    text = BROKER_SH.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {BROKER_SH}"
    return match.group(0)


def _run_function(name: str, body_snippet: str, env: dict) -> subprocess.CompletedProcess:
    script = _extract_function(name) + "\n" + body_snippet
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )


class TestCheckUrlAllowlisted:
    """C1: userinfo (user:pass@) must be stripped before the port, or
    'allowed.example:80@evil.com' passes the allowlist while curl actually
    connects to evil.com."""

    @pytest.fixture
    def env(self):
        import os

        e = dict(os.environ)
        e["GRAPHQL_URL_ALLOWLIST"] = "allowed.example"
        return e

    @pytest.mark.parametrize(
        "url,allowed",
        [
            ("http://allowed.example/graphql", True),
            ("http://evil.com/graphql", False),
            ("http://evilallowed.example/graphql", False),
            ("http://allowed.example:80@evil.com/", False),
            ("http://169.254.169.254/latest/meta-data/@allowed.example/", False),
        ],
    )
    def test_allowlist_decision(self, env, url, allowed):
        result = _run_function(
            "check_url_allowlisted", f'check_url_allowlisted {url!r}', env
        )
        if allowed:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "denied" in result.stderr

    def test_userinfo_bypass_resolves_to_real_host_not_allowlisted_prefix(self, env):
        """Regression pin for the exact C1 bug: prove the denial names the real
        connect target (evil.com), not the allowlisted-looking prefix."""
        result = _run_function(
            "check_url_allowlisted",
            "check_url_allowlisted 'http://allowed.example:80@evil.com/'",
            env,
        )
        assert result.returncode != 0
        assert "evil.com" in result.stderr
        assert "allowed.example" not in result.stderr.split("not in")[0]


class TestGraphqlEndpoints:
    """M2: do_graphql_endpoints must return cleanly (rc 0, no output) for a
    secrets file with zero CLD_GRAPHQL_URL_* aliases -- grep's exit 1 on no
    match must not abort the pipeline under set -euo pipefail."""

    def test_empty_aliases_returns_cleanly(self, tmp_path):
        import os

        secrets = tmp_path / ".env"
        secrets.write_text("SOME_OTHER_VAR=1\n")
        env = dict(os.environ)
        result = _run_function(
            "do_graphql_endpoints",
            f"""
set -euo pipefail
SECRETS_ENV_FILE={str(secrets)!r}
resolve_secrets_env_file() {{ :; }}
do_graphql_endpoints
""",
            env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_missing_secrets_file_returns_cleanly(self, tmp_path):
        import os

        secrets = tmp_path / "does-not-exist.env"
        env = dict(os.environ)
        result = _run_function(
            "do_graphql_endpoints",
            f"""
set -euo pipefail
SECRETS_ENV_FILE={str(secrets)!r}
resolve_secrets_env_file() {{ :; }}
do_graphql_endpoints
""",
            env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_lists_configured_aliases(self, tmp_path):
        import os

        secrets = tmp_path / ".env"
        secrets.write_text("CLD_GRAPHQL_URL_DEV=http://dev.internal/graphql\nCLD_GRAPHQL_URL_STAGING=http://staging.internal/graphql\n")
        env = dict(os.environ)
        result = _run_function(
            "do_graphql_endpoints",
            f"""
set -euo pipefail
SECRETS_ENV_FILE={str(secrets)!r}
resolve_secrets_env_file() {{ :; }}
do_graphql_endpoints
""",
            env,
        )
        assert result.returncode == 0, result.stderr
        assert set(result.stdout.split()) == {"dev", "staging"}
