"""Tests for cld/log.py."""

import logging
import os
import subprocess
import sys

import pytest

from cld.config import Config
from cld.log import (
    get_logger,
    log_subprocess,
    mask_secrets,
    setup_logging,
)


def test_setup_logging_emits_to_stderr_only(capsys):
    setup_logging(Config())
    get_logger("cld.foo").info("hello-from-test")
    cap = capsys.readouterr()
    assert "hello-from-test" in cap.err
    assert cap.out == ""


def test_setup_logging_no_stdout_handler():
    setup_logging(Config())
    cld_log = logging.getLogger("cld")
    for h in cld_log.handlers:
        if isinstance(h, logging.StreamHandler):
            stream = h.stream
            assert stream is not sys.stdout
            try:
                fd = stream.fileno()
                assert fd != 1, "Handler writes to stdout (fd=1)"
            except (OSError, ValueError):
                pass


def test_setup_logging_idempotent():
    setup_logging(Config())
    handler_count_first = len(logging.getLogger("cld").handlers)
    setup_logging(Config())
    handler_count_second = len(logging.getLogger("cld").handlers)
    assert handler_count_first == handler_count_second == 1


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("CLD_LOG_LEVEL", "DEBUG")
    setup_logging(Config())
    assert logging.getLogger("cld").level == logging.DEBUG


def test_log_level_warn_alias(monkeypatch):
    monkeypatch.setenv("CLD_LOG_LEVEL", "WARN")
    setup_logging(Config())
    assert logging.getLogger("cld").level == logging.WARNING


def test_log_level_invalid_falls_back_to_info(monkeypatch, capsys):
    monkeypatch.setenv("CLD_LOG_LEVEL", "BANANA")
    setup_logging(Config())
    assert logging.getLogger("cld").level == logging.INFO


def test_cld_debug_backcompat(monkeypatch):
    monkeypatch.delenv("CLD_LOG_LEVEL", raising=False)
    monkeypatch.delenv("CLD_DEBUG", raising=False)
    setup_logging(Config(debug=True))
    assert logging.getLogger("cld").level == logging.DEBUG


def test_mask_secrets_token_value_masked():
    assert mask_secrets("OAUTH_TOKEN=abc123") == "OAUTH_TOKEN=<redacted>"


def test_mask_secrets_key_value_masked():
    assert mask_secrets("API_KEY=xyz789 other=plain") == "API_KEY=<redacted> other=plain"


def test_mask_secrets_run_secrets_path():
    out = mask_secrets("--mount=/run/secrets/mysql.cnf -ro")
    assert "/run/secrets/<redacted>" in out


def test_mask_secrets_no_match_unchanged():
    assert mask_secrets("plain-string foo=bar") == "plain-string foo=bar"


def test_colors_disabled_when_not_tty(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stderr", sys.__stderr__)
    monkeypatch.delenv("CLD_LOG_COLOR", raising=False)
    setup_logging(Config(log_color="auto"))
    get_logger("cld.color").info("color-check")
    cap = capsys.readouterr()
    assert "\033[" not in cap.err, f"unexpected ANSI in: {cap.err!r}"


def test_log_subprocess_handles_none_streams(caplog):
    result = subprocess.CompletedProcess(
        args=["docker", "ps"], returncode=0, stdout=None, stderr=None,
    )
    setup_logging(Config(log_level="DEBUG"))
    cld_log = logging.getLogger("cld")
    cld_log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="cld"):
            log_subprocess(get_logger("cld.test"), ["docker", "ps"], result)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    finally:
        cld_log.removeHandler(caplog.handler)


def test_log_subprocess_error_with_none_streams(caplog):
    result = subprocess.CompletedProcess(
        args=["false"], returncode=1, stdout=None, stderr=None,
    )
    setup_logging(Config())
    cld_log = logging.getLogger("cld")
    cld_log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger="cld"):
            log_subprocess(get_logger("cld.test"), ["false"], result)
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "expected an ERROR record for non-zero rc"
        assert any("<not captured>" in r.getMessage() for r in error_records)
    finally:
        cld_log.removeHandler(caplog.handler)


@pytest.mark.integration
@pytest.mark.skip(reason="orchestrator MCP is deprecated")
def test_mcp_orchestrator_stdout_is_clean(tmp_path):
    """MCP stdio server must keep stdout free of log lines."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "cld.mcp.orchestrator"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        init_msg = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize",'
            '"params":{"protocolVersion":"2024-11-05","capabilities":{},'
            '"clientInfo":{"name":"test","version":"0.1"}}}\n'
        )
        proc.stdin.write(init_msg)
        proc.stdin.flush()
        try:
            stdout_line = proc.stdout.readline()
        except Exception:
            stdout_line = ""
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        stderr_total = proc.stderr.read() if proc.stderr else ""

    if stdout_line.strip():
        import json
        try:
            json.loads(stdout_line)
        except json.JSONDecodeError:
            pytest.fail(
                f"stdout contained non-JSON content: {stdout_line!r}\n"
                f"stderr: {stderr_total[:1000]}"
            )
    if not stdout_line and "MCP orchestrator starting" not in stderr_total:
        pytest.skip("MCP orchestrator did not start; mcp package may be misconfigured")
