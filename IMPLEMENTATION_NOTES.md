# Implementation notes -- GraphQL testing over the broker

Tracking progress against docs/impl-graphql-broker-plan.md. Deleted in the final commit; content folds into the final report.

## Commit 1: broker: resolve the session revision from the workspace tip

Done. Edited `resolve_test_context` in broker/cld-broker.sh: `-r "${session}@"` +
`--ignore-working-copy`. `bash -n` clean. No shellcheck binary available in this
container -- noted, will mention in final report.

## Commit 2: graphqlserver/ image

Done. Mirrored runtests/ exactly: Dockerfile (header comment changed to describe
graphqlserver, curl call-out comment added, ca-certs/ copied verbatim), build.sh
(GRAPHQL_IMAGE var), entrypoint.sh (new, following runtests/entrypoint.sh step by
step per plan §2.1: GQL_WORKSPACE deterministic not random, GQL_COMMAND required
with no guessed default, exec bash -c "$GQL_COMMAND" as final step, PORT/GQL_PORT
exported). README.md new, mirrors runtests/README.md, with the bind-0.0.0.0
warning as its own called-out section per plan.

Deviation: none so far. `bash -n` clean on entrypoint.sh and build.sh.

## Commit 3: broker: graphql action for server lifecycle and credentialed queries

Done. Added to broker/cld-broker.sh: GRAPHQL_IMAGE/GRAPHQL_START_TIMEOUT/
GRAPHQL_QUERY_TIMEOUT/GRAPHQL_OUTPUT_MAX_BYTES defaults; sweep_gql_orphans,
mask_output, cap_output, curl_capture, resolve_target, check_url_allowlisted,
lookup_alias, resolve_graphql_config (soft), resolve_graphql_context (hard,
used only by start), resolve_graphql_bind, print_gql_status_line,
do_graphql_status/start/stop/logs/query/endpoints, _GRAPHQL_INTROSPECTION_QUERY,
action_graphql. Registered graphql_command/graphql_port/graphql_health_path in
cld/config.py's _TOML_KEYS (mirrors the existing pyproject_dir comment). Added
a `graphql-sweep` verb to broker/cld-brokerctl.sh, duplicating
sweep_gql_orphans rather than sourcing cld-broker.sh -- that file's tail
unconditionally dispatches on $SSH_ORIGINAL_COMMAND once sourced.

`bash -n` clean on cld-broker.sh and cld-brokerctl.sh. `python3 -c "import
ast; ast.parse(...)"` clean on cld/config.py. No shellcheck binary available
in this container (checked again).

Deviations from the plan's literal snippets, both caught during design before
anything was written to disk:

1. **resolve_secrets_env_file extracted as a shared helper.** The plan's
   commit-1 snippet and commit-3 snippet both inline the same
   PROJECT_SUBDIR/.env resolution. Extracted it once so resolve_test_context,
   resolve_graphql_context, and lookup_alias (which needs the secrets path
   without wanting either resolver's side effects) share one implementation
   instead of three copies drifting apart.

2. **Split resolve_graphql_context into a soft config reader
   (resolve_graphql_config) and a hard context resolver
   (resolve_graphql_context).** The plan's single resolver requires
   graphql_command and calls jj unconditionally. Using it for query/introspect/
   endpoints target resolution would (a) deny querying a legitimate external
   alias/URL target on a repo with no local server configured, and (b) break
   on git-backed repos, since the jj call is unconditional and exits 3. Only
   `start` (and status, for GQL_PORT/GQL_HEALTH_PATH) needs the hard form now.

3. **do_graphql_stop resolves nothing** beyond $session/$REPO (already
   provided by the dispatcher) -- it does not call resolve_graphql_context or
   resolve_graphql_config at all. An early draft called
   `resolve_graphql_context 2>/dev/null || true` expecting the redirect to
   swallow a missing-graphql_command failure, but resolve_graphql_context
   calls `exit 3` directly, which `|| true` cannot catch (exit terminates the
   script; it is not a function return). That would have made `stop` unable
   to tear down a server whose graphql_command was since removed -- exactly
   the case where teardown matters most. Fixed by computing the container/
   workspace names directly from $session and skipping config resolution
   entirely.

4. **do_graphql_status's `revision` field reads the actually-serving
   revision back off the running container's own REVISION env var** (via
   `docker inspect`), rather than re-resolving the session's current jj tip.
   The server is pinned to whatever revision it was started with; these
   diverge once the caller keeps editing after the server started. Judged a
   correctness improvement over the plan's literal snippet, not a
   functional gap -- flagging it as a deviation since the plan didn't call
   it out explicitly.

Not yet run: `poetry run pytest` (no Python behavior changed by this commit
besides the config.py key registration, which has no test of its own in the
plan's list -- tests for graphql_op/MCP land in commit 4).

## Commit 4: graphql-tester: delegate lifecycle and queries to the broker

Done. Added `graphql_op` to cld/broker.py (mirrors broker_agent_op, but
capture=True by default per plan). Updated the `help=` string in
cld/cli_container.py to name `graphql`; no other cli_container.py change
needed (`broker()` already forwards any action verbatim). Rewrote
cld/mcp/graphql.py per the plan's deletion/keep list: ServerState and the
subprocess-lifecycle internals (_log_reader, _health_check, _gql_request,
_resolve_endpoint, _start_server, the FastMCP lifespan) are gone; `set_env` is
deleted (ruling C); every tool is now a thin call into `graphql_op` plus
response parsing. Kept _INTROSPECTION_QUERY, _format_type_ref,
_summarize_schema, describe_type, the graphql://schema resource, unchanged.
Dropped `ctx: Context` from every tool -- none needs `await ctx.info(...)`
anymore. The schema cache moved from per-request lifespan state to a
module-level `_cached_schema` behind `_get_cached_schema`/`_set_cached_schema`
accessors, so tests can monkeypatch it the way
tests/test_messenger_mcp.py:21-25 patches `_mailbox_root`. `endpoint: str = ""`
became `target: str = "local"` on `query` and `introspect`, with docstrings
spelling out the three target forms (local/alias/raw URL) since that's the
model's only documentation. Added `setup_logging(...)` under `__main__`,
matching cld/mcp/messenger.py.

Added the third URL-userinfo secret pattern (`scheme://user:pass@host`) to
cld/log.py's `mask_secrets()`, cross-referenced in a comment with the
broker's own duplicate `mask_output()` in cld-broker.sh (the broker can't
import `cld`, so the pattern has to live in both places -- this was already
true for the two existing patterns, just extended).

Added `TestGraphqlOp` to tests/test_broker.py (3 cases: argv+capture default,
a query string with spaces/quotes/`;` surviving as one argv element mirroring
`test_argv_never_becomes_a_command`, capture=False). Added new
tests/test_graphql_mcp.py modeled on tests/test_messenger_mcp.py: argv built
per tool, status-line parsing (including a malformed short line and an empty
response), get_server_logs filtering + invalid-regex path, describe_type's
two failure modes, introspect populating the cache, broker-failure
propagation to ToolError, and pure unit tests for `_format_type_ref` /
`_summarize_schema` (previously zero tests in this module).

Test results:
```
poetry run pytest tests/test_broker.py tests/test_graphql_mcp.py -q
49 passed in 0.93s -1.07s (several runs)
```
Full suite for a regression check: `poetry run pytest -q` -> 738 passed, 42
skipped, 5 xfailed.

Verified every new test can fail (break the guarded behavior, watch it go
red, restore), in three passes:
- broker.py: renamed the `graphql` action string to `graphql-x` inside
  `graphql_op` -> `TestGraphqlOp::test_forwards_op_and_args_capturing_by_default`
  failed as expected; restored, re-ran green.
- graphql.py, five simultaneous breaks -> all five failure groups fired:
  swapped port/endpoint fields in `_parse_status_line` (broke
  `TestStatusLineParsing::test_running`); disabled the `filter_pattern`
  branch in `get_server_logs` (broke both `TestGetServerLogs` cases);
  suppressed the "no cached schema" raise in `describe_type` (broke
  `TestDescribeType::test_raises_without_a_cached_schema`); dropped the
  `_set_cached_schema` call in `introspect` (broke
  `TestIntrospect::test_populates_the_cache_and_returns_a_summary`); typo'd
  `NON_NULL` to `NON_NULL_BROKEN` in `_format_type_ref` (broke both NON_NULL
  cases there and the summarize-schema case exercising it). Restored, re-ran
  green (49 passed).
- graphql.py, two more: suppressed the `returncode != 0` raise in `_run`
  (broke `TestBrokerFailurePropagates`); appended a bogus extra arg to the
  `logs` call (broke `TestLifecycleArgv::test_get_server_logs_forwards_tail`).
  Restored, re-ran green.

Not separately broken (would be redundant with the above, same code paths):
the remaining `TestLifecycleArgv` cases (same argv-forwarding mechanism as
the logs case already proven), `TestDescribeType`'s other two cases and
`TestIntrospect::test_non_json_response_raises_tool_error` (same
`_get_cached_schema`/`json.loads` machinery already exercised), and
`TestSummarizeSchema::test_unwraps_the_data_envelope` /
`TestFormatTypeRef::test_none` / `test_named` (trivial branches of functions
already proven breakable above).

Deviations: none beyond what commit 3's notes already listed as design
decisions (this commit only consumes those).
