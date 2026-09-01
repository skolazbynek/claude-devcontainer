"""Tests for `otelctl.sh doctor` (otel/otelctl.sh), a bash+embedded-python
health check for the standalone OTel pipeline. Docker is not assumed to be
available (task-agent containers generally have no daemon socket), so these
tests only exercise logic that doesn't require a real collector container:

- doctor_check_env: the shell-env verdict table (TestDoctorCheckEnv), run
  directly via bash following the tests/test_broker_sh.py precedent.
- doctor_summary: the ok/warn/fail/skip -> summary line + verdict mapping
  (TestDoctorSummary), same technique.
- The synthetic round-trip and its isolated temp-dir replay fallback
  (TestDoctorRoundTrip), run as a real end-to-end `otelctl.sh doctor`
  invocation against a stand-in OTLP/HTTP receiver (plain http.server, no
  Docker) that replicates just the HTTP contract the real collector exposes
  -- this is the same technique used to manually verify doctor against the
  three states in the task brief, now automated.

Never point a one-shot aggregate.py pass at a real stats directory that a
live --watch might also be using (see docs/design-otel-doctor.md) -- every
CLD_OTEL_DIR here is a fresh tmp_path, never a real one.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

OTELCTL_SH = Path(__file__).resolve().parent.parent / "otel" / "otelctl.sh"
AGGREGATE_PY = Path(__file__).resolve().parent.parent / "otel" / "aggregate.py"


def _extract_function(name: str) -> str:
    text = OTELCTL_SH.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {OTELCTL_SH}"
    return match.group(0)


def _run_function(name: str, body_snippet: str, env: dict) -> subprocess.CompletedProcess:
    script = "set -euo pipefail\n" + _extract_function(name) + "\n" + body_snippet
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


class TestDoctorCheckEnv:
    """doctor_check_env's shell-env verdict table -- run for real via bash,
    not reimplemented in Python, so a change to the real logic breaks this
    test rather than a parallel copy of it."""

    # doctor_check_env now reads through doctor_cfg_get/src/path (design Part
    # 2), which fall back to the shell environment only when CFG_NAMES has no
    # entry for a given variable -- exactly the case here, since this harness
    # never runs the resolver (doctor_check_cfg_resolve). Pulling in the real
    # cfg_* functions and legend/contradiction helpers (rather than stubbing
    # them) means a change to their fallback-to-shell behavior breaks this
    # test too, not just a hand-written copy of it.
    def _run_env(self, env_overrides: dict) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "PORT": "4318"}
        env.update(env_overrides)
        script = (
            "declare -a CFG_NAMES=() CFG_SRCS=() CFG_VALS=() CFG_PATHS=()\n"
            "declare -a TIER_TAGS_ARR=() TIER_PATHS_ARR=() TIER_FOUND_ARR=()\n"
            "DOCTOR_CFG_FROM_FILE=0\nDOCTOR_SUPPRESS_SHELL_OTEL=0\n"
            + _extract_function("doctor_cfg_get") + "\n"
            + _extract_function("doctor_cfg_src") + "\n"
            + _extract_function("doctor_cfg_path") + "\n"
            + _extract_function("doctor_cfg_report_legend") + "\n"
            + _extract_function("doctor_cfg_report_contradictions") + "\n"
            + _extract_function("doctor_check_env") + "\n"
            + "doctor_report() { echo \"$1|$2|$3|${4:-}\"; }\ndoctor_check_env"
        )
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], capture_output=True, text=True, env=env)

    def test_nothing_set_is_advisory_warn_only(self):
        result = self._run_env({})
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("warn|telemetry cfg|no telemetry configuration found")

    def test_fully_correct_env_is_all_ok(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
                "OTEL_RESOURCE_ATTRIBUTES": "service.name=my-session",
            }
        )
        assert result.returncode == 0, result.stderr
        states = [line.split("|", 1)[0] for line in result.stdout.strip().splitlines()]
        assert "fail" not in states
        assert states.count("ok") >= 5

    def test_missing_enable_telemetry_is_fail(self):
        result = self._run_env(
            {
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|telemetry cfg|CLAUDE_CODE_ENABLE_TELEMETRY is not set") for l in lines)

    def test_wrong_enable_telemetry_value_is_warn_not_fail(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "true",
                "OTEL_METRICS_EXPORTER": "otlp",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith('warn|telemetry cfg|CLAUDE_CODE_ENABLE_TELEMETRY=true') for l in lines)

    def test_exporter_missing_otlp_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "console"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|telemetry cfg|OTEL_METRICS_EXPORTER=console does not include otlp") for l in lines)

    def test_exporter_comma_list_including_otlp_is_ok(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "console,otlp"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|telemetry cfg|OTEL_METRICS_EXPORTER includes otlp") for l in lines)

    def test_metrics_protocol_overrides_general_protocol(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/json",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|telemetry cfg|protocol http/json") for l in lines)

    def test_grpc_protocol_is_fail(self):
        result = self._run_env({"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"})
        lines = result.stdout.strip().splitlines()
        assert any("OTEL_EXPORTER_OTLP_PROTOCOL=grpc" in l and l.startswith("fail") for l in lines)

    def test_endpoint_wrong_port_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:9999"}
        )
        lines = result.stdout.strip().splitlines()
        assert any("port 9999 != collector port 4318" in l for l in lines)

    def test_endpoint_https_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://localhost:4318"}
        )
        lines = result.stdout.strip().splitlines()
        assert any('scheme "https"' in l for l in lines)

    def test_endpoint_host_docker_internal_is_ok_never_resolution_tested(self):
        """Trap 1 from the architect's amendment: host.docker.internal is the
        *correct* endpoint for a containerised session and does not resolve
        on the host doctor itself runs on -- it must be accepted outright,
        never sent through a resolution probe that would report it broken.
        PYTHON_OK is deliberately left unset here: if this ever regressed to
        probing resolution, it would hit the no-python skip branch instead
        of ok, so this test would catch that too."""
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://host.docker.internal:4318"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|telemetry cfg|endpoint http://host.docker.internal:4318") for l in lines)

    def test_endpoint_literal_ip_is_ok_never_resolution_tested(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://172.17.0.1:4318"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|telemetry cfg|endpoint http://172.17.0.1:4318") for l in lines)

    def test_endpoint_unrecognized_host_without_python_is_skip_not_fail(self):
        """Trap 2: no getent, and a missing python3 must not be inferred as
        an unresolvable host -- that would false-fail every environment
        without python3 for a hostname doctor simply can't check."""
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.com:4318", "PYTHON_OK": "0"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith('skip|telemetry cfg|endpoint host "example.com" resolution') for l in lines)
        assert not any('does not resolve' in l for l in lines)

    def test_endpoint_unresolvable_host_with_python_is_fail(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://totally-not-a-real-host-xyz123.invalid:4318",
                "PYTHON_OK": "1",
                "PYTHON": "python3",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any('host "totally-not-a-real-host-xyz123.invalid" does not resolve' in l and l.startswith("fail") for l in lines)

    def test_endpoint_resolvable_but_unrecognized_host_is_fail(self):
        """A hostname that resolves fine but isn't loopback/host.docker.internal/
        a literal IP is still a fail -- most likely a copy-pasted endpoint
        pointing at a different machine's collector."""
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.com:4318",
                "PYTHON_OK": "1",
                "PYTHON": "python3",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            'host "example.com" is neither loopback, host.docker.internal, nor a literal IP' in l and l.startswith("fail")
            for l in lines
        )

    def test_resource_attributes_with_space_is_fail(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_RESOURCE_ATTRIBUTES": "service.name=my session"}
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|telemetry cfg|OTEL_RESOURCE_ATTRIBUTES contains a space") for l in lines)

    def test_cumulative_temporality_is_fail(self):
        result = self._run_env(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "Cumulative",
            }
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|telemetry cfg|OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative") for l in lines)

    def test_include_session_id_false_is_warn(self):
        result = self._run_env(
            {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_INCLUDE_SESSION_ID": "false"}
        )
        lines = result.stdout.strip().splitlines()
        assert any("collapses into unknown-session.json" in l for l in lines)


class TestDoctorCfgResolver:
    """doctor_check_cfg_resolve + doctor_check_env driven together, against
    real settings files under tmp_path (design Part 2, D12-D20). Every tier
    is pointed at a tmp file via DOCTOR_SETTINGS_* so the suite never reads a
    real ~/.claude (see _settings_lib_py's "test hazard" note) and never
    depends on cwd."""

    def _run(self, tmp_path, tiers=None, env_overrides=None, cwd=None):
        tiers = tiers or {}
        env = {"PATH": os.environ["PATH"], "PORT": "4318", "PYTHON_OK": "1", "PYTHON": sys.executable}
        for tag in ("managed", "local", "project", "user"):
            path = tmp_path / f"{tag}.json"
            if tag in tiers:
                path.write_text(json.dumps(tiers[tag]))
            env[f"DOCTOR_SETTINGS_{tag.upper()}"] = str(path)
        if env_overrides:
            env.update(env_overrides)
        script = (
            "declare -a CFG_NAMES=() CFG_SRCS=() CFG_VALS=() CFG_PATHS=()\n"
            "declare -a TIER_TAGS_ARR=() TIER_PATHS_ARR=() TIER_FOUND_ARR=()\n"
            "DOCTOR_CFG_FROM_FILE=0\nDOCTOR_SUPPRESS_SHELL_OTEL=0\n"
            + _extract_function("_settings_lib_py") + "\n"
            + _extract_function("_doctor_cfg_resolver_py") + "\n"
            + _extract_function("doctor_check_cfg_resolve") + "\n"
            + _extract_function("doctor_cfg_get") + "\n"
            + _extract_function("doctor_cfg_src") + "\n"
            + _extract_function("doctor_cfg_path") + "\n"
            + _extract_function("doctor_cfg_report_legend") + "\n"
            + _extract_function("doctor_cfg_report_contradictions") + "\n"
            + _extract_function("doctor_check_env") + "\n"
            + "doctor_report() { echo \"$1|$2|$3|${4:-}\"; }\n"
            "doctor_check_cfg_resolve\ndoctor_check_env"
        )
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            capture_output=True, text=True, env=env, cwd=cwd or str(tmp_path),
        )

    def test_precedence_managed_beats_local_beats_project_beats_user(self, tmp_path):
        result = self._run(
            tmp_path,
            tiers={
                "managed": {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}},
                "local": {"env": {"OTEL_METRICS_EXPORTER": "otlp"}},
                "project": {"env": {"OTEL_METRICS_EXPORTER": "console"}},
                "user": {"env": {"OTEL_METRICS_EXPORTER": "console", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}},
            },
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|telemetry cfg|telemetry enabled [managed]") for l in lines), result.stdout
        assert any(l.startswith("ok|telemetry cfg|OTEL_METRICS_EXPORTER includes otlp [local]") for l in lines), result.stdout
        assert any(l.startswith("ok|telemetry cfg|protocol http/json [user]") for l in lines), result.stdout

    def test_unparseable_file_is_fail_and_its_settings_are_not_applied(self, tmp_path):
        """D18: a broken file means none of ITS settings apply -- not just
        the telemetry ones -- so a value that would otherwise resolve from
        that tier must fall through to the next one (or go unset), and the
        break itself is reported as its own fail."""
        (tmp_path / "user.json").write_text("{not valid json")
        result = self._run(
            tmp_path,
            tiers={"project": {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "otlp"}}},
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("fail|telemetry cfg|") and "not valid JSON" in l and "user.json" in l for l in lines), result.stdout
        assert any(l.startswith("ok|telemetry cfg|telemetry enabled [project]") for l in lines), result.stdout

    def test_empty_string_is_explicit_unset_not_missing(self, tmp_path):
        """F9/D19: "" in a settings file is Claude Code's documented way to
        cancel a variable -- distinct from the key being absent -- and must
        be reported as cleared, with the clearing file named, not as a plain
        "is not set"."""
        result = self._run(
            tmp_path,
            tiers={"user": {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": ""}}},
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            l.startswith("fail|telemetry cfg|CLAUDE_CODE_ENABLE_TELEMETRY is cleared to \"\" by")
            and "user.json" in l
            for l in lines
        ), result.stdout
        assert not any(l.startswith("fail|telemetry cfg|CLAUDE_CODE_ENABLE_TELEMETRY is not set") for l in lines), result.stdout

    def test_managed_generic_endpoint_drops_lower_tier_per_signal_value(self, tmp_path):
        """F11/D20: a managed generic OTEL_EXPORTER_OTLP_ENDPOINT makes Claude
        Code drop any lower-tier per-signal OTEL_EXPORTER_OTLP_METRICS_ENDPOINT
        at startup -- the inverse of the normal per-signal-beats-generic rule
        -- so doctor must warn about the drop and resolve the generic value,
        not silently keep validating the (dead) per-signal one."""
        result = self._run(
            tmp_path,
            tiers={
                "managed": {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}},
                "user": {"env": {"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://localhost:9999"}},
            },
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://localhost:9999 from user is dropped at startup" in l
            for l in lines
        ), result.stdout
        assert any(l.startswith("ok|telemetry cfg|endpoint http://localhost:4318 [managed]") for l in lines), result.stdout
        assert not any("port 9999" in l for l in lines), result.stdout

    def test_claude_code_child_session_withholds_otel_and_does_not_fail(self, tmp_path):
        """D16/F6: a wrapper's shell `export`s (no settings file involved)
        set up a fully healthy pipeline, but Claude Code withholds OTEL_*
        from tool subprocesses while still passing CLAUDE_CODE_ENABLE_TELEMETRY
        through. Naively reading the process env would see
        CLAUDE_CODE_ENABLE_TELEMETRY=1 (so the old any-var-set gate would not
        early-return) and then read every OTEL_* var as empty -- a false
        "not set" fail for a pipeline that is actually fine. This is the
        regression the design calls "the single most valuable new test"."""
        result = self._run(
            tmp_path,
            env_overrides={
                "CLAUDE_CODE_CHILD_SESSION": "1",
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
            },
        )
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1, result.stdout
        assert lines[0].startswith("warn|telemetry cfg|running inside Claude Code"), result.stdout
        assert "CLAUDE_CODE_CHILD_SESSION" in lines[0]
        assert not any(l.startswith("fail") for l in lines)

    def test_shell_export_shadowed_by_file_value_is_warn_not_fail(self, tmp_path):
        """D17: a live shell export overridden by a higher-precedence
        settings-file value (F2: file always wins) is dead but not wrong --
        must be a warn naming both the losing shell value and the winning
        file, never a fail."""
        result = self._run(
            tmp_path,
            tiers={
                "user": {
                    "env": {
                        "OTEL_METRICS_EXPORTER": "otlp",
                        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
                    }
                }
            },
            env_overrides={
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "console",
            },
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            l.startswith("warn|telemetry cfg|1 shell export shadowed by a settings-file value and has no effect")
            and "OTEL_METRICS_EXPORTER" in l
            for l in lines
        ), result.stdout
        assert not any(l.startswith("fail") for l in lines), result.stdout

    def test_shell_exports_shadowed_by_file_value_plural_grammar(self, tmp_path):
        """Same as above with two contradictions: the noun ('exports') and
        the verb ('have') must both pluralize, not just the noun."""
        result = self._run(
            tmp_path,
            tiers={
                "user": {
                    "env": {
                        "OTEL_METRICS_EXPORTER": "otlp",
                        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
                    }
                }
            },
            env_overrides={
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "console",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            },
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            l.startswith("warn|telemetry cfg|2 shell exports shadowed by a settings-file value and have no effect")
            for l in lines
        ), result.stdout
        assert not any(l.startswith("fail") for l in lines), result.stdout


class TestDoctorCheckCollectorMountsDrift:
    """doctor_check_collector's config-edited-since-start sub-check
    (otel/otelctl.sh doctor_check_collector, the mounts branch). Docker
    itself is stubbed on PATH (tests/test_broker_sh.py precedent), so this
    exercises real bash + real `date`/`stat` -- only the docker daemon is
    faked, not the platform-portability logic under test.

    Regression coverage for the architect-flagged GNU-only `date -d` /
    `stat -c %Y` bug: on a platform where neither can be parsed, the drift
    sub-check must report `skip`, never `ok` -- an `ok` here would silently
    claim "config not edited since start" on a platform that was never
    actually checked.
    """

    @pytest.fixture
    def fakedocker(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text("""#!/usr/bin/env bash
if [ "$1" = "inspect" ]; then
    shift
    fmt=""
    while [ $# -gt 0 ]; do
        case "$1" in -f) fmt="$2"; shift 2 ;; *) shift ;; esac
    done
    case "$fmt" in
        '{{.State.Status}}') echo "${FAKE_STATUS:-running}" ;;
        '{{.State.ExitCode}}') echo "0" ;;
        '{{.State.Error}}') echo "" ;;
        '{{.State.RestartCount}}') echo "${FAKE_RESTART_COUNT:-0}" ;;
        '{{.Config.Image}}') echo "otel-image:test" ;;
        '{{.State.StartedAt}}') echo "${FAKE_STARTED_AT:-2024-01-01T00:00:00.000000000Z}" ;;
        '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}') echo "${FAKE_DATA_SRC:-}" ;;
        '{{range .Mounts}}{{if eq .Destination "/etc/otelcol-contrib/config.yaml"}}{{.Source}}{{end}}{{end}}') echo "${FAKE_CFG_SRC:-}" ;;
    esac
    exit 0
fi
exit 1
""")
        (bindir / "docker").chmod(0o755)
        return bindir

    def _run(self, fakedocker, tmp_path, env_overrides: dict, extra_path: str = "", cfg_mtime: float = None):
        here = tmp_path / "here"
        here.mkdir()
        state = tmp_path / "state"
        cfg = here / "otel-collector-config.yaml"
        cfg.write_text("receivers: {}\n")
        if cfg_mtime is not None:
            os.utime(cfg, (cfg_mtime, cfg_mtime))

        env = {
            "PATH": f"{extra_path}{':' if extra_path else ''}{fakedocker}:{os.environ['PATH']}",
            "CONTAINER_NAME": "cld-otel-collector",
            "DATA_DIR": str(state),
            "HERE": str(here),
            "FAKE_DATA_SRC": f"{state}/data",
            "FAKE_CFG_SRC": str(cfg),
        }
        env.update(env_overrides)
        return _run_function(
            "doctor_check_collector",
            'doctor_report() { echo "$1|$2|$3|${4:-}"; }\ndoctor_check_collector',
            env,
        )

    def test_config_untouched_since_start_is_ok(self, fakedocker, tmp_path):
        """cfg mtime predates started_at -- not edited since start."""
        started = time.time()
        result = self._run(
            fakedocker,
            tmp_path,
            {"FAKE_STARTED_AT": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(started))},
            cfg_mtime=started - 3600,
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("ok|mounts|/data ->") for l in lines), result.stdout

    def test_config_edited_after_start_is_warn(self, fakedocker, tmp_path):
        """cfg mtime postdates started_at -- edited since start."""
        started = time.time() - 3600
        result = self._run(
            fakedocker,
            tmp_path,
            {"FAKE_STARTED_AT": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(started))},
            cfg_mtime=time.time(),
        )
        lines = result.stdout.strip().splitlines()
        assert any(l.startswith("warn|mounts|") and "edited since collector started" in l for l in lines), result.stdout

    def test_unparseable_start_time_is_skip_not_ok(self, fakedocker, tmp_path):
        """Neither GNU `date -d` nor the BSD `date -j -u -f` fallback can
        parse this -- started_epoch stays empty, and the drift sub-check
        must not silently claim ok."""
        result = self._run(fakedocker, tmp_path, {"FAKE_STARTED_AT": "not-a-timestamp"})
        lines = result.stdout.strip().splitlines()
        assert any(
            l.startswith("skip|mounts|config-edited-since-start check|") and "could not be parsed" in l for l in lines
        ), result.stdout
        assert not any(l.startswith("ok|mounts|") for l in lines)

    def test_unreadable_mtime_is_skip_not_ok(self, fakedocker, tmp_path):
        """A `stat` that supports neither GNU `-c %Y` nor BSD `-f %m` must
        not be silently treated as 'not edited' -- pin the skip path
        distinct from the unparseable-start-time path above."""
        started = time.time() - 3600
        badstat_dir = tmp_path / "badstat-bin"
        badstat_dir.mkdir()
        (badstat_dir / "stat").write_text("#!/usr/bin/env bash\nexit 1\n")
        (badstat_dir / "stat").chmod(0o755)
        result = self._run(
            fakedocker,
            tmp_path,
            {"FAKE_STARTED_AT": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(started))},
            extra_path=str(badstat_dir),
        )
        lines = result.stdout.strip().splitlines()
        assert any(
            l.startswith("skip|mounts|config-edited-since-start check|") and "could not read" in l for l in lines
        ), result.stdout
        assert not any(l.startswith("ok|mounts|") for l in lines)


class TestDoctorSummary:
    """doctor_summary's ok/warn/fail/skip counts -> summary line + verdict.
    Regression-pins the comma-spacing bug (IFS=', ' only honors the first
    char of IFS when joining ${arr[*]}) and the shell-env-only-failure
    verdict branch."""

    def _run(self, counts: dict, fail_labels=(), cfg_from_file=False) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"]}
        labels_decl = " ".join(f'"{l}"' for l in fail_labels)
        body = f"""
DOCTOR_OK_COUNT={counts.get('ok', 0)}
DOCTOR_WARN_COUNT={counts.get('warn', 0)}
DOCTOR_FAIL_COUNT={counts.get('fail', 0)}
DOCTOR_SKIP_COUNT={counts.get('skip', 0)}
DOCTOR_FIRST_FAIL_DETAIL="first fail detail"
DOCTOR_FAIL_LABELS_ARR=({labels_decl})
DOCTOR_CFG_FROM_FILE={1 if cfg_from_file else 0}
doctor_summary
"""
        return _run_function("doctor_summary", body, env)

    def test_all_ok_no_failures_flowing(self):
        result = self._run({"ok": 4})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "4 ok -- telemetry is flowing\n"

    def test_counts_join_with_comma_space(self):
        result = self._run({"ok": 4, "warn": 3, "fail": 5, "skip": 3}, fail_labels=["docker"] * 5)
        first_line = result.stdout.splitlines()[0]
        assert first_line == "4 ok, 3 warnings, 5 failures, 3 skipped -- telemetry is NOT being collected"

    def test_singular_warning_and_failure(self):
        result = self._run({"ok": 1, "warn": 1, "fail": 1}, fail_labels=["docker"])
        first_line = result.stdout.splitlines()[0]
        assert "1 warning" in first_line
        assert "1 failure" in first_line
        assert "1 warnings" not in first_line
        assert "1 failures" not in first_line

    def test_next_line_only_when_failures_present(self):
        result_ok = self._run({"ok": 4})
        assert "next:" not in result_ok.stdout
        result_fail = self._run({"ok": 4, "fail": 1}, fail_labels=["docker"])
        assert result_fail.stdout.splitlines()[-1] == "next: first fail detail"

    def test_all_failures_telemetry_cfg_from_shell_softens_verdict(self):
        result = self._run({"ok": 4, "fail": 2}, fail_labels=["telemetry cfg", "telemetry cfg"])
        assert "the pipeline is healthy, but this shell will not export to it" in result.stdout

    def test_mixed_failures_including_non_telemetry_cfg_is_strict(self):
        result = self._run({"ok": 4, "fail": 2}, fail_labels=["telemetry cfg", "docker"])
        assert "telemetry is NOT being collected" in result.stdout

    def test_all_failures_telemetry_cfg_from_file_is_not_softened(self):
        """D15/D22: a broken settings *file* is authoritative and read once
        at Claude Code startup -- unlike a shell-only gap, it must not get
        the softened 'this shell will not export to it' wording, since the
        problem isn't specific to this shell at all."""
        result = self._run(
            {"ok": 4, "fail": 2}, fail_labels=["telemetry cfg", "telemetry cfg"], cfg_from_file=True
        )
        assert "Claude Code's telemetry config is broken" in result.stdout
        assert "the pipeline is healthy" not in result.stdout


# --- round trip + isolated replay fallback, no Docker ---------------------


class _FakeCollectorHandler(BaseHTTPRequestHandler):
    """Replicates just the HTTP contract doctor's port/round-trip checks
    depend on: 400 on a malformed body to /v1/metrics, 404 elsewhere, and on
    a well-formed export request, 200 + the raw body appended to
    raw-metrics.jsonl exactly like the real collector's `file` exporter
    (append: true) does."""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/metrics":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        with open(self.server.raw_path, "a") as f:
            f.write(body.decode("utf-8") + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_collector(tmp_path):
    port = _free_port()
    raw_path = tmp_path / "data" / "raw-metrics.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.touch()
    server = ThreadingHTTPServer(("127.0.0.1", port), _FakeCollectorHandler)
    server.raw_path = str(raw_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, raw_path
    server.shutdown()
    thread.join(timeout=5)


def _run_doctor(cld_otel_dir: Path, port: int, extra_env=None, timeout_secs=8):
    env = dict(os.environ)
    env.update(
        {
            "CLD_OTEL_DIR": str(cld_otel_dir),
            "CLD_OTEL_PORT": str(port),
            "PATH": os.environ["PATH"],
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(OTELCTL_SH), "doctor", "--timeout", str(timeout_secs)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestDoctorRoundTrip:
    def test_no_aggregator_falls_back_to_isolated_replay_and_warns(self, fake_collector, tmp_path):
        """Check 5: with the receiver reachable but no aggregate.py --watch
        running, doctor must not fail outright -- it runs an isolated
        temp-dir replay of aggregate.py (never touching the real stats dir)
        to prove the metric *would* aggregate correctly, and reports a warn
        telling the user to start the aggregator, not a fail."""
        port, raw_path = fake_collector
        result = _run_doctor(tmp_path, port)

        assert "[ ok ] port" in result.stdout
        round_trip_lines = [l for l in result.stdout.splitlines() if "round trip" in l]
        assert any(l.startswith("[warn]") for l in round_trip_lines), result.stdout
        assert any("no aggregator running" in l for l in result.stdout.splitlines())

        # The real stats dir must never have been touched by a one-shot pass.
        assert not (tmp_path / "stats").exists() or not list((tmp_path / "stats").rglob("*.json"))

    def test_live_aggregator_gets_a_real_green_round_trip(self, fake_collector, tmp_path):
        """With a real aggregate.py --watch process running against the same
        CLD_OTEL_DIR, the synthetic metric must land in and then be cleaned
        from a real stats file -- the happy path, not the replay fallback."""
        port, raw_path = fake_collector
        agg_proc = subprocess.Popen(
            ["python3", str(AGGREGATE_PY), "--watch"],
            env={**os.environ, "CLD_OTEL_DIR": str(tmp_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            time.sleep(0.5)
            result = _run_doctor(tmp_path, port)
        finally:
            agg_proc.terminate()
            try:
                agg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agg_proc.kill()

        round_trip_lines = [l for l in result.stdout.splitlines() if "round trip" in l]
        assert any(l.startswith("[ ok ]") for l in round_trip_lines), result.stdout
        assert any("stats file" in l for l in round_trip_lines)
        # Cleaned up afterwards -- no leftover synthetic stats file.
        assert not (tmp_path / "stats").exists() or not list((tmp_path / "stats").rglob("*doctorcheck*"))


def _run_otelctl(args, env_overrides=None, cwd=None):
    # CLD_OTEL_DIR sidesteps otelctl.sh's top-level `${CLD_OTEL_DIR:-$HOME/...}`
    # -- irrelevant to settings/settings install, but read unconditionally
    # under `set -u` even for those subcommands.
    env = {"PATH": os.environ["PATH"], "PORT": "4318", "CLD_OTEL_DIR": "/nonexistent-otel-dir"}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(OTELCTL_SH), *args], capture_output=True, text=True, env=env, cwd=cwd
    )


TELEMETRY_KEYS = [
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_RESOURCE_ATTRIBUTES",
]


class TestSettingsPrint:
    """`otelctl.sh settings` -- the print-only fragment (design category 7)."""

    def test_default_output_is_pure_json_with_five_keys(self):
        result = _run_otelctl(["settings"])
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert set(parsed.keys()) == {"env"}
        assert set(parsed["env"].keys()) == set(TELEMETRY_KEYS)
        assert all(isinstance(v, str) for v in parsed["env"].values())
        assert parsed["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"
        assert parsed["env"]["OTEL_RESOURCE_ATTRIBUTES"] == "service.name=my-session"

    def test_docker_swaps_host(self):
        result = _run_otelctl(["settings", "--docker"])
        parsed = json.loads(result.stdout)
        assert parsed["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://host.docker.internal:4318"

    def test_service_name_substitutes(self):
        result = _run_otelctl(["settings", "--service-name", "my-container"])
        parsed = json.loads(result.stdout)
        assert parsed["env"]["OTEL_RESOURCE_ATTRIBUTES"] == "service.name=my-container"

    def test_service_name_with_no_value_is_usage_error_not_a_silent_crash(self):
        # --service-name as the last arg leaves nothing for `shift 2` to
        # consume; under `set -e` that used to exit 1 with no message at all.
        result = _run_otelctl(["settings", "--service-name"])
        assert result.returncode == 2, result.stdout
        assert "usage" in result.stderr.lower()

    def test_service_name_empty_string_is_rejected(self):
        result = _run_otelctl(["settings", "--service-name", ""])
        assert result.returncode == 2, result.stdout
        assert "must not be empty" in result.stderr

    def test_stdout_carries_no_advice_text(self):
        # Every human-facing note goes to stderr; a naive `settings > frag.json`
        # must produce a file that's exactly the fragment.
        result = _run_otelctl(["settings"])
        json.loads(result.stdout)  # raises if stdout has anything but the fragment
        assert "relaunch" in result.stderr


class TestSettingsInstall:
    """`otelctl.sh settings install` -- the merge table (design category 8)."""

    def _install(self, target, extra_args=(), env_overrides=None):
        return _run_otelctl(["settings", "install", "--file", str(target), *extra_args], env_overrides)

    def test_creates_from_nothing(self, tmp_path):
        target = tmp_path / "settings.json"
        result = self._install(target)
        assert result.returncode == 0, result.stderr
        assert f"created {target}" in result.stdout
        data = json.loads(target.read_text())
        assert set(data["env"].keys()) == set(TELEMETRY_KEYS)

    def test_service_name_with_no_value_is_usage_error_not_a_silent_crash(self, tmp_path):
        target = tmp_path / "settings.json"
        result = self._install(target, extra_args=["--service-name"])
        assert result.returncode == 2, result.stdout
        assert "usage" in result.stderr.lower()
        assert not target.exists()

    def test_service_name_empty_string_is_rejected(self, tmp_path):
        target = tmp_path / "settings.json"
        result = self._install(target, extra_args=["--service-name", ""])
        assert result.returncode == 2, result.stdout
        assert "must not be empty" in result.stderr
        assert not target.exists()

    def test_unrelated_top_level_and_env_keys_survive_byte_for_byte(self, tmp_path):
        target = tmp_path / "settings.json"
        original = {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"PreToolUse": []},
            "env": {"SOME_OTHER_VAR": "keep-me"},
        }
        target.write_text(json.dumps(original, indent=2) + "\n")
        result = self._install(target)
        assert result.returncode == 0, result.stderr
        data = json.loads(target.read_text())
        assert data["permissions"] == original["permissions"]
        assert data["hooks"] == original["hooks"]
        assert data["env"]["SOME_OTHER_VAR"] == "keep-me"
        assert set(TELEMETRY_KEYS) <= set(data["env"].keys())

    def test_idempotent_rerun_reports_no_change(self, tmp_path):
        target = tmp_path / "settings.json"
        self._install(target)
        before = target.read_bytes()
        result = self._install(target)
        assert result.returncode == 0, result.stderr
        assert "already configured, no change" in result.stdout
        assert target.read_bytes() == before

    def test_conflicting_value_refuses_and_leaves_file_untouched(self, tmp_path):
        target = tmp_path / "settings.json"
        original = {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel.corp:4318"}}
        target.write_text(json.dumps(original, indent=2) + "\n")
        before = target.read_bytes()
        result = self._install(target)
        assert result.returncode == 1
        assert "refusing to change" in result.stderr
        assert "re-run with --force" in result.stderr
        assert target.read_bytes() == before

    def test_force_applies_the_conflicting_value(self, tmp_path):
        target = tmp_path / "settings.json"
        original = {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel.corp:4318"}}
        target.write_text(json.dumps(original, indent=2) + "\n")
        result = self._install(target, extra_args=["--force"])
        assert result.returncode == 0, result.stderr
        data = json.loads(target.read_text())
        assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"

    def test_invalid_json_aborts_with_file_untouched(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("{\"env\": {\n")
        before = target.read_bytes()
        result = self._install(target)
        assert result.returncode == 1
        assert "not valid JSON" in result.stderr
        assert target.read_bytes() == before

    def test_dry_run_writes_nothing(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("{}\n")
        before = target.read_bytes()
        result = self._install(target, extra_args=["--dry-run"])
        assert result.returncode == 0, result.stderr
        assert target.read_bytes() == before
        printed = json.loads(result.stdout)
        assert set(TELEMETRY_KEYS) <= set(printed["env"].keys())

    def test_docker_with_user_target_exits_2(self):
        result = _run_otelctl(["settings", "install", "--user", "--docker"])
        assert result.returncode == 2
        assert "only resolves inside a" in result.stderr

    def test_docker_with_file_target_is_allowed(self, tmp_path):
        target = tmp_path / "settings.json"
        result = self._install(target, extra_args=["--docker"])
        assert result.returncode == 0, result.stderr
        data = json.loads(target.read_text())
        assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://host.docker.internal:4318"


def _settings_merge_python_source() -> str:
    """The exact python program `settings install` feeds to $PYTHON -- the
    shared lib (_settings_lib_py) followed by the merge body -- extracted
    from the real script so a change to either can't silently drift from
    this test."""
    lib_src_match = re.search(r"<<'PYLIB'\n(.*?)\nPYLIB\n", _extract_function("_settings_lib_py"), re.DOTALL)
    assert lib_src_match
    install_src = _extract_function("settings_install")
    merge_match = re.search(r"_settings_lib_py; cat <<'PYEOF'\n(.*?)\nPYEOF\n", install_src, re.DOTALL)
    assert merge_match
    return lib_src_match.group(1) + "\n" + merge_match.group(1)


class TestSettingsInstallVerifyRestore:
    """D7's read-back verification (design category 9). Per
    feedback_verify_that_a_check_can_fail: prove the guard can actually fail,
    not just trust it, by pointing the real merge program at a Path.write_text
    stub that silently drops a key from the temp file before the atomic
    rename -- simulating the on-disk corruption the verify step exists to
    catch -- and asserting the original bytes come back."""

    # Drops an *unrelated* key -- the class of corruption D7 actually guards
    # against (our own five keys aren't checked by the verify step at all,
    # only that nothing else got clobbered).
    DROP_UNRELATED_KEY_FAULT = (
        "import pathlib, json as _json\n"
        "_orig_write_text = pathlib.Path.write_text\n"
        "def _faulty_write_text(self, data, *a, **kw):\n"
        "    if '.otelctl-tmp-' in self.name:\n"
        "        obj = _json.loads(data)\n"
        "        obj.get('env', {}).pop('SOME_OTHER_VAR', None)\n"
        "        data = _json.dumps(obj, indent=2) + '\\n'\n"
        "    return _orig_write_text(self, data, *a, **kw)\n"
        "pathlib.Path.write_text = _faulty_write_text\n"
    )

    # Truncates mid-write into unparseable JSON -- for the created=True case
    # there are no pre-existing keys to compare against, so the only way the
    # verify step can catch a corrupted write is assertion 1 (it must parse).
    TRUNCATE_FAULT = (
        "import pathlib\n"
        "_orig_write_text = pathlib.Path.write_text\n"
        "def _faulty_write_text(self, data, *a, **kw):\n"
        "    if '.otelctl-tmp-' in self.name:\n"
        "        data = data[: len(data) // 2]\n"
        "    return _orig_write_text(self, data, *a, **kw)\n"
        "pathlib.Path.write_text = _faulty_write_text\n"
    )

    def _run_merge(self, target, source, env_overrides=None):
        env = {"PATH": os.environ["PATH"]}
        env.update(
            {
                "SETTINGS_TARGET_KIND": "file",
                "SETTINGS_TARGET_PATH": str(target),
                "SETTINGS_FORCE": "0",
                "SETTINGS_DRY_RUN": "0",
                "SETTINGS_VARS": "\n".join(f"{k}=v-{k}" for k in TELEMETRY_KEYS),
            }
        )
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(["python3", "-c", source], capture_output=True, text=True, env=env)

    def test_unfaulted_merge_sanity_check(self, tmp_path):
        # Same program, no fault injected -- must succeed, proving the merge
        # source extraction itself is correct before trusting the fault test.
        target = tmp_path / "settings.json"
        result = self._run_merge(target, _settings_merge_python_source())
        assert result.returncode == 0, result.stderr
        data = json.loads(target.read_text())
        assert data["env"]["OTEL_RESOURCE_ATTRIBUTES"] == "v-OTEL_RESOURCE_ATTRIBUTES"

    def test_corrupted_write_is_detected_and_original_bytes_restored(self, tmp_path):
        target = tmp_path / "settings.json"
        original = {"env": {"SOME_OTHER_VAR": "keep-me"}}
        original_text = json.dumps(original, indent=2) + "\n"
        target.write_text(original_text)

        source = self.DROP_UNRELATED_KEY_FAULT + _settings_merge_python_source()
        result = self._run_merge(target, source)

        assert result.returncode == 1, result.stdout
        assert "verification failed" in result.stderr
        assert "restored the original file" in result.stderr
        assert target.read_text() == original_text, "original bytes must be restored, not left half-written"

    def test_corrupted_write_on_created_file_removes_it(self, tmp_path):
        # created=True path: nothing pre-existed, so "restore" means unlink,
        # not write-back (there is no original_bytes to write).
        target = tmp_path / "settings.json"
        source = self.TRUNCATE_FAULT + _settings_merge_python_source()
        result = self._run_merge(target, source)

        assert result.returncode == 1, result.stdout
        assert "verification failed" in result.stderr
        assert not target.exists(), "a from-nothing create must be rolled back, not left half-written"

    # Unlike DROP_UNRELATED_KEY_FAULT (which corrupts only the bytes written
    # to disk), this mutates the in-memory `settings` dict itself -- the
    # class of bug D7's baseline comparison exists to catch is a *future
    # edit to the merge code* that touches a key outside our five, not just
    # an on-disk write fault. If the baseline used for comparison is the
    # same object the merge mutated (`baseline = existing`), this fault is
    # invisible: the baseline "sees" its own corruption and agrees with the
    # corrupted reread. The baseline must come from a copy/reparse taken
    # before any mutation.
    FUTURE_EDIT_FAULT = (
        "import json as _json\n"
        "_orig_dumps = _json.dumps\n"
        "_faulted = []\n"
        "def _faulty_dumps(obj, *a, **kw):\n"
        "    if not _faulted and isinstance(obj, dict) and 'env' in obj:\n"
        "        _faulted.append(True)\n"
        "        obj['env']['SOME_OTHER_VAR'] = 'corrupted-by-a-future-bug'\n"
        "    return _orig_dumps(obj, *a, **kw)\n"
        "_json.dumps = _faulty_dumps\n"
    )

    def test_merge_code_corrupting_an_unrelated_key_in_memory_is_detected(self, tmp_path):
        target = tmp_path / "settings.json"
        original = {"env": {"SOME_OTHER_VAR": "keep-me"}}
        original_text = json.dumps(original, indent=2) + "\n"
        target.write_text(original_text)

        source = self.FUTURE_EDIT_FAULT + _settings_merge_python_source()
        result = self._run_merge(target, source)

        assert result.returncode == 1, result.stdout
        assert "verification failed" in result.stderr
        assert "restored the original file" in result.stderr
        assert target.read_text() == original_text, "original bytes must be restored, not left corrupted"
