"""MCP server for GraphQL API testing -- server lifecycle + client queries."""

import json
import os
import re
import signal
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Thread
from typing import AsyncIterator
from urllib.error import URLError
from urllib.request import Request, urlopen

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError


@dataclass
class ServerState:
    proc: subprocess.Popen | None = None
    port: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    command: str = ""
    workdir: str = ""
    log_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    cached_schema: dict | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def endpoint(self) -> str | None:
        if self.running:
            return f"http://localhost:{self.port}/graphql"
        return None

    def kill(self) -> None:
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.port = None


def _log_reader(state: ServerState) -> None:
    assert state.proc and state.proc.stdout
    for line in state.proc.stdout:
        state.log_buffer.append(line.rstrip("\n"))


def _health_check(port: int, path: str = "/graphql", timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://localhost:{port}{path}"
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(url, method="GET"), timeout=2):
                return True
        except (URLError, OSError, TimeoutError):
            time.sleep(0.3)
    return False


def _resolve_endpoint(state: ServerState, endpoint: str) -> str | None:
    return endpoint if endpoint else state.endpoint


def _gql_request(endpoint: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_state(ctx: Context) -> ServerState:
    return ctx.request_context.lifespan_context


async def _start_server(ctx: Context, state: ServerState, command: str, port: int, workdir: str, env: dict[str, str] | None, health_path: str, health_timeout: float) -> dict:
    if state.running:
        raise ToolError(f"Server already running on port {state.port} (PID {state.proc.pid})")

    state.command = command
    state.workdir = workdir

    merged_env = {**os.environ, **state.env}
    if env:
        merged_env.update(env)

    cmd = command
    if "--port" not in cmd and "-p" not in cmd:
        cmd = f"{cmd} --port {port}"

    state.log_buffer.clear()

    state.proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=workdir,
        env=merged_env,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
    )
    state.port = port

    Thread(target=_log_reader, args=(state,), daemon=True).start()

    await ctx.info(f"Waiting for server on port {port}...")

    if _health_check(port, health_path, health_timeout):
        await ctx.info(f"Server healthy (PID {state.proc.pid})")
        return {
            "status": "running",
            "pid": state.proc.pid,
            "port": port,
            "endpoint": f"http://localhost:{port}{health_path}",
        }

    returncode = state.proc.poll()
    logs = list(state.log_buffer)[-20:]
    state.kill()
    raise ToolError(f"Server did not become healthy within {health_timeout}s (exit_code={returncode})\n" + "\n".join(logs[-20:]))


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[ServerState]:
    state = ServerState()
    try:
        yield state
    finally:
        state.kill()


mcp = FastMCP("graphql-tester", lifespan=app_lifespan)


# --- Lifecycle tools ---


@mcp.tool()
async def start_server(
    ctx: Context,
    command: str = "strawberry server",
    port: int = 8000,
    workdir: str = ".",
    env: dict[str, str] | None = None,
    health_path: str = "/graphql",
    health_timeout: float = 15.0,
) -> dict:
    """Start a local GraphQL server as a subprocess.

    command: shell command to start the server (default: strawberry server).
    port: port to listen on. Appended as --port if not already in command.
    workdir: working directory for the server process.
    env: extra environment variables (merged with current env and set_env overrides).
    health_path: path to poll for health check (default: /graphql).
    health_timeout: seconds to wait for the server to become healthy.
    """
    return await _start_server(ctx, _get_state(ctx), command, port, workdir, env, health_path, health_timeout)


@mcp.tool()
def stop_server(ctx: Context) -> dict:
    """Stop the running local GraphQL server."""
    state = _get_state(ctx)
    if not state.running:
        return {"status": "not_running"}
    pid = state.proc.pid
    state.kill()
    return {"status": "stopped", "pid": pid}


@mcp.tool()
async def restart_server(ctx: Context) -> dict:
    """Restart the local server with current env/config. Uses the same command and workdir from the last start."""
    state = _get_state(ctx)
    if not state.command:
        raise ToolError("No previous server configuration. Use start_server first.")
    port = state.port or 8000
    workdir = state.workdir or "."
    state.kill()
    time.sleep(0.5)
    return await _start_server(ctx, state, state.command, port, workdir, None, "/graphql", 15.0)


@mcp.tool()
def server_status(ctx: Context) -> dict:
    """Check if the local GraphQL server is running."""
    state = _get_state(ctx)
    if not state.proc:
        return {"status": "not_started"}
    if not state.running:
        return {"status": "exited", "exit_code": state.proc.returncode}
    return {
        "status": "running",
        "pid": state.proc.pid,
        "port": state.port,
        "endpoint": state.endpoint,
    }


@mcp.tool()
def set_env(ctx: Context, key: str, value: str) -> dict:
    """Set an environment variable for the server. Takes effect on next start/restart."""
    state = _get_state(ctx)
    state.env[key] = value
    return {"env": dict(state.env)}


@mcp.tool()
def get_server_logs(ctx: Context, tail: int = 50, filter_pattern: str = "") -> list[str]:
    """Get recent server log lines.

    tail: number of lines to return (default 50).
    filter_pattern: optional regex to filter lines.
    """
    state = _get_state(ctx)
    lines = list(state.log_buffer)
    if filter_pattern:
        try:
            pat = re.compile(filter_pattern, re.IGNORECASE)
            lines = [l for l in lines if pat.search(l)]
        except re.error as e:
            return [f"Invalid regex: {e}"]
    return lines[-tail:]


# --- Client tools ---


_INTROSPECTION_QUERY = """
{
  __schema {
    queryType { name }
    mutationType { name }
    types {
      name
      kind
      description
      fields {
        name
        description
        type { name kind ofType { name kind ofType { name kind } } }
        args { name type { name kind ofType { name kind } } }
      }
      inputFields {
        name
        type { name kind ofType { name kind ofType { name kind } } }
      }
      enumValues { name description }
    }
  }
}
"""


@mcp.resource("graphql://schema")
def schema_resource(ctx: Context) -> str:
    """Cached GraphQL schema from last introspection."""
    state = _get_state(ctx)
    if not state.cached_schema:
        return "No schema cached. Call the introspect tool first."
    return json.dumps(state.cached_schema, indent=2)


@mcp.tool()
async def introspect(ctx: Context, endpoint: str = "") -> dict:
    """Run a GraphQL introspection query and return the schema.

    endpoint: GraphQL endpoint URL. Defaults to the local running server.
              Can point to any external instance (e.g. dev, staging).
    """
    state = _get_state(ctx)
    resolved = _resolve_endpoint(state, endpoint)
    if not resolved:
        raise ToolError("No endpoint specified and no local server running")

    try:
        result = _gql_request(resolved, _INTROSPECTION_QUERY)
    except Exception as e:
        raise ToolError(f"Introspection failed ({resolved}): {e}")

    state.cached_schema = result
    await ctx.info("Schema cached and available as graphql://schema resource")
    return result


@mcp.tool()
def query(ctx: Context, query: str, variables: dict | None = None, endpoint: str = "") -> dict:
    """Execute a GraphQL query or mutation.

    query: the GraphQL query/mutation string.
    variables: optional variables dict.
    endpoint: GraphQL endpoint URL. Defaults to the local running server.
              Can point to any external instance (e.g. dev, staging).
    """
    resolved = _resolve_endpoint(_get_state(ctx), endpoint)
    if not resolved:
        raise ToolError("No endpoint specified and no local server running")

    try:
        return _gql_request(resolved, query, variables)
    except Exception as e:
        raise ToolError(f"Query failed ({resolved}): {e}")


if __name__ == "__main__":
    mcp.run()
