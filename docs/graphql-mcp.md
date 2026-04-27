# GraphQL Testing MCP

MCP server for starting, managing, and querying a local GraphQL server from Claude Code.

## Setup

```bash
claude mcp add -s user graphql-tester -- /path/to/cld/scripts/mcp/run-graphql.sh
```

Requires the cld venv (`poetry install`).

## Tools

### Lifecycle

| Tool | Purpose |
|---|---|
| `start_server` | Start a GraphQL server subprocess. Default command: `poetry run python manage.py`, port `5000`. Polls health check until ready. |
| `stop_server` | Stop the running server. |
| `restart_server` | Restart with same command/workdir/env from last start. |
| `server_status` | Check if server is running, get PID/port/endpoint. |
| `set_env` | Set env var for the server process. Takes effect on next start/restart. |
| `get_server_logs` | Tail server logs (last 500 lines buffered). Supports regex filtering. |

### Client

| Tool | Purpose |
|---|---|
| `introspect` | Run introspection query, cache the schema. Works against local server or any external endpoint. |
| `query` | Execute a GraphQL query or mutation. Supports variables. |

All client tools accept an optional `endpoint` parameter. If omitted, they target the local running server (`http://localhost:{port}/graphql`).

### Resource

- `graphql://schema` -- returns the cached schema from the last `introspect` call.

## Typical workflow

1. `start_server` with your project's serve command and workdir
2. `introspect` to fetch and cache the schema
3. `query` to execute queries/mutations against the running server
4. `get_server_logs` to debug issues
5. `stop_server` when done
