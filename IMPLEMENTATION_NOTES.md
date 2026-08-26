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
