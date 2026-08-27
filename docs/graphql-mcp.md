# GraphQL Testing MCP

MCP server for starting, managing, and querying a project's GraphQL server from Claude Code.

## Quick start

Wiring this onto a repo for the first time (host-side steps, then per-repo, then first run):

1. **Build the server image** (once per host): `graphqlserver/build.sh`.
2. **Broker config** (`/etc/cld/broker.conf` or wherever `CLD_BROKER_CONF`
   points): set `GRAPHQL_IMAGE` (defaults to `graphqlserver:latest`, matches
   step 1) and make sure `PATH` includes `docker`, `jj`, and `curl` -- see
   `broker/broker.conf.sample`.
3. **Restart the broker** so it picks up the config: `cld-brokerctl restart`.
4. **Per-repo config** -- add to the repo's `.cld/config.toml`:
   ```toml
   graphql_command = "poetry run python manage.py runserver 0.0.0.0:$GQL_PORT"
   graphql_port = "8000"            # optional, default 8000
   graphql_health_path = "/graphql" # optional, default /graphql
   ```
5. **Wire the MCP itself** (once per host, if not already present): add a
   `graphql-tester` entry to the **host's** `~/.claude.json`, then `cld build`
   + restart the container -- the tool only appears in a container when the
   host's `~/.claude.json` already has that entry.
6. **First run**, from inside a container with the MCP wired: `start_server`
   -> `introspect` -> `query` against `"local"` -- then, after editing code
   the server serves, `restart_server` and query again.

**Three things that silently break a first-time setup:**

- **Quote every value in `.cld/config.toml`.** `cld_conf_get` parses
  double-quoted strings only -- `graphql_port = 8000` (unquoted) reads back
  as unset, not as `8000`.
- **`graphql_command` must bind `0.0.0.0`, never `127.0.0.1`.** See
  `graphqlserver/README.md`'s "one thing to get right" section for why.
- **The `graphql-tester` MCP only appears in a container if the host's
  `~/.claude.json` already has a `graphql-tester` entry** — a container-side
  `claude mcp add` alone is not enough (`imgs/claude-devcontainer/container-init.sh:129`).

**Optional: credentialed external targets.** To query a deployed environment
instead of (or in addition to) `"local"`, add to the repo's `.env`:
```
CLD_GRAPHQL_URL_DEV=https://dev.example.internal/graphql
CLD_GRAPHQL_AUTH_DEV=Bearer eyJ...       # optional, full Authorization value
CLD_GRAPHQL_COOKIE_DEV=session=...       # optional, full Cookie value
```
then use `target="dev"` in `query`/`introspect`. A raw `http(s)://` URL
instead of an alias needs its hostname in the broker operator's
`GRAPHQL_URL_ALLOWLIST` (`broker/broker.conf.sample`) -- denied by default.

The `cld` tool's own repo has no GraphQL server of its own, so run the first
smoke test against a repo that does.

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
| `server_status` | `not_started` / `starting` / `running` / `exited`, with port/endpoint/revision/container when running, plus `stale` (true if the running server's revision no longer matches the session's tip -- `restart_server` fixes it; null if there's nothing to compare). |
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
