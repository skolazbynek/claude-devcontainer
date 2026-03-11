"""MCP server for orchestrating Claude Docker agents."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("claude-orchestrator")


def _find_jj_root() -> Path:
    # Inside a container, use the bind-mounted origin (host-visible) dir
    origin = os.environ.get("WORKSPACE_ORIGIN", "")
    if origin and (Path(origin) / ".jj").is_dir():
        return Path(origin)
    d = Path.cwd()
    while d != d.parent:
        if (d / ".jj").is_dir():
            return d
        d = d.parent
    raise RuntimeError("No jj repository found")


def _repo_root() -> Path:
    """Repo root = directory containing the scripts/ dir."""
    return Path(__file__).resolve().parent.parent.parent


def _scripts_dir() -> Path:
    return _repo_root() / "scripts"


_STRIP_ENV = {"SESSION_NAME", "MYSQL_DEFAULTS_FILE", "INSTRUCTION_FILE"}


def _clean_env() -> dict[str, str]:
    """Return env dict without vars that would leak the parent container's state."""
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


# --- Agent lifecycle ---


@mcp.tool()
def launch_agent(task_file: str, name: str = "") -> dict:
    """Launch an autonomous Claude agent in a Docker container.

    task_file: path to a markdown task file (absolute or relative to jj root).
               Use 'inline:<text>' to create an ephemeral task file from text.
    name: optional session name suffix. Auto-generated if omitted.
    """
    jj_root = _find_jj_root()

    if task_file.startswith("inline:"):
        content = task_file[len("inline:"):]
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="task-", dir=jj_root / "prompts",
            delete=False,
        )
        tmp.write(content)
        tmp.close()
        task_file = tmp.name

    task_path = Path(task_file)
    if not task_path.is_absolute():
        task_path = jj_root / task_path

    if not task_path.is_file():
        return {"error": f"Task file not found: {task_path}"}

    cmd = [str(_scripts_dir() / "run-claude-agent.sh")]
    if name:
        cmd += ["-n", name]
    cmd.append(str(task_path))

    result = _run(cmd, cwd=str(jj_root), env=_clean_env())

    if result.returncode != 0:
        return {"error": result.stderr or result.stdout, "exit_code": result.returncode}

    # Parse container ID and session name from script output
    info = {"stdout": result.stdout.strip()}
    for line in result.stdout.splitlines():
        if line.startswith("Container ID:"):
            info["container_id"] = line.split(":", 1)[1].strip()
        if line.startswith("Agent name:"):
            info["session_name"] = line.split(":", 1)[1].strip()
    return info


@mcp.tool()
def list_agents() -> list[dict]:
    """List running Claude agent containers."""
    result = _run([
        "docker", "ps",
        "--filter", "ancestor=claude-agent:latest",
        "--format", '{{json .}}',
    ])
    agents = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        data = json.loads(line)
        agents.append({
            "container_id": data.get("ID", ""),
            "name": data.get("Names", ""),
            "status": data.get("Status", ""),
            "running_for": data.get("RunningFor", ""),
        })
    return agents


@mcp.tool()
def check_status(session_name: str) -> dict:
    """Check status of an agent by session name. Combines container state and summary.json if available."""
    jj_root = _find_jj_root()

    # Container state
    result = _run(["docker", "inspect", "--format", '{{.State.Status}}', session_name])
    container_status = result.stdout.strip() if result.returncode == 0 else "not_found"

    info: dict = {"session_name": session_name, "container_status": container_status}

    # Summary file
    summary_path = jj_root / f"agent-output-{session_name}" / "summary.json"
    if summary_path.is_file():
        try:
            info["summary"] = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            info["summary"] = "invalid json"

    return info


@mcp.tool()
def stop_agent(session_name: str) -> dict:
    """Stop a running agent container."""
    result = _run(["docker", "stop", session_name])
    if result.returncode != 0:
        return {"error": result.stderr.strip(), "exit_code": result.returncode}
    return {"stopped": session_name}


# --- Results ---


@mcp.tool()
def get_results(session_name: str) -> dict:
    """Get agent results: summary.json and result.json content."""
    jj_root = _find_jj_root()
    output_dir = jj_root / f"agent-output-{session_name}"

    if not output_dir.is_dir():
        return {"error": f"Output directory not found: {output_dir}"}

    info: dict = {"session_name": session_name}

    summary_path = output_dir / "summary.json"
    if summary_path.is_file():
        try:
            info["summary"] = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            info["summary_raw"] = summary_path.read_text()[:2000]

    result_path = output_dir / "result.json"
    if result_path.is_file():
        try:
            info["result"] = json.loads(result_path.read_text())
        except json.JSONDecodeError:
            info["result_raw"] = result_path.read_text()[:5000]

    return info


@mcp.tool()
def get_log(session_name: str, tail: int = 80) -> str:
    """Get the tail of an agent's log file."""
    jj_root = _find_jj_root()
    log_path = jj_root / f"agent-output-{session_name}" / "agent.log"

    if not log_path.is_file():
        return f"Log file not found: {log_path}"

    lines = log_path.read_text().splitlines()
    return "\n".join(lines[-tail:])


@mcp.tool()
def get_diff(session_name: str) -> str:
    """Get the jj diff for an agent's bookmark."""
    jj_root = _find_jj_root()
    result = _run(["jj", "diff", "-r", session_name], cwd=str(jj_root))
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout[:50000] if result.stdout else "(no changes)"


# --- Prompts ---


@mcp.tool()
def list_prompts() -> list[dict]:
    """List available task prompt files in the prompts/ directory."""
    prompts_dir = _repo_root() / "prompts"
    if not prompts_dir.is_dir():
        return []
    return [
        {"name": f.name, "path": str(f), "size": f.stat().st_size}
        for f in sorted(prompts_dir.glob("*.md"))
    ]


@mcp.tool()
def read_prompt(name: str) -> str:
    """Read a task prompt file by name (from prompts/ directory)."""
    prompts_dir = _repo_root() / "prompts"
    path = prompts_dir / name
    if not path.is_file():
        return f"Prompt not found: {name}"
    return path.read_text()


@mcp.tool()
def save_prompt(name: str, content: str) -> dict:
    """Save a task prompt to the prompts/ directory."""
    prompts_dir = _repo_root() / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    path = prompts_dir / name
    if not name.endswith(".md"):
        path = prompts_dir / f"{name}.md"
    path.write_text(content)
    return {"saved": str(path)}


# --- Jujutsu ---


@mcp.tool()
def jj_log(revset: str = "@", template: str = "") -> str:
    """Run jj log with a revset expression."""
    jj_root = _find_jj_root()
    cmd = ["jj", "log", "-r", revset]
    if template:
        cmd += ["-T", template]
    result = _run(cmd, cwd=str(jj_root))
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout


@mcp.tool()
def jj_bookmark_list() -> str:
    """List jj bookmarks."""
    jj_root = _find_jj_root()
    result = _run(["jj", "bookmark", "list"], cwd=str(jj_root))
    return result.stdout if result.returncode == 0 else f"Error: {result.stderr.strip()}"


@mcp.tool()
def jj_new(revset: str = "@", message: str = "") -> str:
    """Create a new jj change. Optionally set a description."""
    jj_root = _find_jj_root()
    cmd = ["jj", "new", revset]
    if message:
        cmd += ["-m", message]
    result = _run(cmd, cwd=str(jj_root))
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout or "OK"


@mcp.tool()
def jj_commit(message: str) -> str:
    """Commit the current jj working copy with a message."""
    jj_root = _find_jj_root()
    result = _run(["jj", "commit", "-m", message], cwd=str(jj_root))
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout or "OK"


@mcp.tool()
def jj_describe(message: str, revset: str = "@") -> str:
    """Set description on a jj change."""
    jj_root = _find_jj_root()
    result = _run(["jj", "describe", "-r", revset, "-m", message], cwd=str(jj_root))
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout or "OK"


@mcp.tool()
def jj_diff(revset: str = "@", stat: bool = False) -> str:
    """Show diff for a revision. Use stat=True for summary only."""
    jj_root = _find_jj_root()
    cmd = ["jj", "diff", "-r", revset]
    if stat:
        cmd.append("--stat")
    result = _run(cmd, cwd=str(jj_root))
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout[:50000] if result.stdout else "(no changes)"


if __name__ == "__main__":
    mcp.run()
