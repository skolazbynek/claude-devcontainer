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
