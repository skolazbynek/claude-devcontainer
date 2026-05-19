"""Stdlib-only logging setup for the ``cld`` package.

Exposes ``setup_logging``, ``get_logger``, ``log_subprocess`` and
``mask_secrets``. ``setup_logging`` is idempotent and attaches a single
``StreamHandler`` on ``logging.getLogger("cld")`` writing to ``sys.stderr``.
"""

import logging
import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cld.config import Config

_NC = "\033[0m"
_DIM = "\033[2m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD_RED = "\033[1;31m"
_RED_ON_WHITE = "\033[1;31;47m"
_GREEN = "\033[32m"

_LEVEL_COLORS = {
    logging.DEBUG: _DIM,
    logging.WARNING: _YELLOW,
    logging.ERROR: _BOLD_RED,
    logging.CRITICAL: _RED_ON_WHITE,
}

_LEVEL_SHORT = {"WARNING": "WARN"}

_FORMAT = "%(asctime)s %(levelname_short)-5s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"

_TAIL_BYTES = 4096


class _LazyStderrHandler(logging.StreamHandler):
    """StreamHandler that resolves ``sys.stderr`` at each emit.

    Plain ``StreamHandler(sys.stderr)`` caches the stream object at
    construction time, missing later patches (pytest's ``capsys`` /
    typer's ``CliRunner``). This subclass re-reads ``sys.stderr`` live.

    Internal but also imported by ``cld.config`` (which needs a
    handler before ``setup_logging`` runs). Keep this and
    ``_CldFormatter`` stable for that consumer.
    """

    def __init__(self):
        super().__init__()

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


class _CldFormatter(logging.Formatter):
    def __init__(self, use_color: bool):
        super().__init__(_FORMAT, datefmt=_DATEFMT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_short = _LEVEL_SHORT.get(record.levelname, record.levelname)
        text = super().format(record)
        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelno)
            if color:
                text = f"{color}{text}{_NC}"
        return text


def _resolve_level(cfg: "Config") -> int:
    env_val = os.environ.get("CLD_LOG_LEVEL")
    cfg_level = getattr(cfg, "log_level", "INFO")
    cfg_debug = getattr(cfg, "debug", False)
    raw: str | None = None
    if env_val:
        raw = env_val
    elif cfg_level and cfg_level.upper() != "INFO":
        raw = cfg_level
    elif cfg_debug:
        return logging.DEBUG
    else:
        return logging.INFO
    name = raw.strip().upper()
    if name == "WARN":
        name = "WARNING"
    value = logging.getLevelName(name)
    if isinstance(value, int):
        return value
    logging.getLogger("cld.log").warning(
        "Invalid log level %r; falling back to INFO", raw
    )
    return logging.INFO


def _resolve_color(cfg: "Config") -> bool:
    mode = (os.environ.get("CLD_LOG_COLOR") or getattr(cfg, "log_color", "auto") or "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if mode != "auto":
        mode = "auto"
    stderr = getattr(sys, "stderr", None)
    isatty = getattr(stderr, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except (ValueError, OSError):
        return False


def setup_logging(cfg: "Config", *, force_stderr: bool = False) -> None:
    """Configure the ``cld`` logger with a single stderr StreamHandler.

    Idempotent: existing handlers on ``logging.getLogger("cld")`` are removed
    before the new one is attached. ``force_stderr`` is a future-proofing
    flag; the stream is always ``sys.stderr``.
    """
    level = _resolve_level(cfg)
    use_color = _resolve_color(cfg)

    cld_log = logging.getLogger("cld")
    for h in list(cld_log.handlers):
        cld_log.removeHandler(h)

    handler = _LazyStderrHandler()
    handler.setFormatter(_CldFormatter(use_color))

    cld_log.addHandler(handler)
    cld_log.setLevel(level)
    cld_log.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _tail(data: str | bytes | None) -> str:
    if data is None:
        return "<not captured>"
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8", errors="replace")
        except Exception:
            data = repr(data)
    if len(data) > _TAIL_BYTES:
        return data[-_TAIL_BYTES:]
    return data


def log_subprocess(
    log: logging.Logger,
    cmd: list[str],
    result: subprocess.CompletedProcess,
) -> None:
    rendered = " ".join(cmd)
    log.debug("$ %s", rendered)
    log.debug("-> rc=%d", result.returncode)
    if result.returncode != 0:
        log.error(
            "$ %s failed (rc=%d)\nstdout: %s\nstderr: %s",
            rendered,
            result.returncode,
            _tail(result.stdout),
            _tail(result.stderr),
        )


_SECRET_KV_RE = re.compile(
    r"(?i)(TOKEN|KEY|SECRET|PASSWORD)=[^\s,'\"]+"
)
_SECRET_PATH_RE = re.compile(r"/run/secrets/[\w./-]+")


def mask_secrets(s: str) -> str:
    s = _SECRET_KV_RE.sub(r"\1=<redacted>", s)
    s = _SECRET_PATH_RE.sub("/run/secrets/<redacted>", s)
    return s
