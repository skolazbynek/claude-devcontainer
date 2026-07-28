# cld — Developer Workflow Brainstorm

Working notes. Multi-command developer journeys (not single-command descriptions).
Status: initial 5 ideas, to iterate. Each is a short seed, not a spec.

## 1. The command center (multi-repo hub)
A dev opens one `cld master` for their "home" repo and registers their other repos as
sibling targets. From that one shell they `cld agent` up a persistent teammate in each
repo, then drive the whole fleet by messenger — "agent-payments: add the idempotency
key", "agent-web: consume the new field" — reading replies as they land. The master is a
control tower; the dev never leaves it, and each repo's agent keeps its own memory.

## 2. Backlog burn-down (parallel fire-and-forget)
A dev has a pile of small, independent chores (10 flaky tests, a lint sweep, 3 dependency
bumps). They fire a `cld run` per item, each detached and each committing to its own
branch off the same anchor. They walk away, come back to a set of ready-to-review
branches, and cherry-pick/squash the good ones — throughput over supervision.

## 3. Heavyweight feature via chain (quality-gated single deliverable)
For one substantial feature, the dev runs `cld chain run rdi` (research → design →
implement → review). They inspect the design output before letting implementation
proceed, and the accumulator branch collects the vetted result. One deliverable, but
pushed through independent personas so it arrives already critiqued.

## 4. Coordinated cross-repo change (distributed change with handoffs)
A change that spans an API repo, a client SDK, and a consumer app. Master launches a
sibling agent in each, then sequences them via messenger: API agent lands the contract
first and reports its branch, master relays that to the SDK and consumer agents to build
against. The tool becomes the coordination layer for a change no single repo owns.

## 5. The standing teammate (long-lived, conversational)
A dev keeps one `cld agent` alive in a repo for weeks. They message it ad hoc — "triage
this stack trace", "why did CI go red", "bump deps and open a branch" — and because it's
one persistent session, it remembers prior context and its snapshotted work survives
`restart`. It's less a command runner and more a resident engineer you ping.

---

## Notes for iteration
- #1 and #4 lean hardest on the newest, least-tested machinery (sibling-launch +
  messenger). Highest risk, highest payoff.
- #2 and #3 are the most self-contained (single-repo, no cross-container messaging).
- Open question: which of these is the primary intended use? Drop/merge as needed.
