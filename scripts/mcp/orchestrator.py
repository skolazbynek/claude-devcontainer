"""MCP server for orchestrating Claude Docker agents."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("claude-orchestrator")

_HOST_VISIBLE_PREFIXES = ("/workspace/origin", "/workspace/current")


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


def _builtin_prompts_dir() -> Path:
    return _repo_root() / "prompts"


def _workspace_prompts_dir() -> Path:
    return _find_jj_root() / "prompts"


def _scripts_dir() -> Path:
    return _repo_root() / "scripts"


_STRIP_ENV = {"SESSION_NAME", "MYSQL_DEFAULTS_FILE", "INSTRUCTION_FILE", "AGENT_MODEL", "AGENT_REVISION"}


def _clean_env() -> dict[str, str]:
    """Return env dict without vars that would leak the parent container's state."""
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _is_host_visible(path: Path) -> bool:
    return any(str(path).startswith(p) for p in _HOST_VISIBLE_PREFIXES)


def _stage_to_host(path: Path) -> Path:
    """Copy a non-host-visible file to jj_root so it can be mounted into agent containers."""
    jj_root = _find_jj_root()
    stage_dir = jj_root / ".agent-tasks"
    stage_dir.mkdir(exist_ok=True)
    staged = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="task-", dir=stage_dir, delete=False,
    )
    staged.write(path.read_text())
    staged.close()
    return Path(staged.name)


def _parse_description(path: Path) -> str:
    """Extract description from markdown frontmatter (--- delimited)."""
    try:
        text = path.read_text()
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("---", 3)
    if end == -1:
        return ""
    for line in text[3:end].splitlines():
        if line.strip().lower().startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def _jj_file_show(revset: str, filepath: str) -> str | None:
    """Read a file from a jj revision. Returns content or None."""
    jj_root = _find_jj_root()
    result = _run(
        ["jj", "file", "show", "-r", revset, filepath],
        cwd=str(jj_root),
    )
    if result.returncode != 0:
        return None
    return result.stdout


# --- Agent lifecycle ---


@mcp.tool()
def launch_agent(task_file: str, name: str = "", model: str = "", revision: str = "") -> dict:
    """Launch an autonomous Claude agent in a Docker container.

    task_file: path to a markdown task file (absolute or relative to jj root).
               Use 'inline:<text>' to create an ephemeral task file from text.
    name: optional session name suffix. Auto-generated if omitted.
    model: claude model to use (e.g. 'opus', 'sonnet'). Defaults to sonnet.
    revision: jj revset to initialize the workspace from. Defaults to current working copy (@).
    """
    jj_root = _find_jj_root()

    if task_file.startswith("inline:"):
        content = task_file[len("inline:"):]
        stage_dir = jj_root / ".agent-tasks"
        stage_dir.mkdir(exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="task-", dir=stage_dir, delete=False,
        )
        tmp.write(content)
        tmp.close()
        task_file = tmp.name

    task_path = Path(task_file)
    if not task_path.is_absolute():
        task_path = jj_root / task_path

    if not task_path.is_file():
        return {"error": f"Task file not found: {task_path}"}

    # Stage non-host-visible files (e.g. builtin prompts) so the launcher can mount them
    if not _is_host_visible(task_path):
        task_path = _stage_to_host(task_path)

    cmd = [str(_scripts_dir() / "run-claude-agent.sh")]
    if name:
        cmd += ["-n", name]
    if model:
        cmd += ["-m", model]
    if revision:
        cmd += ["-r", revision]
    cmd.append(str(task_path))

    result = _run(cmd, cwd=str(jj_root), env=_clean_env())

    if result.returncode != 0:
        return {"error": result.stderr or result.stdout, "exit_code": result.returncode}

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
def check_status(session_name: str, include_result: bool = False) -> dict:
    """Check status of an agent by session name.

    While running: reports container state.
    After completion: container is gone (--rm), reads summary from jj bookmark.
    Set include_result=True to also return result.json (can be large).
    """
    jj_root = _find_jj_root()
    info: dict = {"session_name": session_name}

    # Check if container is still running
    result = _run([
        "docker", "ps", "--filter", f"name=^{session_name}$",
        "--format", "{{.Status}}",
    ])
    if result.stdout.strip():
        info["status"] = "running"
        info["container_status"] = result.stdout.strip()
        return info

    # Container gone -- check jj bookmark for results
    result = _run(["jj", "log", "-r", session_name, "--no-graph", "-T", "commit_id"], cwd=str(jj_root))
    if result.returncode != 0:
        info["status"] = "unknown"
        info["error"] = f"No running container or jj bookmark found for '{session_name}'"
        return info

    info["status"] = "completed"
    info["commit"] = result.stdout.strip().split("\n")[0]

    output_prefix = f"agent-output-{session_name}"

    summary_raw = _jj_file_show(session_name, f"{output_prefix}/summary.json")
    if summary_raw:
        try:
            info["summary"] = json.loads(summary_raw)
        except json.JSONDecodeError:
            info["summary_raw"] = summary_raw[:2000]

    if include_result:
        result_raw = _jj_file_show(session_name, f"{output_prefix}/result.json")
        if result_raw:
            try:
                info["result"] = json.loads(result_raw)
            except json.JSONDecodeError:
                info["result_raw"] = result_raw[:5000]

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
def get_log(session_name: str, tail: int = 80) -> str:
    """Get the tail of an agent's log from its jj bookmark."""
    content = _jj_file_show(session_name, f"agent-output-{session_name}/agent.log")
    if content is None:
        return f"No log found for bookmark '{session_name}'"
    lines = content.splitlines()
    return "\n".join(lines[-tail:])


# --- Prompts ---


@mcp.tool()
def list_prompts() -> list[dict]:
    """List available task prompt files from both builtin and workspace prompts."""
    prompts: list[dict] = []

    builtin = _builtin_prompts_dir()
    if builtin.is_dir():
        for f in sorted(builtin.glob("*.md")):
            prompts.append({
                "name": f.name, "path": str(f),
                "source": "builtin", "size": f.stat().st_size,
                "description": _parse_description(f),
            })

    try:
        workspace = _workspace_prompts_dir()
        if workspace.is_dir():
            for f in sorted(workspace.glob("*.md")):
                prompts.append({
                    "name": f.name, "path": str(f),
                    "source": "workspace", "size": f.stat().st_size,
                    "description": _parse_description(f),
                })
    except RuntimeError:
        pass  # no jj root

    return prompts


@mcp.tool()
def read_prompt(name: str) -> str:
    """Read a task prompt file by name. Searches workspace first, then builtin."""
    try:
        workspace = _workspace_prompts_dir() / name
        if workspace.is_file():
            return workspace.read_text()
    except RuntimeError:
        pass

    builtin = _builtin_prompts_dir() / name
    if builtin.is_file():
        return builtin.read_text()

    return f"Prompt not found: {name}"


@mcp.tool()
def save_prompt(name: str, content: str) -> dict:
    """Save a task prompt to the workspace prompts directory (jj root).

    Returns the saved path, which can be passed directly to launch_agent.
    """
    prompts_dir = _workspace_prompts_dir()
    prompts_dir.mkdir(exist_ok=True)
    if not name.endswith(".md"):
        name = f"{name}.md"
    path = prompts_dir / name
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
