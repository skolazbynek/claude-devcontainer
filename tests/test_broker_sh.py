"""Tests for select shell functions in broker/cld-broker.sh, run directly via
bash rather than through the Python broker client (cld/broker.py, covered in
test_broker.py) -- these two are pure-bash logic bugs (a URL-parsing SSRF
bypass, a set -euo pipefail abort on no-match grep) that a mocked-subprocess
Python test can't exercise. Each test sources just the one function under
test out of the real script file, so it breaks if and only if that function's
behavior changes -- not a general shell test harness, just subprocess calls.
"""

import json
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


def _run_functions(names: list, body_snippet: str, env: dict) -> subprocess.CompletedProcess:
    script = "\n".join(_extract_function(n) for n in names) + "\n" + body_snippet
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


class TestGraphqlStatusStale:
    """M5: do_graphql_status's 6th field (stale) compares the container's own
    served revision (REVISION env) against the session's current jj tip.
    docker and jj are stubbed on PATH -- real curl_capture is never reached
    because the stubbed `docker port` returns nothing, keeping the server in
    the 'starting' state (still enough to exercise the stale computation,
    which happens before the port/probe branching)."""

    @pytest.fixture
    def fakebin(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text("""#!/usr/bin/env bash
case "$1" in
  ps) exit 0 ;;
  inspect)
    shift; shift
    fmt=""
    while [ $# -gt 0 ]; do
      case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
    done
    if [ -z "$fmt" ]; then
      [ "${FAKE_CONTAINER_EXISTS:-1}" = 1 ] && exit 0 || exit 1
    fi
    case "$fmt" in
      '{{.State.Running}}') echo "${FAKE_RUNNING:-true}" ;;
      '{{range .Config.Env}}{{println .}}{{end}}') printf 'REVISION=%s\\n' "${FAKE_REV:-}" ;;
    esac
    ;;
  port) exit 0 ;;
esac
""")
        (bindir / "jj").write_text("""#!/usr/bin/env bash
[ "${FAKE_TIP_FAILS:-0}" = 1 ] && exit 1
echo "${FAKE_TIP:-}"
""")
        (bindir / "docker").chmod(0o755)
        (bindir / "jj").chmod(0o755)
        return bindir

    def _status(self, fakebin, env_overrides, tmp_path):
        import os

        env = dict(os.environ)
        env["PATH"] = f"{fakebin}:{env['PATH']}"
        env.update(env_overrides)
        return _run_functions(
            ["do_graphql_status", "sweep_gql_orphans", "resolve_graphql_config",
             "print_gql_status_line", "cld_conf_get"],
            f"""
set -euo pipefail
REPO={str(tmp_path)!r}
session=cld_agent_test
do_graphql_status
""",
            env,
        )

    def test_matching_revision_is_not_stale(self, fakebin, tmp_path):
        result = self._status(
            fakebin, {"FAKE_REV": "abc123", "FAKE_TIP": "abc123"}, tmp_path
        )
        assert result.returncode == 0, result.stderr
        fields = result.stdout.rstrip("\n").split("\t")
        assert fields[0] == "starting"
        assert fields[5] == "false"

    def test_mismatched_revision_is_stale(self, fakebin, tmp_path):
        result = self._status(
            fakebin, {"FAKE_REV": "abc123", "FAKE_TIP": "def456"}, tmp_path
        )
        assert result.returncode == 0, result.stderr
        fields = result.stdout.rstrip("\n").split("\t")
        assert fields[0] == "starting"
        assert fields[5] == "true"

    def test_unresolvable_tip_leaves_stale_empty(self, fakebin, tmp_path):
        result = self._status(
            fakebin, {"FAKE_REV": "abc123", "FAKE_TIP_FAILS": "1"}, tmp_path
        )
        assert result.returncode == 0, result.stderr
        fields = result.stdout.rstrip("\n").split("\t")
        assert fields[0] == "starting"
        assert fields[5] == ""

    def test_not_started_stale_empty(self, fakebin, tmp_path):
        result = self._status(fakebin, {"FAKE_CONTAINER_EXISTS": "0"}, tmp_path)
        assert result.returncode == 0, result.stderr
        fields = result.stdout.rstrip("\n").split("\t")
        assert fields[0] == "not_started"
        assert fields[5] == ""


class TestGraphqlQueryBody:
    """do_graphql_query must not pipe the request body into curl_capture: a
    pipeline runs its last element in a subshell, so CURL_STATUS/CURL_BODY set
    there never reach the reads that follow, and the function dies on
    'CURL_BODY: unbound variable' under set -euo pipefail."""

    @pytest.fixture
    def fakecurl(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        # Echoes the --data-binary argument back as the response body, so a
        # body that never arrived (or arrived as the literal '@-') is visible
        # in the assertion rather than silently passing.
        (bindir / "curl").write_text("""#!/usr/bin/env bash
body=""
while [ $# -gt 0 ]; do
  case "$1" in --data-binary) body="$2"; shift 2 ;; *) shift ;; esac
done
printf '%s\\n200' "$body"
""")
        (bindir / "curl").chmod(0o755)
        return bindir

    def _query(self, fakecurl, query="{ __typename }", variables=""):
        import os

        env = dict(os.environ)
        env["PATH"] = f"{fakecurl}:{env['PATH']}"
        return _run_functions(
            ["do_graphql_query", "curl_capture", "mask_output", "cap_output"],
            f"""
set -euo pipefail
GRAPHQL_QUERY_TIMEOUT=5
GRAPHQL_OUTPUT_MAX_BYTES=65536
resolve_target() {{ TARGET_URL=http://stub/graphql; AUTH_HEADER=""; COOKIE_HEADER=""; }}
do_graphql_query local {query!r} {variables!r}
""",
            env,
        )

    def test_body_reaches_curl_and_response_is_returned(self, fakecurl):
        result = self._query(fakecurl)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"query": "{ __typename }", "variables": {}}

    def test_variables_are_merged_into_the_body(self, fakecurl):
        result = self._query(fakecurl, "query T($n: String!) { __type(name: $n) { name } }", '{"n": "Query"}')
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["variables"] == {"n": "Query"}
