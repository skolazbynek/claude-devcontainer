"""Tests for broker/cld-broker.py, the host-side sshd ForceCommand, as opposed
to the container-side client (cld/broker.py, covered in test_broker.py). The
script is loaded straight from its file -- it is not part of the `cld`
package -- and docker/jj are stubbed on PATH, so each test drives the real
function against a faked host.
"""

import base64
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

BROKER_PY = Path(__file__).resolve().parent.parent / "broker" / "cld-broker.py"

_spec = importlib.util.spec_from_file_location("cld_broker", BROKER_PY)
broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(broker)


@pytest.fixture(autouse=True)
def conf(monkeypatch):
    settings = {}
    monkeypatch.setattr(broker, "CONF", settings)
    return settings


def _fakebin(tmp_path, monkeypatch, **scripts: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in scripts.items():
        (bindir / name).write_text("#!/usr/bin/env bash\n" + body)
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return bindir


def _exit_code(excinfo) -> int:
    return excinfo.value.code


class TestParseRepoArg:
    """parse_repo_arg consumes a LEADING --repo <name> / --repo=<name> -- and
    only a leading one: a --repo later in the argv belongs to the action's own
    command (e.g. pytest)."""

    @pytest.mark.parametrize(
        "argv,name,rest",
        [
            (["--repo", "lide-api", "-k", "login"], "lide-api", ["-k", "login"]),
            (["--repo=lide-api", "status"], "lide-api", ["status"]),
            (["-k", "login"], "", ["-k", "login"]),
            (["-k", "x", "--repo", "y"], "", ["-k", "x", "--repo", "y"]),
        ],
        ids=["separate-token", "equals-form", "absent", "non-leading-untouched"],
    )
    def test_parse(self, argv, name, rest):
        assert broker.parse_repo_arg(argv) == (name, rest)

    @pytest.mark.parametrize("argv", [["--repo"], ["--repo="]], ids=["bare", "empty-equals"])
    def test_missing_name_denied(self, argv, capsys):
        with pytest.raises(SystemExit) as excinfo:
            broker.parse_repo_arg(argv)
        assert _exit_code(excinfo) == 2
        assert "denied" in capsys.readouterr().err


class TestLabelCallerRepo:
    """LabelCaller.repo maps a --repo name to a repo via the caller's
    host-set flat org.cld.repo.<name> labels: ticket callers get per-name
    validation plus the single-repo default; v1 callers keep their
    repo-root and refuse --repo (design section 6.1)."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        return _fakebin(tmp_path, monkeypatch, docker="""
cmd="$1"; shift
[ "$cmd" = inspect ] || exit 1
printf '%s' "${FAKE_LABELS:-}"
""")

    @pytest.fixture
    def repos(self, tmp_path):
        for name in ("lide-api", "diskuze-api"):
            (tmp_path / name / ".jj").mkdir(parents=True)
        return tmp_path

    def _resolve(self, monkeypatch, labels: str, kind: str, name: str, repo_preset: str = "/preset") -> str:
        monkeypatch.setenv("FAKE_LABELS", labels)
        return broker.LabelCaller("cld_ticket_x", kind, repo_preset).repo(name)

    def _labels(self, repos) -> str:
        return (
            "org.cld.kind=ticket\n"
            f"org.cld.repo.lide-api={repos}/lide-api\n"
            f"org.cld.repo.diskuze-api={repos}/diskuze-api\n"
        )

    def test_named_repo_resolves_to_its_labeled_path(self, monkeypatch, repos):
        assert self._resolve(monkeypatch, self._labels(repos), "ticket", "lide-api") == f"{repos}/lide-api"

    def test_unknown_repo_denied_listing_mounted_names(self, monkeypatch, repos, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, self._labels(repos), "ticket", "nope")
        assert _exit_code(excinfo) == 3
        err = capsys.readouterr().err
        assert "not mounted" in err
        assert "lide-api" in err and "diskuze-api" in err

    def test_multi_repo_without_name_denied(self, monkeypatch, repos, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, self._labels(repos), "ticket", "")
        assert _exit_code(excinfo) == 3
        assert "pass --repo" in capsys.readouterr().err

    def test_single_repo_defaults_without_name(self, monkeypatch, repos):
        labels = f"org.cld.repo.lide-api={repos}/lide-api\n"
        assert self._resolve(monkeypatch, labels, "ticket", "") == f"{repos}/lide-api"

    def test_labeled_path_must_be_a_repo(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "bare").mkdir()
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, f"org.cld.repo.bare={tmp_path}/bare\n", "ticket", "bare")
        assert _exit_code(excinfo) == 3
        assert "is not a repo" in capsys.readouterr().err

    def test_v1_kind_refuses_repo_flag(self, monkeypatch, repos, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._resolve(monkeypatch, self._labels(repos), "task-agent", "lide-api")
        assert _exit_code(excinfo) == 2
        assert "only for ticket containers" in capsys.readouterr().err

    def test_v1_kind_keeps_dispatcher_repo(self, monkeypatch, repos):
        assert self._resolve(monkeypatch, "", "task-agent", "", repo_preset="/v1/repo") == "/v1/repo"


class TestLabelCallerTargets:
    """LabelCaller.targets is the caller's repo-root plus its org.cld.targets
    allowlist, with empty fields dropped -- a ticket has no repo-root."""

    @pytest.mark.parametrize(
        "repo_root,label,expected",
        [
            ("/root", "", ["/root"]),
            ("/root", "/a:/b:", ["/root", "/a", "/b"]),
            ("", "", []),
        ],
        ids=["root-only", "root-plus-allowlist", "ticket-none"],
    )
    def test_targets(self, tmp_path, monkeypatch, repo_root, label, expected):
        monkeypatch.setenv("FAKE_TARGETS", label)
        _fakebin(tmp_path, monkeypatch, docker='printf "%s\\n" "$FAKE_TARGETS"\n')
        assert broker.LabelCaller("cld_x", "task-agent", repo_root).targets() == expected


class TestDispatchRunTests:
    """Full-script dispatch: SSH_ORIGINAL_COMMAND in, `docker run` argv out --
    proves the dispatcher's kind branch, LabelCaller.repo and the --repo
    argv consumption compose for both ticket and v1 callers."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        return _fakebin(tmp_path, monkeypatch, docker="""
cmd="$1"; shift
case "$cmd" in
  inspect)
    fmt=""
    while [ $# -gt 0 ]; do
      case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
    done
    case "$fmt" in
      *org.cld.kind*)      printf '%s\\n' "${FAKE_KIND:-}" ;;
      *org.cld.repo-root*) printf '%s\\n' "${FAKE_REPO_ROOT:-}" ;;
      *range*)             printf '%s' "${FAKE_LABELS:-}" ;;
    esac
    ;;
  image) echo sha256:img123 ;;
  run) echo "DOCKER_RUN $*" ;;
esac
""", jj="echo rev123\n")

    @pytest.fixture
    def repos(self, tmp_path):
        for name in ("lide-api", "diskuze-api"):
            (tmp_path / name / ".jj").mkdir(parents=True)
        return tmp_path

    def _dispatch(self, session: str, argv: list, env_overrides: dict) -> subprocess.CompletedProcess:
        payload = base64.b64encode(b"".join(a.encode() + b"\0" for a in argv)).decode()
        env = dict(os.environ)
        env["CLD_BROKER_CONF"] = "/nonexistent"
        env["SSH_ORIGINAL_COMMAND"] = f"run-tests {session} {payload}"
        env["VENV_CACHE_ROOT"] = ""
        env.update(env_overrides)
        return subprocess.run([sys.executable, str(BROKER_PY)], capture_output=True, text=True, env=env)

    def test_ticket_caller_with_repo_flag(self, repos):
        labels = (
            f"org.cld.repo.lide-api={repos}/lide-api\n"
            f"org.cld.repo.diskuze-api={repos}/diskuze-api\n"
        )
        result = self._dispatch(
            "cld_ticket_x", ["--repo", "diskuze-api", "-k", "login"],
            {"FAKE_KIND": "ticket", "FAKE_LABELS": labels},
        )
        assert result.returncode == 0, result.stderr
        assert f"-v {repos}/diskuze-api:/repo" in result.stdout
        assert "-e REVISION=rev123" in result.stdout
        assert result.stdout.rstrip().endswith("runtests:latest -k login")
        assert "--repo" not in result.stdout

    def test_ticket_caller_multi_repo_without_flag_denied(self, repos):
        labels = (
            f"org.cld.repo.lide-api={repos}/lide-api\n"
            f"org.cld.repo.diskuze-api={repos}/diskuze-api\n"
        )
        result = self._dispatch(
            "cld_ticket_x", ["-k", "login"],
            {"FAKE_KIND": "ticket", "FAKE_LABELS": labels},
        )
        assert result.returncode == 3
        assert "pass --repo" in result.stderr

    def test_v1_caller_unchanged(self, repos):
        result = self._dispatch(
            "cld_agent_lide-api_fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api"},
        )
        assert result.returncode == 0, result.stderr
        assert f"-v {repos}/lide-api:/repo" in result.stdout
        assert result.stdout.rstrip().endswith("runtests:latest -k login")

    def test_venv_cache_wired(self, repos, tmp_path):
        cache = tmp_path / "cache"
        result = self._dispatch(
            "cld_agent_lide-api_fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api",
             "VENV_CACHE_ROOT": str(cache)},
        )
        assert result.returncode == 0, result.stderr
        assert f"-v {cache}:/cache" in result.stdout
        assert "-e VENV_CACHE_IMAGE_ID=sha256:img123" in result.stdout
        assert all((cache / d).is_dir() for d in ("venvs", "poetry", "locks"))

    def test_venv_cache_disabled(self, repos):
        result = self._dispatch(
            "cld_agent_lide-api_fix", ["-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api"},
        )
        assert result.returncode == 0, result.stderr
        assert ":/cache" not in result.stdout
        assert "VENV_CACHE_IMAGE_ID" not in result.stdout

    def test_v1_caller_repo_flag_denied(self, repos):
        result = self._dispatch(
            "cld_agent_lide-api_fix", ["--repo", "lide-api", "-k", "login"],
            {"FAKE_KIND": "task-agent", "FAKE_REPO_ROOT": f"{repos}/lide-api"},
        )
        assert result.returncode == 2
        assert "only for ticket containers" in result.stderr

    @pytest.mark.parametrize(
        "command,message",
        [
            ("Deploy cld_agent_x", "bad action"),
            ("deploy cld_agent_x", "unknown action 'deploy'"),
            ("run-tests ../../etc/passwd", "bad session id"),
        ],
        ids=["bad-action", "unknown-action", "bad-session"],
    )
    def test_malformed_command_denied(self, command, message):
        result = self._dispatch("", [], {"SSH_ORIGINAL_COMMAND": command})
        assert result.returncode == 2
        assert message in result.stderr


class TestDispatchCli:
    """Full-script host route (`cld-brokerctl run`): arguments on the script's own
    command line select CliCaller, so the caller's repo, revision, session and
    targets come from flags and cwd, with no container or docker label read."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        return _fakebin(tmp_path, monkeypatch, docker="""
[ "$1" = run ] && printf 'DOCKER_RUN\\n' && printf '%s\\n' "$@"
""", jj="""
printf '%s\\n' "$*" >> "$FAKE_JJ_LOG"
echo rev123
""", cld="""
printf '%s\\n' "$@"
""")

    @pytest.fixture
    def repo(self, tmp_path):
        (tmp_path / "my.repo" / ".jj").mkdir(parents=True)
        (tmp_path / "my.repo" / "sub").mkdir()
        return tmp_path / "my.repo"

    def _run(self, tmp_path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, CLD_BROKER_CONF="/nonexistent", FAKE_JJ_LOG=str(tmp_path / "jj.log"),
                   VENV_CACHE_ROOT="")
        env.pop("SSH_ORIGINAL_COMMAND", None)
        return subprocess.run([sys.executable, str(BROKER_PY), *args], cwd=cwd,
                              capture_output=True, text=True, env=env)

    def test_run_tests_defaults_from_cwd(self, tmp_path, repo):
        result = self._run(tmp_path, repo / "sub", "run-tests", "-k", "two words", "-r", "x")
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert f"{repo}:/repo" in lines
        assert "REVISION=rev123" in lines
        assert lines[-5:] == ["runtests:latest", "-k", "two words", "-r", "x"]
        assert (tmp_path / "jj.log").read_text().splitlines() == [
            f"-R {repo} log --no-graph -n1 -r @ -T commit_id"]

    def test_revset_and_repo_flags(self, tmp_path, repo):
        result = self._run(tmp_path, tmp_path, "-C", str(repo), "-r", "main", "run-tests")
        assert result.returncode == 0, result.stderr
        assert "-r main" in (tmp_path / "jj.log").read_text()

    def test_ticket_repo_selector_denied(self, tmp_path, repo):
        result = self._run(tmp_path, repo, "run-tests", "--repo", "lide-api")
        assert result.returncode == 2
        assert "pass -C <path>" in result.stderr

    def test_no_repo_above_cwd(self, tmp_path):
        result = self._run(tmp_path, tmp_path, "list-containers")
        assert result.returncode == 3
        assert "no repo at or above" in result.stderr

    @pytest.mark.parametrize(
        "flags,parent",
        [([], "cld_host_my-repo"), (["-s", "cld_mine"], "cld_mine")],
        ids=["default-session-sanitized", "explicit-session"],
    )
    def test_session_becomes_task_agent_parent(self, tmp_path, repo, flags, parent):
        result = self._run(tmp_path, repo, *flags, "task-agent", str(repo), "status")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["task-agent", "status", "--parent", parent]

    def test_extra_target_allowed(self, tmp_path, repo):
        (tmp_path / "other" / ".jj").mkdir(parents=True)
        other = tmp_path / "other"
        denied = self._run(tmp_path, repo, "task-agent", str(other), "status")
        allowed = self._run(tmp_path, repo, "-t", str(other), "task-agent", str(other), "status")
        assert denied.returncode == 3
        assert allowed.returncode == 0, allowed.stderr

    def test_bad_session_denied(self, tmp_path, repo):
        result = self._run(tmp_path, repo, "-s", "../x", "list-containers")
        assert result.returncode == 2
        assert "bad session id" in result.stderr


class TestParseEnvFile:
    """broker.conf and the repo .env are read, never sourced: shell quoting is
    honored, but nothing is expanded or executed."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("A=plain", {"A": "plain"}),
            ("export A=exported", {"A": "exported"}),
            ('A="two words"', {"A": "two words"}),
            ("A='single $HOME'", {"A": "single $HOME"}),
            ('A="$HOME"', {"A": "$HOME"}),
            ("A=value # comment", {"A": "value"}),
            ("A=pass#word", {"A": "pass#word"}),
            ("# A=commented", {}),
            ("echo hi", {}),
            ("A='unterminated", {}),
        ],
        ids=["plain", "export", "double-quoted", "single-quoted", "no-expansion",
             "trailing-comment", "hash-inside-value", "comment-line", "command", "bad-quoting"],
    )
    def test_line(self, tmp_path, line, expected):
        path = tmp_path / ".env"
        path.write_text(line + "\n")
        assert broker.parse_env_file(str(path)) == expected

    def test_conf_updates_only_exported_variables(self, tmp_path, monkeypatch, conf):
        path = tmp_path / "broker.conf"
        path.write_text("PATH=/conf/bin\nRUNTESTS_IMAGE=custom:1\n")
        monkeypatch.setenv("CLD_BROKER_CONF", str(path))
        monkeypatch.setenv("PATH", "/env/bin")
        monkeypatch.delenv("RUNTESTS_IMAGE", raising=False)
        broker.load_conf()
        assert os.environ["PATH"] == "/conf/bin"
        assert "RUNTESTS_IMAGE" not in os.environ
        assert broker._setting("RUNTESTS_IMAGE") == "custom:1"
        assert conf["PATH"] == "/conf/bin"


class TestCheckUrlAllowlisted:
    """C1: userinfo (user:pass@) must be stripped before the port, or
    'allowed.example:80@evil.com' passes the allowlist while the request
    actually connects to evil.com."""

    @pytest.fixture(autouse=True)
    def allowlist(self, conf):
        conf["GRAPHQL_URL_ALLOWLIST"] = "allowed.example"

    @pytest.mark.parametrize(
        "url,allowed",
        [
            ("http://allowed.example/graphql", True),
            ("http://evil.com/graphql", False),
            ("http://evilallowed.example/graphql", False),
            ("http://allowed.example:80@evil.com/", False),
            ("http://169.254.169.254/latest/meta-data/@allowed.example/", False),
            ("http:///graphql", False),
        ],
    )
    def test_allowlist_decision(self, url, allowed, capsys):
        if allowed:
            broker.check_url_allowlisted(url)
            return
        with pytest.raises(SystemExit) as excinfo:
            broker.check_url_allowlisted(url)
        assert _exit_code(excinfo) != 0
        assert "denied" in capsys.readouterr().err

    def test_userinfo_bypass_resolves_to_real_host_not_allowlisted_prefix(self, capsys):
        """Regression pin for the exact C1 bug: prove the denial names the real
        connect target (evil.com), not the allowlisted-looking prefix."""
        with pytest.raises(SystemExit):
            broker.check_url_allowlisted("http://allowed.example:80@evil.com/")
        err = capsys.readouterr().err
        assert "evil.com" in err
        assert "allowed.example" not in err.split("not in")[0]


class TestMaskOutput:
    @pytest.mark.parametrize(
        "raw,masked",
        [
            (b"DB_PASSWORD=hunter2, next", b"DB_PASSWORD=<redacted>, next"),
            (b"see /run/secrets/db.pw now", b"see /run/secrets/<redacted> now"),
            (b"mysql://root:pw@db:3306/x", b"mysql://<redacted>@db:3306/x"),
            (b'{"Token": "abc"}', b'{"Token": "<redacted>"}'),
            (b'{"token":\n"abc"}', b'{"token":\n"abc"}'),
            (b"\xff token=x\n", b"\xff token=<redacted>\n"),
        ],
        ids=["kv", "secrets-path", "url-userinfo", "json", "json-across-lines-untouched", "invalid-utf8"],
    )
    def test_mask(self, raw, masked):
        assert broker.mask_output(raw) == masked


class TestCapOutput:
    @pytest.mark.parametrize(
        "keep,expected",
        [("head", b"abc"), ("tail", b"def")],
    )
    def test_truncates_and_reports_on_stderr(self, keep, expected, capsysbinary):
        broker.cap_output(b"abcdef\n\n", 3, keep)
        out, err = capsysbinary.readouterr()
        assert out == expected
        assert b"truncated to 3 of 6 bytes" in err

    def test_under_limit_drops_trailing_newlines_only(self, capsysbinary):
        broker.cap_output(b"a\nb\n\n", 100, "head")
        out, err = capsysbinary.readouterr()
        assert out == b"a\nb"
        assert err == b""


class TestGraphqlEndpoints:
    """M2: do_graphql_endpoints must return cleanly (no output) for a secrets
    file with zero CLD_GRAPHQL_URL_* aliases, or none at all."""

    def _endpoints(self, monkeypatch, secrets: Path, capsys) -> str:
        monkeypatch.setattr(broker, "resolve_secrets_env_file", lambda repo: broker.SecretsEnv(".", str(secrets)))
        broker.do_graphql_endpoints(broker.Scope("cld_agent_test", "/repo"))
        return capsys.readouterr().out

    def test_empty_aliases_returns_cleanly(self, tmp_path, monkeypatch, capsys):
        secrets = tmp_path / ".env"
        secrets.write_text("SOME_OTHER_VAR=1\n")
        assert self._endpoints(monkeypatch, secrets, capsys) == ""

    def test_missing_secrets_file_returns_cleanly(self, tmp_path, monkeypatch, capsys):
        assert self._endpoints(monkeypatch, tmp_path / "does-not-exist.env", capsys) == ""

    def test_lists_configured_aliases(self, tmp_path, monkeypatch, capsys):
        secrets = tmp_path / ".env"
        secrets.write_text(
            "CLD_GRAPHQL_URL_DEV=http://dev.internal/graphql\n"
            "CLD_GRAPHQL_URL_STAGING_EU=http://staging.internal/graphql\n"
        )
        assert set(self._endpoints(monkeypatch, secrets, capsys).split()) == {"dev", "staging-eu"}


class TestResolveTargetAlias:
    def test_alias_url_and_credentials_come_from_the_env_file(self, tmp_path, monkeypatch):
        secrets = tmp_path / ".env"
        secrets.write_text(
            "CLD_GRAPHQL_URL_STAGING_EU=https://staging/graphql\n"
            "CLD_GRAPHQL_AUTH_STAGING_EU='Bearer abc'\n"
            'CLD_GRAPHQL_COOKIE_STAGING_EU="session=1; x=2"\n'
        )
        monkeypatch.setattr(broker, "resolve_secrets_env_file", lambda repo: broker.SecretsEnv(".", str(secrets)))
        ctx = broker.Scope("cld_agent_test", "/repo")
        assert broker.resolve_target(ctx, "staging-eu") == ("https://staging/graphql", "Bearer abc", "session=1; x=2")

    def test_unknown_alias_denied(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(broker, "resolve_secrets_env_file",
                            lambda repo: broker.SecretsEnv(".", str(tmp_path / ".env")))
        with pytest.raises(SystemExit) as excinfo:
            broker.resolve_target(broker.Scope("cld_agent_test", "/repo"), "nope")
        assert _exit_code(excinfo) == 3
        assert "CLD_GRAPHQL_URL_NOPE" in capsys.readouterr().err


class TestGraphqlStatusStale:
    """M5: do_graphql_status's 6th field (stale) compares the container's own
    served revision (REVISION env) against the session's current jj tip, as
    LabelCaller.revision resolves it.
    The stubbed `docker port` returns nothing, keeping the server in the
    'starting' state -- still enough to exercise the stale computation, which
    happens before the port/probe branching."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        return _fakebin(tmp_path, monkeypatch, docker="""
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
""", jj="""
[ "${FAKE_TIP_FAILS:-0}" = 1 ] && exit 1
echo "${FAKE_TIP:-}"
""")

    def _status(self, monkeypatch, tmp_path, capsys, **env) -> list:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        tip = broker.LabelCaller("cld_agent_test", "task-agent", str(tmp_path)).revision(str(tmp_path))
        broker.do_graphql_status(broker.Scope("cld_agent_test", str(tmp_path)), tip)
        return capsys.readouterr().out.rstrip("\n").split("\t")

    def test_matching_revision_is_not_stale(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_REV="abc123", FAKE_TIP="abc123")
        assert fields[0] == "starting"
        assert fields[3] == "abc123"
        assert fields[5] == "false"

    def test_mismatched_revision_is_stale(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_REV="abc123", FAKE_TIP="def456")
        assert fields[0] == "starting"
        assert fields[5] == "true"

    def test_unresolvable_tip_leaves_stale_empty(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_REV="abc123", FAKE_TIP_FAILS="1")
        assert fields[0] == "starting"
        assert fields[5] == ""

    def test_not_started_stale_empty(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_CONTAINER_EXISTS="0")
        assert fields == ["not_started", "", "", "", "", ""]

    def test_exited(self, monkeypatch, tmp_path, capsys):
        fields = self._status(monkeypatch, tmp_path, capsys, FAKE_RUNNING="false")
        assert fields == ["exited", "", "", "", "cld_gql_cld_agent_test", ""]


class TestGraphqlStartRepoMismatch:
    """do_graphql_start's idempotence is per repo: a server already running
    for repo A answering `graphql start --repo B` must refuse and name A --
    not hand a multi-repo ticket the wrong repo's endpoint. Same-repo (and
    pre-label, empty-label) reuse stays idempotent."""

    @pytest.fixture(autouse=True)
    def fakebin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(broker, "sweep_gql_orphans", lambda: None)
        monkeypatch.setattr(broker, "resolve_graphql_context", lambda repo: (
            broker.SecretsEnv(".", "/none/.env"), broker.GraphqlConfig("serve", "8000", "/graphql")))
        monkeypatch.setattr(broker, "do_graphql_status", lambda scope, tip: print("STATUS_CALLED"))
        return _fakebin(tmp_path, monkeypatch, docker="""
[ "$1" = inspect ] || exit 1
shift
fmt=""
while [ $# -gt 0 ]; do
  case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
done
case "$fmt" in
  '') exit 0 ;;
  '{{.State.Running}}') echo true ;;
  *gql-repo*) printf '%s\\n' "${FAKE_GQL_REPO:-}" ;;
esac
""")

    def _start(self, monkeypatch, repo: str, gql_repo: str) -> None:
        monkeypatch.setenv("FAKE_GQL_REPO", gql_repo)
        broker.do_graphql_start(broker.Scope("cld_ticket_x", repo), "rev")

    def test_cross_repo_reuse_refused_naming_the_running_repo(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._start(monkeypatch, repo="/repos/b", gql_repo="/repos/a")
        assert _exit_code(excinfo) == 3
        out, err = capsys.readouterr()
        assert "denied" in err
        assert "/repos/a" in err
        assert "graphql stop" in err
        assert "STATUS_CALLED" not in out

    def test_same_repo_reuse_stays_idempotent(self, monkeypatch, capsys):
        self._start(monkeypatch, repo="/repos/a", gql_repo="/repos/a")
        assert "STATUS_CALLED" in capsys.readouterr().out

    def test_unlabeled_server_stays_idempotent(self, monkeypatch, capsys):
        # v1 / pre-label containers carry no org.cld.gql-repo label.
        self._start(monkeypatch, repo="/repos/a", gql_repo="")
        assert "STATUS_CALLED" in capsys.readouterr().out


class TestGraphqlQuery:
    """do_graphql_query builds the body with json.dumps and returns the
    response body followed by a newline; a non-2xx status exits 1."""

    @pytest.fixture
    def sent(self, monkeypatch):
        sent = {}

        def fake_post(url, body, headers, timeout):
            sent.update(url=url, body=body, headers=headers)
            return sent.get("status", 200), body

        monkeypatch.setattr(broker, "resolve_target", lambda scope, target: ("http://stub/graphql", "Bearer t", ""))
        monkeypatch.setattr(broker, "http_post", fake_post)
        return sent

    def _query(self, query="{ __typename }", variables=""):
        broker.do_graphql_query(broker.Scope("cld_agent_test", "/repo"), "local", query, variables)

    def test_body_reaches_the_request_and_response_is_returned(self, sent, capsys):
        self._query()
        out = capsys.readouterr().out
        assert out.endswith("\n")
        assert json.loads(out) == {"query": "{ __typename }", "variables": {}}
        assert sent["headers"] == {"Content-Type": "application/json", "Authorization": "Bearer t"}

    def test_variables_are_merged_into_the_body(self, sent, capsys):
        self._query("query T($n: String!) { __type(name: $n) { name } }", '{"n": "Query"}')
        assert json.loads(capsys.readouterr().out)["variables"] == {"n": "Query"}

    def test_malformed_variables_denied(self, sent, capsys):
        with pytest.raises(SystemExit) as excinfo:
            self._query(variables="{nope")
        assert _exit_code(excinfo) == 2
        assert "malformed variables JSON" in capsys.readouterr().err

    def test_non_2xx_exits_1_after_printing_the_body(self, sent, capsys):
        sent["status"] = 502
        with pytest.raises(SystemExit) as excinfo:
            self._query()
        assert _exit_code(excinfo) == 1
        out, err = capsys.readouterr()
        assert json.loads(out)["query"] == "{ __typename }"
        assert "HTTP 502" in err


class TestHttpPost:
    """http_post keeps curl -sS's contract: any HTTP status is a response,
    redirects are not followed, URL userinfo becomes basic auth, and only a
    transport failure returns None."""

    @pytest.fixture
    def server(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.update(path=self.path, body=body, auth=self.headers.get("Authorization"))
                status = {"/redirect": 302, "/fail": 500}.get(self.path, 200)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/elsewhere")
                self.end_headers()
                self.wfile.write(b"reply:" + body)

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"127.0.0.1:{httpd.server_address[1]}", seen
        httpd.shutdown()

    @pytest.mark.parametrize(
        "path,status",
        [("/graphql", 200), ("/redirect", 302), ("/fail", 500)],
        ids=["ok", "redirect-not-followed", "error-status"],
    )
    def test_status_and_body(self, server, path, status):
        address, seen = server
        assert broker.http_post(f"http://{address}{path}", b"{}", {}, 5) == (status, b"reply:{}")
        assert seen["path"] == path

    def test_userinfo_becomes_basic_auth(self, server):
        address, seen = server
        broker.http_post(f"http://user:p%40ss@{address}/graphql", b"{}", {}, 5)
        assert seen["auth"] == "Basic " + base64.b64encode(b"user:p@ss").decode()

    def test_transport_failure_is_none(self, capsys):
        assert broker.http_post("http://127.0.0.1:1/graphql", b"{}", {}, 5) is None
        assert "graphql request failed" in capsys.readouterr().err


class TestDecodeArgv:
    @pytest.mark.parametrize(
        "raw,argv",
        [(b"", []), (b"a\0b\0", ["a", "b"]), (b"a\0\0b", ["a", "", "b"])],
        ids=["empty", "nul-terminated", "empty-arg-kept"],
    )
    def test_decode(self, raw, argv):
        assert broker.decode_argv(base64.b64encode(raw).decode()) == argv

    def test_bad_payload_denied(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            broker.decode_argv("not base64!")
        assert _exit_code(excinfo) == 2
