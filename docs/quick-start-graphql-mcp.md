# Quick start: GraphQL testing MCP

Run a repo's GraphQL server on the host, at the same revision your container
is on, and query it from Claude Code. Tool reference: `docs/graphql-mcp.md`.
Design: `docs/design-cld-broker.md` §16.

Assumes the cld broker is already set up (`broker/README.md`). The server runs
host-side with the repo's real `.env`; the container never holds a credential.

## Once, on the host

```bash
graphqlserver/build.sh
```

In `/etc/cld/broker.conf` (or wherever `CLD_BROKER_CONF` points):

```bash
GRAPHQL_IMAGE=graphqlserver:latest
# PATH must include docker, jj and curl -- the forced command gets a minimal one
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
```

```bash
cld-brokerctl restart
```

Then make sure the **host's** `~/.claude.json` has a `graphql-tester` entry
(`claude mcp add -s user graphql-tester -- /path/to/cld/scripts/mcp/run-graphql.sh`),
and rebuild so containers pick up the MCP:

```bash
cld build
```

## Per repo

In the repo's `.cld/config.toml`:

```toml
graphql_command = "poetry run python manage.py runserver 0.0.0.0:$GQL_PORT"
graphql_port = "8000"            # optional, default 8000
graphql_health_path = "/graphql" # optional, default /graphql
```

## Then, in the container

`start_server` → `introspect` → `query`. Edit code, `restart_server`, query
again. `get_server_logs` when something is wrong.

First `start_server` pays a full `poetry install`, so it is slow; a readiness
probe waits for the server before returning.

## Three things that silently break setup

- **Quote every value in `.cld/config.toml`.** The broker's parser reads
  double-quoted strings only, so `graphql_port = 8000` reads back as unset,
  not as `8000`.
- **`graphql_command` must bind `0.0.0.0`, not `127.0.0.1`** — otherwise the
  port publishes but nothing answers. See `graphqlserver/README.md`.
- **The MCP only appears in a container if the *host's* `~/.claude.json` has
  the `graphql-tester` entry.** Adding it inside a container does nothing
  (`imgs/claude-devcontainer/container-init.sh`).

## Optional: querying a deployed environment

Add to the repo's `.env`:

```
CLD_GRAPHQL_URL_DEV=https://dev.example.internal/graphql
CLD_GRAPHQL_AUTH_DEV=Bearer eyJ...       # optional, full Authorization value
CLD_GRAPHQL_COOKIE_DEV=session=...       # optional, full Cookie value
```

Then pass `target="dev"` to `query`/`introspect`. The broker attaches the
credentials host-side; the container never sees them. `list_endpoints` shows
which aliases a repo has.

A raw `http(s)://` URL works too, but only if its hostname is in
`GRAPHQL_URL_ALLOWLIST` in `broker.conf`, and it never gets credentials. The
allowlist is empty by default, so raw URLs are denied until you add one.

## Note

The `cld` repo has no GraphQL server of its own, so run the first smoke test
against a repo that does. `MANUAL_TESTS.md` §7 has the full smoke sequence.
