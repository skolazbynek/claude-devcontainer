"""Detached process control for the bridge, mirroring ``broker/cld-brokerctl.sh``.

Everything lives under ``~/.cld/bridge``: the PID file and the log. The daemon is
just ``cld bridge mattermost`` in a new session with its output redirected, so the
foreground command stays the one implementation and remains usable for debugging.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from cld.log import get_logger

log = get_logger(__name__)

_BRIDGE_DIR = Path(os.environ.get("CLD_BRIDGE_DIR", "~/.cld/bridge")).expanduser()
_STOP_TIMEOUT = 10.0


def bridge_dir() -> Path:
    _BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    return _BRIDGE_DIR


def pid_file(name: str = "mattermost") -> Path:
    return bridge_dir() / f"{name}.pid"


def log_file(name: str = "mattermost") -> Path:
    return bridge_dir() / f"{name}.log"


def _is_ours(pid: int) -> bool:
    """Guard against PID reuse: the recorded pid must still be a cld process.

    Best-effort and Linux-only; where /proc is absent we fall back to "it exists",
    which is what cld-brokerctl does.
    """
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return True
    return "cld" in cmdline.read_bytes().decode("utf-8", "replace")


def running_pid(name: str = "mattermost") -> int | None:
    """The live pid from the PID file, or None (clearing a stale file)."""
    path = pid_file(name)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, OSError):
        path.unlink(missing_ok=True)
        return None
    if not _is_ours(pid):
        path.unlink(missing_ok=True)
        return None
    return pid


def start(name: str = "mattermost") -> int:
    """Spawn the foreground command detached, append its output to the log, record the pid."""
    if (pid := running_pid(name)) is not None:
        raise RuntimeError(f"already running (pid {pid}) -- `cld bridge stop` first")

    handle = log_file(name).open("a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "cld", "bridge", name],
        stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    handle.close()
    pid_file(name).write_text(str(proc.pid))

    # A config error surfaces before the caller's shell returns, not silently in a log.
    time.sleep(1.0)
    if proc.poll() is not None:
        pid_file(name).unlink(missing_ok=True)
        tail = "\n".join(log_file(name).read_text().splitlines()[-15:])
        raise RuntimeError(f"bridge exited immediately (rc={proc.returncode}):\n{tail}")
    return proc.pid


def stop(name: str = "mattermost") -> int | None:
    """SIGTERM the daemon and wait for it to go. Returns the pid it stopped, or None."""
    pid = running_pid(name)
    if pid is None:
        return None
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + _STOP_TIMEOUT
    while time.time() < deadline:
        # running_pid, not a bare kill(pid, 0): a terminated process that nobody has
        # reaped yet is a zombie, and signalling a zombie still succeeds. Its cmdline
        # is empty, which _is_ours reads as gone -- so this exits when the process is
        # really dead rather than after the full timeout.
        if running_pid(name) != pid:
            break
        time.sleep(0.2)
    else:
        log.warning("pid %d did not exit within %.0fs; sending SIGKILL", pid, _STOP_TIMEOUT)
        os.kill(pid, signal.SIGKILL)
    pid_file(name).unlink(missing_ok=True)
    return pid


def tail_log(lines: int, name: str = "mattermost") -> str:
    path = log_file(name)
    if not path.is_file():
        return f"no log at {path}"
    return "\n".join(path.read_text().splitlines()[-lines:])
