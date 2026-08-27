"""MCP server for GraphQL API testing -- server lifecycle + client queries.

Lifecycle and queries are delegated to the host broker's `graphql` action
(docs/impl-graphql-broker-plan.md): the broker runs the server from the real
repo checkout, at the calling container's revision, with the real repo
secrets -- the server and its credentials never live in this container. This
module is a thin client over `cld.broker.graphql_op`.
"""

import json
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from cld.broker import graphql_op
from cld.config import Config
from cld.log import get_logger, setup_logging

log = get_logger(__name__)

mcp = FastMCP("graphql-tester")

# Last introspected schema, module-level rather than per-request lifespan
# state (there's no subprocess to hold onto anymore) so a test can
# monkeypatch it the way tests/test_messenger_mcp.py patches _mailbox_root.
_cached_schema: dict | None = None


def _get_cached_schema() -> dict | None:
    return _cached_schema


def _set_cached_schema(schema: dict) -> None:
    global _cached_schema
    _cached_schema = schema


def _run_result(op: str, *args: str):
    """Run a `graphql` broker op, returning the raw completed-process result."""
    result = graphql_op(op, *args)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise ToolError(f"graphql {op} failed: {detail}")
    return result


def _run(op: str, *args: str) -> str:
    """Run a `graphql` broker op, returning its captured stdout or raising ToolError."""
    return _run_result(op, *args).stdout


def _parse_json_response(op: str, result) -> dict:
    """Parse a query/introspect result's stdout as JSON, distinguishing a
    truncated body (cap_output's stderr marker) from a genuinely malformed one."""
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        if "output truncated" in (result.stderr or ""):
            raise ToolError(
                f"{op} response was truncated ({(result.stderr or '').strip()}) -- "
                "raise GRAPHQL_OUTPUT_MAX_BYTES in broker.conf"
            )
        raise ToolError(f"{op} response was not valid JSON: {e}")


def _parse_status_line(line: str) -> dict:
    """Parse a `graphql status`-shaped tab-separated line: state, port, endpoint, revision, container, stale."""
    parts = (line or "").rstrip("\n").split("\t")
    parts += [""] * (6 - len(parts))
    status, port, endpoint, revision, container, stale = parts[:6]
    return {
        "status": status or "unknown",
        "port": int(port) if port.isdigit() else None,
        "endpoint": endpoint or None,
        "revision": revision or None,
        "container": container or None,
        # None (not "false") when there's nothing to compare against -- not
        # started, exited, or the tip couldn't be resolved -- so "unknown"
        # stays distinguishable from "fresh".
        "stale": {"true": True, "false": False}.get(stale),
    }


# --- Lifecycle tools ---


@mcp.tool()
def start_server() -> dict:
    """Start the GraphQL server for this session's repo, at its current revision.

    Runs on the host broker from the real repo checkout with the real .env
    secrets. Idempotent -- calling this while a server is already running just
    returns its current status, without restarting it -- if the session has
    kept editing since then, that status's `stale: true` means the running
    server is serving an older revision; call `restart_server` to bring it
    up to date. The repo must set `graphql_command` in its `.cld/config.toml`
    (see docs/graphql-mcp.md); this is a slow path, every start/restart pays
    a fresh `poetry install`.
    """
    return _parse_status_line(_run("start"))


@mcp.tool()
def stop_server() -> dict:
    """Stop this session's GraphQL server, if one is running."""
    return {"status": _run("stop").strip() or "stopped"}


@mcp.tool()
def restart_server() -> dict:
    """Restart this session's GraphQL server -- use this after editing code or config it serves."""
    return _parse_status_line(_run("restart"))


@mcp.tool()
def server_status() -> dict:
    """Check this session's GraphQL server: not_started / starting / running / exited.

    `stale` is true when a running/starting server's revision no longer
    matches this session's current tip (you've kept editing since it
    started) -- call `restart_server` to fix it. False when it matches, null
    when there's nothing running to compare or the tip couldn't be resolved.
    """
    return _parse_status_line(_run("status"))


@mcp.tool()
def get_server_logs(tail: int = 50, filter_pattern: str = "") -> list[str]:
    """Get recent server log lines.

    tail: number of lines to return (default 50).
    filter_pattern: optional regex to filter lines, applied client-side to the
    lines the broker returns.
    """
    lines = _run("logs", str(tail)).splitlines()
    if filter_pattern:
        try:
            pat = re.compile(filter_pattern, re.IGNORECASE)
            lines = [l for l in lines if pat.search(l)]
        except re.error as e:
            return [f"Invalid regex: {e}"]
    return lines[-tail:]


@mcp.tool()
def list_endpoints() -> list[str]:
    """List configured GraphQL aliases (set in the repo's .env as CLD_GRAPHQL_URL_<ALIAS>).

    Use an alias as `target` in `query`/`introspect` to reach it with its
    attached credentials, without this container ever holding them.
    """
    return [line for line in _run("endpoints").splitlines() if line]


# --- Client tools ---


@mcp.resource("graphql://schema")
def schema_resource() -> str:
    """Cached GraphQL schema from last introspection."""
    schema = _get_cached_schema()
    if not schema:
        return "No schema cached. Call the introspect tool first."
    return json.dumps(schema, indent=2)


def _format_type_ref(t: dict | None) -> str:
    if not t:
        return "?"
    if t.get("kind") == "NON_NULL":
        return _format_type_ref(t.get("ofType")) + "!"
    if t.get("kind") == "LIST":
        return "[" + _format_type_ref(t.get("ofType")) + "]"
    return t.get("name") or "?"


def _summarize_schema(raw: dict) -> dict:
    schema = raw.get("data", raw).get("__schema", {})
    query_type = (schema.get("queryType") or {}).get("name")
    mutation_type = (schema.get("mutationType") or {}).get("name")

    summary = {"queries": [], "mutations": []}

    for t in schema.get("types", []):
        if t["name"].startswith("__"):
            continue
        fields = t.get("fields") or []
        if t["name"] == query_type and fields:
            summary["queries"] = [
                f"{f['name']}({', '.join(a['name'] + ': ' + _format_type_ref(a['type']) for a in f.get('args', []))}): {_format_type_ref(f['type'])}"
                for f in fields
            ]
        elif t["name"] == mutation_type and fields:
            summary["mutations"] = [
                f"{f['name']}({', '.join(a['name'] + ': ' + _format_type_ref(a['type']) for a in f.get('args', []))}): {_format_type_ref(f['type'])}"
                for f in fields
            ]

    return summary


@mcp.tool()
def introspect(target: str = "local") -> dict:
    """Fetch the GraphQL schema and return a compact summary (type names, field signatures).

    Full schema is cached -- use describe_type to get details on a specific type.
    target: "local" (this session's own server, the default), an alias
    configured in the repo's .env (CLD_GRAPHQL_URL_<ALIAS>, credentialed by
    the broker), or a raw http(s):// URL (only reachable if allowlisted in
    broker.conf; no credentials are attached to a raw URL).
    """
    result = _parse_json_response("introspection", _run_result("introspect", target))
    _set_cached_schema(result)
    return _summarize_schema(result)


@mcp.tool()
def describe_type(type_name: str) -> dict:
    """Return full details for a specific type from the cached schema.

    type_name: exact name of the type (e.g. "User", "CreateUserInput").
    Requires a prior introspect call.
    """
    schema = _get_cached_schema()
    if not schema:
        raise ToolError("No cached schema. Call introspect first.")
    root = schema.get("data", schema).get("__schema", {})
    for t in root.get("types", []):
        if t["name"] == type_name:
            return t
    raise ToolError(f"Type '{type_name}' not found in cached schema")


@mcp.tool()
def query(query: str, variables: dict | None = None, target: str = "local") -> dict:
    """Execute a GraphQL query or mutation.

    query: the GraphQL query/mutation string.
    variables: optional variables dict.
    target: "local" (this session's own server, the default), an alias
    configured in the repo's .env (CLD_GRAPHQL_URL_<ALIAS>, credentialed by
    the broker), or a raw http(s):// URL (only reachable if allowlisted in
    broker.conf; no credentials are attached to a raw URL).
    """
    result = _run_result("query", target, query, json.dumps(variables or {}))
    return _parse_json_response("query", result)


if __name__ == "__main__":
    setup_logging(Config.from_env(), force_stderr=True)
    log.info("graphql-tester MCP server starting")
    mcp.run()
