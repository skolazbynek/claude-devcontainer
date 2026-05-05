# PRODUCT_REVIEW.md fix campaign — summary

Date: 2026-04-27
Approach: parallel orchestrator agents per file/area; results gathered into one jj change per fix area.

## jj change stack

The fixes are stacked on top of `main` as 13 separate jj changes:

| # | Description | Issues addressed |
|---|---|---|
| 1 | snapshot baseline (chardev files captured)                                                                       | (jj internal -- not a fix) |
| 2 | fix(vcs/git): handle root commits in diff, fix double checkout, repair squash()                                   | 2.15, 2.16, 4.7 |
| 3 | fix(docker,agent): allowlist .config mounts, secrets RNG, parent_image+force in ensure_image, public to_host_path | 1.2, 2.4, 2.12, 2.13, 2.14, 4.1, 4.5, 4.8, 4.9, 2.3 (force) |
| 4 | feat(cli): --version, error wrapper, cld build, review trunk auto-detect, headless passthrough fix                | 1.1, 1.3, 2.2, 2.3, 2.6, 2.7, 3.1 |
| 5 | fix(prompts/review): align on CODE_REVIEW.md, document which prompt is used by which path                         | 1.6, 3.3 |
| 6 | chore(prompts): move graphql-mcp.md to docs/, generalize fix-pytest, fix team-leader prefixes                     | 3.2, 3.8, 3.12 |
| 7 | chore(tests): drop duplicate stub fixtures, document HOST_PROJECT_DIR, rename find_jj_root in comments            | 4.2, 4.6, 4.15 |
| 8 | feat(loop): [e]dit prompt option, configurable agent_timeout                                                      | 3.11, 4.14 |
| 9 | feat(loop): cumulative cost reporting from agent result.json                                                      | 2.11 |
| 10 | feat(agent-entrypoint): externalize system prompt, opt-in LLM commit msg                                          | 2.10, 3.14 |
| 11 | fix(orchestrator): list_agents enumerates branches, check_status flags failures, vcs_describe arg order swapped   | 2.8, 2.9, 3.9, 3.15 |
| 12 | docs: README security/dev/orchestrator-flow, CHANGELOG, CLAUDE dedupe, SUMMARY.md move, team-orchestrator rename, spec status badges | 1.4, 1.5, 3.4, 3.5, 3.6, 3.7, 3.10, 3.13, 4.10, 4.11, 4.13 |
| 13 | refactor: centralize agent/loop temp files under .cld/                                                            | 2.1 |
| 14 | test(cli): invocation tests for headless, review, devcontainer, build, --version                                  | 4.12 |

## Issues NOT fully addressed

- **1.4 firewall** — documented in README's "Security model and known gaps", but no `init-firewall.sh` shipped. Implementing a firewall (NET_ADMIN cap, iptables rules) is L-effort and was scoped out.
- **1.5 docker.sock proxy** — likewise documented as a known gap. Putting docker.sock behind `tecnativa/docker-socket-proxy` was scoped out.
- **2.5 `cld loop --detach`** — listed as `deferred` in `specs/implement-review-loop.md` Status table; not implemented.
- **3.15 AGENT-FAILURE.md** — orchestrator's `check_status` now surfaces it, but the agent entrypoint's `summary.json` writer wasn't updated to capture failure details. A follow-up commit can complete the round-trip.

## Process notes

- **Wave 1**: 11 agents launched in parallel covering each major file/area with no overlap. 6 returned usable diffs; 5 produced empty commits or hit container errors (bubblewrap). Re-launched as Wave 2.
- **Wave 2**: 6 agents (4 retries + 2 new tasks). 3 returned usable diffs (loop, entrypoint, cost). 3 produced empty commits again (orchestrator, docs, tempdir).
- The 3 stuck areas were completed directly in the host workspace using the original task prompts as a checklist. Manual completion was faster than a third agent retry given the agent's track record on these specific tasks.
- Per-area changes were placed in their own jj changes via `jj restore --from <agent-commit>` for individual files, with `jj squash --from --into --paths` used to disentangle changes that landed in the wrong commit during chained edits.
- Each Wave-1 agent commit also contained a number of spurious "added" chardev files at the repo root (WSL device entries snapshotted as 0-byte regulars). These were filtered out by applying changes file-by-file rather than squashing entire commits.
- Unit tests pass: `poetry run pytest -m "not integration and not docker and not e2e"` -> 39 passed, 6 deselected.
