# GraphQL Testing MCP

MCP server for starting, managing, and querying a project's GraphQL server from Claude Code.

Lifecycle and queries run on the **host**, through the cld broker's `graphql`
action (see `docs/design-cld-broker.md` §16 and
`docs/impl-graphql-broker-plan.md`), not as a subprocess inside this
container. That means: the server runs from the real repo checkout with the
real `.env` secrets, on the exact jj revision the calling container is on --
and this container never holds a database password or an API token. The
tradeoff is speed: every start/restart is a fresh container + `poetry
install`, there is no fast-restart path.

## Setup

```bash
claude mcp add -s user graphql-tester -- /path/to/cld/scripts/mcp/run-graphql.sh
```

Requires the cld venv (`poetry install`) and a working broker connection (see
`docs/design-cld-broker.md`). The tool only appears in a container when the
**host's** `~/.claude.json` already has a `graphql-tester` entry
(`imgs/claude-devcontainer/container-init.sh:129`).

Per-repo, add to `.cld/config.toml`:

```toml
graphql_command = "poetry run python manage.py runserver 0.0.0.0:$GQL_PORT"
graphql_port = "8000"            # optional, default 8000
graphql_health_path = "/graphql" # optional, default /graphql
```

`graphql_command` **must** bind `0.0.0.0`, not `127.0.0.1` -- see
`graphqlserver/README.md`'s "one thing to get right" section for why.

`graphql_command`, `graphql_port`, and `graphql_health_path` are read from the
repo's **host checkout** (`.cld/config.toml` as it stands on disk when a
lifecycle op runs), not from the session's revision -- so editing config takes
effect on the next `start`/`restart` regardless of what the session's `@` is
on, unlike the server's own code, which is materialized fresh at that revision
every time (see "Requires the cld venv" above and ruling B).

Editing `cld/mcp/graphql.py` needs `cld build` + a container restart: `cld/`
is `COPY`'d into the image, not bind-mounted.

## Tools

### Lifecycle

| Tool | Purpose |
|---|---|
| `start_server` | Start this session's server at its current revision. Idempotent -- returns existing status if already running. Requires `graphql_command`. |
| `stop_server` | Stop it. |
| `restart_server` | Stop + start -- use after editing code or config the server serves. |
| `server_status` | `not_started` / `starting` / `running` / `exited`, with port/endpoint/revision/container when running. |
| `get_server_logs` | Tail server logs. `filter_pattern` is a client-side regex over the returned lines. |
| `list_endpoints` | List configured aliases (see "Targets" below). |

There is no `set_env` tool. The server's environment comes entirely from the
repo's own `.env`, sourced host-side by the broker -- a container-supplied
env override would be a way to smuggle a value into a process holding real
credentials, so that path doesn't exist.

### Client

| Tool | Purpose |
|---|---|
| `introspect` | Run introspection, cache the schema. `target` defaults to `"local"`. |
| `query` | Execute a query/mutation. Supports `variables`. `target` defaults to `"local"`. |
| `describe_type` | Full details for one type from the cached schema (local, no broker round-trip). |

### Resource

- `graphql://schema` -- the cached schema from the last `introspect` call.

## Targets

`query` and `introspect` take a `target`, resolved host-side by the broker:

- **`"local"`** (the default) -- this session's own server, started with
  `start_server`.
- **an alias** (e.g. `"dev"`, `"staging"`) -- looked up in the repo's `.env`
  as `CLD_GRAPHQL_URL_<ALIAS>`, `CLD_GRAPHQL_AUTH_<ALIAS>` (a full
  `Authorization` header value) and/or `CLD_GRAPHQL_COOKIE_<ALIAS>`. The
  broker attaches whichever of those are set; this container never sees
  them. `list_endpoints` lists the aliases a repo has configured.
- **a raw `http(s)://` URL** -- only reachable if its hostname is in the
  broker operator's `GRAPHQL_URL_ALLOWLIST` (`broker/broker.conf.sample`); no
  credentials are ever attached to a raw URL. Denied by default (empty
  allowlist).

## Typical workflow

1. Add `graphql_command` (and, if needed, `graphql_port`/`graphql_health_path`) to the repo's `.cld/config.toml`.
2. `start_server` -- first call pays for `poetry install`; a readiness probe (`{__typename}`) waits for it to come up.
3. `introspect` to fetch and cache the schema.
4. `query` to execute queries/mutations against `"local"` or a configured alias.
5. `get_server_logs` to debug issues.
6. `restart_server` after editing code the server serves; `stop_server` when done.
