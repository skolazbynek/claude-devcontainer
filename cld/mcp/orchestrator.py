"""MCP server for orchestrating Claude Docker agents."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from cld.docker import find_repo_root
from cld.vcs import get_backend

mcp = FastMCP("claude-orchestrator")

_HOST_VISIBLE_PREFIXES = ("/workspace/origin", "/workspace/current")
_CLD_ROOT = Path(__file__).resolve().parent.parent.parent


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Execute an arbitrary shell command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _is_host_visible(path: Path) -> bool:
    """Check whether *path* is under a host-visible mount prefix."""
    return any(str(path).startswith(p) for p in _HOST_VISIBLE_PREFIXES)


def _stage_to_host(path: Path) -> Path:
    """Copy a non-host-visible file into the repo root so it can be bind-mounted."""
    repo_root = find_repo_root()
    stage_dir = repo_root / ".agent-tasks"
    stage_dir.mkdir(exist_ok=True)
    staged = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="task-", dir=stage_dir, delete=False,
    )
    staged.write(path.read_text())
    staged.close()
    return Path(staged.name)


def _parse_description(path: Path) -> str:
    """Extract a ``description:`` value from YAML frontmatter in a markdown file."""
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


def _builtin_prompts_dir() -> Path:
    """Return the path to built-in prompt templates shipped with cld."""
    return _CLD_ROOT / "prompts"


def _workspace_prompts_dir() -> Path:
    """Return the path to workspace-local prompt templates (inside the repo)."""
    return find_repo_root() / "prompts"


# --- Agent lifecycle ---


@mcp.tool()
def launch_agent(task_file: str, name: str = "", model: str = "", revision: str = "") -> dict:
    """Launch an autonomous Claude agent in a Docker container.

    task_file: path to a markdown task file (absolute or relative to repo root).
               Use 'inline:<text>' to create an ephemeral task file from text.
    name: optional session name suffix. Auto-generated if omitted.
    model: claude model to use (e.g. 'opus', 'sonnet'). Defaults to sonnet.
    revision: revision to initialize the workspace from. Defaults to current working copy.
    """
    from cld.agent import launch_agent as _launch_agent

    repo_root = find_repo_root()

    # Handle inline task creation
    if task_file.startswith("inline:"):
        content = task_file[len("inline:"):]
        stage_dir = repo_root / ".agent-tasks"
        stage_dir.mkdir(exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="task-", dir=stage_dir, delete=False,
        )
        tmp.write(content)
        tmp.close()
        task_file = tmp.name

    task_path = Path(task_file)
    if not task_path.is_absolute():
        task_path = repo_root / task_path

    if not task_path.is_file():
        return {"error": f"Task file not found: {task_path}"}

    # Stage non-host-visible files so the launcher can mount them
    if not _is_host_visible(task_path):
        task_path = _stage_to_host(task_path)

    from cld.config import Config

    try:
        return _launch_agent(
            Config.from_env(),
            task_file=task_path,
            name=name,
            model=model,
            revision=revision,
        )
    except SystemExit as e:
        return {"error": "Agent launch failed", "exit_code": e.code}


@mcp.tool()
def list_agents() -> list[dict]:
    """List Claude agents -- running containers and completed-but-not-merged branches.

    Since agent containers run with --rm, completed agents disappear from `docker ps`.
    This also enumerates VCS branches matching agent_*/review_*/loop_* and merges them in.
    Each entry is {session_name, status: 'running'|'completed', container_id?, running_for?}.
    """
    result = _run([
        "docker", "ps",
        "--filter", "ancestor=claude-agent:latest",
        "--format", '{{json .}}',
    ])
    agents: dict[str, dict] = {}
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        data = json.loads(line)
        name = data.get("Names", "")
        agents[name] = {
            "session_name": name,
            "status": "running",
            "container_id": data.get("ID", ""),
            "container_status": data.get("Status", ""),
            "running_for": data.get("RunningFor", ""),
        }

    try:
        branches_raw = get_backend().list_branches()
    except RuntimeError:
        branches_raw = ""
    for line in branches_raw.splitlines():
        name = line.strip().split()[0] if line.strip() else ""
        # Strip any trailing ':' or '@' jj decorations
        name = name.rstrip(":").split("@")[0]
        if not name or name in agents:
            continue
        if any(name.startswith(p) for p in ("agent_", "review_", "loop_")):
            agents[name] = {"session_name": name, "status": "completed"}

    return list(agents.values())


@mcp.tool()
def check_status(session_name: str, include_result: bool = False) -> dict:
    """Check status of an agent by session name.

    While running: reports container state.
    After completion: container is gone (--rm), reads summary from VCS branch/bookmark.
    Set include_result=True to also return result.json (can be large).
    """
    vcs = get_backend()
    info: dict = {"session_name": session_name}

    result = _run([
        "docker", "ps", "--filter", f"name=^{session_name}$",
        "--format", "{{.Status}}",
    ])
    if result.stdout.strip():
        info["status"] = "running"
        info["container_status"] = result.stdout.strip()
        return info

    # Container gone -- check VCS for the agent's branch/bookmark
    try:
        commit = vcs.resolve_revision(session_name)
        info["status"] = "completed"
        info["commit"] = commit
    except RuntimeError:
        info["status"] = "unknown"
        info["error"] = f"No running container or VCS branch found for '{session_name}'"
        return info

    output_prefix = f"agent-output-{session_name}"

    summary_raw = vcs.file_show(session_name, f"{output_prefix}/summary.json")
    if summary_raw:
        try:
            info["summary"] = json.loads(summary_raw)
        except json.JSONDecodeError:
            info["summary_raw"] = summary_raw[:2000]
    else:
        info["status"] = "failed"
        info.pop("commit", None)
        info["error"] = "summary.json missing -- agent likely failed before commit"
        failure_raw = vcs.file_show(session_name, f"{output_prefix}/AGENT-FAILURE.md")
        if failure_raw:
            info["failure"] = failure_raw[:5000]

    if include_result:
        result_raw = vcs.file_show(session_name, f"{output_prefix}/result.json")
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
    """Get the tail of an agent's log from its VCS branch/bookmark."""
    vcs = get_backend()
    content = vcs.file_show(session_name, f"agent-output-{session_name}/agent.log")
    if content is None:
        return f"No log found for branch '{session_name}'"
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
        pass

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
    """Save a task prompt to the workspace prompts directory (repo root).

    Returns the saved path, which can be passed directly to launch_agent.
    """
    prompts_dir = _workspace_prompts_dir()
    prompts_dir.mkdir(exist_ok=True)
    if not name.endswith(".md"):
        name = f"{name}.md"
    path = prompts_dir / name
    path.write_text(content)
    return {"saved": str(path)}


# --- VCS operations (backend-agnostic) ---


@mcp.tool()
def vcs_log(revset: str = "", template: str = "") -> str:
    """Show VCS log for a revision expression.

    For jj: revset is a jj revset, template is a jj template string.
    For git: revset is a git revision spec, template is a --format string.
    Defaults to current working copy / HEAD.
    """
    vcs = get_backend()
    default_rev = "@" if vcs.name == "jj" else "HEAD"
    return vcs.log(revset or default_rev, template)


@mcp.tool()
def vcs_branch_list() -> str:
    """List VCS branches (jj bookmarks or git branches)."""
    return get_backend().list_branches()


@mcp.tool()
def vcs_new(revset: str = "", message: str = "") -> str:
    """Create a new VCS change on top of a revision.

    For jj: creates a new empty change. For git: checks out the revision.
    *message* sets the description on the new change (jj) or is ignored (git).
    """
    vcs = get_backend()
    default_rev = "@" if vcs.name == "jj" else "HEAD"
    output = vcs.new_change(revset or default_rev)
    if message and vcs.name == "jj":
        # In jj, describe the newly created change. In git, there's no
        # empty change to describe -- the message will go on the next commit.
        vcs.describe("@", message)
    return output or "OK"


@mcp.tool()
def vcs_commit(message: str) -> str:
    """Commit current changes with a message.

    For jj: commits the working copy. For git: stages all changes then commits.
    """
    vcs = get_backend()
    return vcs.commit(message) or "OK"


@mcp.tool()
def vcs_describe(revset: str = "", message: str = "") -> str:
    """Set description/message on a VCS change.

    Argument order matches the backend's describe(revision, message).
    For jj: updates the change description. For git: rewrites the commit message.
    """
    vcs = get_backend()
    default_rev = "@" if vcs.name == "jj" else "HEAD"
    return vcs.describe(revset or default_rev, message) or "OK"


@mcp.tool()
def vcs_diff(revset: str = "", stat: bool = False) -> str:
    """Show diff for a revision. Use stat=True for summary only.

    Without revset: shows working copy / uncommitted changes.
    With revset: shows changes introduced by that revision.
    """
    vcs = get_backend()
    output = vcs.diff(revset, stat=stat)
    return output[:50000] if output else "(no changes)"


# --- Backward-compatible aliases (jj_ prefixed tools still work) ---


@mcp.tool()
def jj_log(revset: str = "@", template: str = "") -> str:
    """[Compatibility] Run jj log with a revset expression. Delegates to vcs_log."""
    return vcs_log(revset, template)


@mcp.tool()
def jj_bookmark_list() -> str:
    """[Compatibility] List jj bookmarks. Delegates to vcs_branch_list."""
    return vcs_branch_list()


@mcp.tool()
def jj_new(revset: str = "@", message: str = "") -> str:
    """[Compatibility] Create a new jj change. Delegates to vcs_new."""
    return vcs_new(revset, message)


@mcp.tool()
def jj_commit(message: str) -> str:
    """[Compatibility] Commit the current working copy. Delegates to vcs_commit."""
    return vcs_commit(message)


@mcp.tool()
def jj_describe(message: str, revset: str = "@") -> str:
    """[Compatibility] Set description on a change. Delegates to vcs_describe."""
    return vcs_describe(revset, message)


@mcp.tool()
def jj_diff(revset: str = "@", stat: bool = False) -> str:
    """[Compatibility] Show diff for a revision. Delegates to vcs_diff."""
    return vcs_diff(revset, stat)


if __name__ == "__main__":
    mcp.run()
