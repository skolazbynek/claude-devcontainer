# `cld` -- Pre-release Product Review

Findings from a product/UX audit of the CLI, container images, MCP servers,
prompts, docs and tests, plus a sanity check against Anthropic's reference
devcontainer and the broader Claude-Code-orchestrator ecosystem.

Each finding has:

- **Severity** -- Critical / High / Medium / Low.
- **Effort** -- S (< 1h), M (a few hours), L (a day+).
- **Where** -- file:line or area.

Findings inside each section are sorted by ascending effort, so quick wins are
on top and bigger refactors at the bottom. Read top-to-bottom for a release
punch-list.

---

## 1. Critical -- release blockers

### 1.1 `cld headless` is broken (cannot pass any args) -- S

`cli.py:170-173`. The Typer command takes a `Context` but is missing
`context_settings={"allow_extra_args": True, "ignore_unknown_options": True}`,
so `ctx.args` is always empty and any flag triggers "No such option".

Repro:

```
$ cld headless -p "test"
No such option: -p
$ cld headless test
Got unexpected extra argument (test)
```

The README/CLAUDE.md advertise it as a passthrough to `claude -p` -- it
literally cannot. There is also no test for this command (`tests/test_cli.py`
covers `agent` and `loop` only), which is why it shipped broken.

**Fix:** add `context_settings={"allow_extra_args": True, "ignore_unknown_options": True}` and a regression test.

### 1.2 No automatic build of the parent image -- S

`cld/agent.py:78` calls `ensure_image(AGENT_IMAGE, ...)` only. The agent image
is `FROM claude-devcontainer:latest`. If the user follows the README out of
order or only builds one image, the `docker build` for the agent fails several
seconds in with an obscure pull error.

`cld review` and `cld loop` inherit this footgun via `launch_agent`.

**Fix:** in `ensure_image` (or a new `cld build` subcommand) build the
devcontainer image first if missing, then the agent image. Or: in the agent
Dockerfile pin a registry tag and document the pull path.

### 1.3 Stack traces leak on common error paths -- S

Examples (all reproducible from a clean checkout):

- `cld agent -p hello` outside a VCS repo -- prints a Python traceback through
  `get_backend()` instead of "Error: not in a jj/git repo".
- `cld review feature trunk` against a non-existent branch -- traceback from
  `vcs.fork_point`.
- `cld agent -p hello` when Docker is reachable but the user lacks socket
  permission -- `subprocess.run([...], check=True)` raises
  `CalledProcessError` and prints a stack trace.

The CLI has clean error-handling for missing task files but not for any of the
docker/VCS preconditions. This is the very first thing a new user hits.

**Fix:** wrap launch entry-points in a top-level handler that catches
`SystemExit`/`CalledProcessError`/`RuntimeError` from `get_backend`, `require_docker`,
`vcs.*` calls, and prints a one-line `[ERROR] ...` message.

### 1.4 Network is not isolated -- agent can exfiltrate everything mounted in -- M

The README markets containers as security-hardened: `--cap-drop=ALL`,
`--security-opt=no-new-privileges`, CPU/memory limits. None of these prevent
outbound traffic. Anthropic's reference devcontainer for `--dangerously-skip-permissions`
ships an `init-firewall.sh` with default-deny outbound and a small allowlist
(npm, GitHub, claude.ai, etc.). cld has no equivalent.

What an agent can read once it's running:

| Mount | Mode | What it contains |
|---|---|---|
| `~/.claude` | rw | Claude OAuth tokens, session state |
| `~/.claude.json` | ro | All MCP server creds the user has configured |
| `~/.config` (devcontainer) | ro | Discord/Slack/gh/aws CLI creds, anything else there |
| `.gitconfig` | ro | git-credential-helper output, signing keys |
| `MYSQL_CONFIG` | ro | DB credentials |
| `/var/run/docker.sock` | rw | **Host root** (see 1.5) |

Combined with `--dangerously-skip-permissions` and a malicious or hallucinating
agent, all of these are exfiltratable to anywhere on the public internet.

**Fix:** ship an `init-firewall.sh` modeled on Anthropic's reference, run it as
the first step in `entrypoint-claude-agent.sh` and `entrypoint-claude-devcontainer.sh`
(needs `--cap-add=NET_ADMIN` or a sidecar). At minimum, document this gap
prominently in the README's Security section.

### 1.5 `/var/run/docker.sock` mount = host root, not "an exception" -- M

`cld/docker.py:184-191`. The README says: *"Containers run as ... cap-drop ALL
... The docker socket mount is the exception."* That undersells it -- access
to docker.sock is industry-acknowledged equivalent to host root. An agent can
do `docker run -v /:/host --privileged ...` and read or modify anything on the
host. Every other security control becomes decorative.

This is the one item I would not ship publicly without either:

- Putting docker.sock behind a Unix-socket proxy (e.g. `tecnativa/docker-socket-proxy`)
  that whitelists the orchestrator's needed verbs (`POST /containers/*`, etc.)
  and rejects `--privileged`, `--volume`, `--cap-add`, etc.; or
- Making the orchestrator opt-in (`--enable-orchestrator`) and clearly flag the
  trust boundary in the README and in the CLI banner.

### 1.6 `cld review`'s "fix" pair is wired to the wrong filename -- S

Two prompt files describe a review-then-fix flow, but the filenames don't
match:

- `imgs/claude-agent-review/review-template.md:24` -- writes to `review-output.md`.
- `imgs/claude-agent-review/fix-mr.md:2` -- reads from `./CODE_REVIEW.md`.
- `prompts/code-review.md:63` -- writes to `CODE_REVIEW.md`.
- `prompts/loop-review.md` -- writes to `${OUTPUT_FILE}` (`CODE_REVIEW_iterN.md`).

So the documented `review` -> `fix-mr` workflow doesn't actually feed one into
the other. Pick one filename (suggest `CODE_REVIEW.md`) and align all four
prompts.

---

## 2. High -- workflow gaps and security

### 2.1 Temp files leak into the user's repo -- S

Several call paths drop files at `repo_root` with `delete=False` and never
clean up:

| File created | Where | Cleaned up? | gitignored? |
|---|---|---|---|
| `.cld-task-*.md` | `agent.py:_build_task_file` | no | yes (`.cld-task-*`) |
| `.cld-loop-task-*.md` | `cli.py:148-157` | no | no |
| `review-diff-<session>.patch` | `agent.py:158` | no | no |
| `review-task-<session>-*.md` | `agent.py:186` | no | no |
| `.cld-loop-impl-*.md`, `.cld-loop-review-*.md`, `.cld-loop-diff-*.patch` | `loop.py` | yes (`_cleanup_temp_files`) | no |

After a few runs the repo accumulates patches/tasks that show up in `jj st` /
`git status` and risk getting committed. The `loop` command bothered to write
a cleanup helper -- the others should reuse it (or use a dedicated subdirectory
`.cld/` that's a single gitignore line).

### 2.2 No `cld --version` -- S

`cli.py:25` instantiates `typer.Typer(add_completion=False)` with no
`callback`/`--version`. Bug-reports against an unknown version are painful.
Add a `--version` callback wired to `cld.__version__` (and add `__version__`
to `cld/__init__.py`).

### 2.3 No `cld build` subcommand -- S

The README expects users to memorize and copy two `docker build` invocations.
A `cld build [--no-cache]` that builds devcontainer then agent (and prints
expected wall-time / image sizes) would smooth onboarding and feed straight
into the auto-build fix in 1.2.

### 2.4 Image build is silent and slow on first run -- S

`ensure_image` (cld/docker.py:73) triggers `docker build` with no warning.
The devcontainer image installs neovim, jj, golang, build-essential, claude
itself -- typically 5+ minutes on first build. New users will think the tool
hung.

**Fix:** before calling `docker build`, print "Image not found, building (~5
min) ..." with a hint to interrupt if not desired.

### 2.5 `cld loop` blocks the terminal for hours -- M

Default is 3 iterations × (impl + review) × `_AGENT_TIMEOUT=1800s` poll
ceiling = up to 3 hours foreground. The product spec
(`specs/implement-review-loop.md` UC3) describes a `--detach` mode and `cld
loop status` / `list` / `stop` -- none implemented. For the product's headline
"hands-off" use case this is the difference between "set and forget" and
"can't close my terminal".

Either implement minimal `--detach` (write loop state to `repo_root/.cld/loop-<name>.json`,
fork, watch) or remove the deferred section from the spec doc to set
expectations.

### 2.6 `cld review` only takes two branch names -- M

Real review workflows want one of:

- `cld review FEATURE` -- diff against trunk auto-detected (`main`, `master`,
  `trunk`).
- `cld review -r REVSET` -- review an arbitrary revision range.
- `cld review` (no args, current change vs parent).

The current required `FEATURE TRUNK` positionals make the simple case verbose.

### 2.7 `--revision` defaults are surprising -- S

`AGENT_REVISION` defaults to `@` (jj) / `HEAD` (git). `@` in jj is the *current
working copy*, which often has uncommitted in-progress work. Most users mean
"the last committed change" (`@-`).

**Fix:** Default to `@-` for jj and `HEAD` for git, document the difference in
README's "Workspace isolation" section.

### 2.8 `list_agents` only shows running containers -- S

`cld/mcp/orchestrator.py:122-140` filters by `ancestor=claude-agent:latest`.
Since agent containers run with `--rm`, they vanish from `docker ps` the
moment they exit. So `list_agents` does NOT enumerate completed-but-not-yet-merged
agents, which is the more useful set. The docstring says "running" once but
nothing else explains the limitation.

**Fix:** also list VCS branches matching `agent_*` / `review_*` / `loop_*`
patterns and merge them with the running list, marking each entry as
`running|completed`.

### 2.9 `check_status` is misleading on commit failures -- S

`cld/mcp/orchestrator.py:163-190`. If the container exited but `summary.json`
is absent (e.g. commit step failed -- entrypoint exits 3), `check_status`
returns `{"status": "completed", "commit": <hash>}` with no error indication.
`status` here means "the branch exists", not "the agent succeeded". Either
rename to `branch_exists` or read `result.json` and surface failures.

### 2.10 Each successful agent run spends extra tokens generating its own commit message -- S

`imgs/claude-agent/entrypoint-claude-agent.sh:151`. After the main task, the
entrypoint shells out to `claude -p` again to get a 72-char description of the
diff. That's an extra API call per run, doubles wall-time on small changes,
and varies the commit message between identical-result runs.

Cheaper alternatives: use the diff-stat (`agent <session>: 3 files, +42/-12`)
or take the first line of the agent's `result.json` last response. Make the
LLM-generated message opt-in via env var (`AGENT_COMMIT_MSG_LLM=1`).

### 2.11 No cost / token reporting -- M

Each `result.json` already carries `cost_usd`/`duration_ms`. Surface them in
`launch_agent`'s exit banner (single number) and in `cld loop`'s exit report
(per-iteration totals). Critical for users running detached or long loops.

### 2.12 `~/.config` mounted in full to the devcontainer -- M

`cld/docker.py:166-170` mounts the entire `~/.config` ro. That includes
`gh/`, `aws/`, `gcloud/`, `discord/`, `Slack/`, anything that uses XDG.
Anthropic's reference devcontainer doesn't do this. Combined with no
firewall, this is a ~credential-locker mount.

**Fix:** allowlist specific subdirs (`anthropic/`, `claude/`, `nvim/`) instead
of the whole tree. Or split into a separate `--mount-config-all` opt-in flag.

### 2.13 `~/.claude` mounted rw -- M

`cld/docker.py:152-156`. Mounting Claude session state rw means an agent (or
a malicious task file) can both read tokens *and* overwrite the user's session
data. Read-only would be safer, with a writable overlay only for things claude
must persist. Document the tradeoff.

### 2.14 Race on default session names -- S

`cld/docker.py:47-49` uses `random.randint(10000, 99999)`. With 5-digit
namespace, ~316 concurrent agents reach 50% collision probability. Not a
problem for solo users; very real for the team-leader prompt that launches
parallel waves.

**Fix:** `secrets.token_hex(3)` or include `int(time.time())`.

### 2.15 git backend's `squash` ignores its arguments -- M

`cld/vcs/git.py:158-168`. The signature is `squash(from_rev, into_rev)`, but
the implementation hardcodes "soft reset HEAD~1, amend". Currently nothing in
production calls it with non-default args, but the abstract base class
promises generic behavior. Either implement it (cherry-pick + reset) or rename
to `squash_into_parent` and remove the unused params from the base class.

### 2.16 git backend's `diff(revision)` breaks on root commits -- S

`cld/vcs/git.py:179-181` runs `git diff <rev>~1..<rev>`, which fails when
`<rev>` is a root commit (no parent). `cld review` against a brand-new repo
or a branch with a single commit will silently produce empty output and the
launcher exits with "Generated diff is empty".

**Fix:** when `<rev>~1` doesn't resolve, diff against `4b825dc` (the empty
tree) instead.

---

## 3. Medium -- documentation, structure, surface

### 3.1 `cld headless` rationale is unclear -- S

If it worked, it would be `claude -p --permission-mode acceptEdits`. That's
already a one-liner -- the only thing the wrapper adds is the permission flag.
Either explain in `--help` and README why a user would prefer `cld headless`
to `claude -p` directly, or remove the command. (See also 1.1.)

### 3.2 `prompts/graphql-mcp.md` isn't a task prompt -- S

It's MCP-server documentation for `cld/mcp/graphql.py`. The orchestrator's
`list_prompts` will include it, and any agent that picks it up by mistake will
treat the docs as a task. Move it to `docs/graphql-mcp.md` (or any name not
ending in `.md` under `prompts/`). Also: it lists `port: 8000` as the default
but `cld/mcp/graphql.py:167` hardcodes `5000` and the tool signature doesn't
even accept a `port` parameter -- so users can't override either.

### 3.3 Two review prompts that look identical at a glance -- M

`imgs/claude-agent-review/review-template.md` (used by `cld review`) and
`prompts/code-review.md` (used by orchestrator MCP) cover the same review
philosophy with slightly different wording, parameters, and output filenames.
And `prompts/loop-review.md` is a third copy with template variables. New
users won't know which one the tool actually invokes for them.

**Fix:** Either consolidate (one canonical review prompt, parameterized) or
clearly document which command uses which.

### 3.4 `specs/` describes features that were dropped -- S

Both `specs/implement-review-loop.md` and `..._-technical.md` describe
`--detach`, `--keep-bookmarks`, `cld loop status/list/stop`, `[e]dit prompt`
in approve mode -- none of which are implemented. The "see Note" banner at
the top mentions VCS-agnostic refactor but not the dropped features. New
contributors reading the specs will assume they exist.

**Fix:** add a "Status" badge per feature, or move specs to `docs/design/`
with an explicit "implemented in commit X / deferred / dropped" matrix.

### 3.5 `SUMMARY.md` shouldn't be at the repo root -- S

It's a one-shot test-suite design report from a development cycle. It looks
like a top-level project summary because of the filename. Either delete it,
move to `docs/test-strategy.md`, or rename to something less prominent.

### 3.6 README doesn't introduce the prompts/ feature or the orchestrator -- S

The orchestrator section lists tools but never explains the typical end-to-end
flow:

1. user starts a devcontainer
2. inside it, runs `claude --agent team-orchestrator`
3. the orchestrator calls `launch_agent` to spawn sibling Docker agents
4. results materialize as VCS branches the host user can `jj squash --from`

Without that flow, the entry points feel like a pile of unrelated commands.
Add a one-screen diagram + walkthrough.

### 3.7 README missing/wrong prerequisites -- S

- Linux-only? `cld/docker.py:140-142` hard-fails if `/etc/ssl/certs` is
  missing, which is the case on macOS Docker Desktop in some setups. README
  doesn't say "Linux host required".
- `cld headless` requires `claude` on the host. Not listed.
- `MYSQL_CONFIG` looks vendor-specific (Seznam? Diskuze in
  `prompts/fix-pytest-files.md` -- internal hostname). Either generalize as
  `--secret-mount path:dest` or remove from README.

### 3.8 `prompts/fix-pytest-files.md` references "Diskuze API" -- S

`prompts/fix-pytest-files.md:21`. Internal-product reference in a public
repo. Likewise `prompts/parse-pytest-files.md` + `scripts/split_failures.py`
hint at a private testing workflow. These prompts are useful templates -- but
either generalize them or move to a `examples/` dir clearly marked as
"vendor-specific samples".

### 3.9 `vcs_describe` MCP tool reverses argument order -- S

`cld/mcp/orchestrator.py:330` `vcs_describe(message, revset="")` while the
backend method is `describe(revision, message)`. Tool authors copying patterns
between code and MCP usage will hit this. Either swap the param order in the
tool to match the backend, or document the swap explicitly.

### 3.10 README's command examples don't always work -- S

`README.md:54` -- `tail -f $(jj root)/agent-output-agent_fix-auth/agent.log`
runs *before* the agent has created the file (race), so users will see "no
such file". Add `until [ -f ... ]; do sleep 1; done && tail -f ...` or just
say "wait a few seconds, then run".

### 3.11 `--approve` mode is missing the `[e]dit prompt` option promised in the spec -- S

`cld/loop.py:259`. Spec promised c/s/v/e; only c/s/v shipped. Either implement
or update the spec.

### 3.12 `team-leader.md` references tools without `mcp__orchestrator__` prefix in the body but with the prefix in setup -- S

`prompts/team-leader.md` mixes `mcp__orchestrator__check_status` (line 91)
with `check_status` (table at the end). Pick one. Currently the bare names
won't work in real Claude Code unless aliases are set up.

### 3.13 No CHANGELOG / no version pinned -- S

`pyproject.toml` says `version = "0.1.0"`. There's no CHANGELOG.md, no
release notes, no SemVer commitments. Public release should set
expectations.

### 3.14 `dangerouslyDisableSandbox` is irrelevant... but the agent system prompt is -- M

`imgs/claude-agent/entrypoint-claude-agent.sh:88-101` injects a system prompt
that is hard-coded into the image. Users have no way to override it without
rebuilding. Move it to `imgs/claude-agent/agent-system-prompt.md` and either
mount it from a configurable path or expose `--system-prompt-file` in `cld
agent`.

### 3.15 The agent's "AGENT-FAILURE.md" feature is half-wired -- S

System prompt asks the agent to write `AGENT-FAILURE.md` on giving up
(`entrypoint-claude-agent.sh:96`), but `summary.json` doesn't capture failure
details, and `check_status`/`get_log` don't surface this file. Either remove
the instruction or make `check_status` return its contents.

---

## 4. Low -- cleanup

### 4.1 Dead code: `run_container` in `cld/docker.py:222` -- S

Defined but never called. CLI uses `os.execvp` directly; agent and orchestrator
use `subprocess.run` directly. Delete.

### 4.2 Duplicate test fixtures -- S

`tests/fixtures/` has both `claude-stub`, `claude-stub-noop`, `claude-stub-review`
(loose executables) and `stub-default/`, `stub-noop/`, `stub-review/`
(directories). Only the directories are referenced by `tests/conftest.py:127-138`.
Delete the loose files.

### 4.3 `test-workspace/` directory in repo root -- S

Scratch material from development (a `hello.py` and an empty subdir).
Listed in `.gitignore` but checked in. Delete from the working tree.

### 4.4 Empty `.test-repos/` directory checked in -- S

Created by E2E test fixtures, gitignored, but the empty dir is in the working
tree. Don't include.

### 4.5 `cld/agent.py:19` imports `_to_host_path` from another module -- S

Underscored = private to `docker.py`. Either rename to `to_host_path` (public)
or move the helper into a `cld/_paths.py` shared module.

### 4.6 Duplicate `find_jj_root` references in code/comments -- S

`cld/docker.py:34` is now `find_repo_root`, but `tests/conftest.py:90` and
the spec docs still call it `find_jj_root` in comments / pseudocode. Rename
in comments for consistency.

### 4.7 `git.py:new_change` calls `git checkout` twice -- S

`cld/vcs/git.py:103`: `_run_git(["checkout", revision]).stdout + _run_git(["checkout", revision]).stderr`.
Should be one call captured into a variable.

### 4.8 `load_dotenv` is a hand-rolled minimal parser -- S

`cld/docker.py:52-63` doesn't handle quoted values, `export ` prefix, or
escapes. Replace with `python-dotenv` (small dep, in pyproject ecosystem) or
note the limitation.

### 4.9 `_to_host_path` uses `~` instead of `CONTAINER_HOME` -- S

`cld/docker.py:103`. The container home is hardcoded to `/home/claude`
(`CONTAINER_HOME`), but the path-translation logic substitutes the *host's*
`~`. On a host where `os.path.expanduser("~")` is shorter or longer than
`/home/claude`, paths get mangled. Use `CONTAINER_HOME` as the in-container
prefix.

### 4.10 `MEMORY.md`-style memory is in the repo root via `CLAUDE.md` -- S

Two CLAUDE.md files: project + user (auto-memory) merge in operation. Not a
bug, but the project CLAUDE.md is partly redundant with README.md and the
orchestrator section is duplicated almost verbatim. Pick one source of truth.

### 4.11 `prompts/team-leader.md` sets `--agent team-orchestrator` in README but the file is `team-leader.md` -- S

`README.md:107` -- `claude --agent team-orchestrator`. The matching prompt
file is `prompts/team-leader.md`. Either rename file or update README.

### 4.12 No tests for `cld review`, `cld devcontainer`, `cld headless` CLI -- S

`tests/test_cli.py` only covers `agent` and `loop`. Add basic invocation
tests.

### 4.13 Test markers documented in pyproject but not in README -- S

`pyproject.toml:20-24` defines `integration`, `docker`, `e2e` markers. README
gives one `poetry install` line for "Setup" but never says how to run tests
locally vs in CI vs in the devcontainer. Add a short "Development" section.

### 4.14 `_AGENT_TIMEOUT = 1800` in `loop.py` is unconfigurable -- S

`cld/loop.py:22`. 30 min may be too short or too long. Surface as
`--agent-timeout` on `cld loop`.

### 4.15 `_HOST_PROJECT_DIR` env trick in conftest is undocumented -- S

`tests/conftest.py:24`. The way E2E tests detect "running inside the
devcontainer" via `HOST_PROJECT_DIR` is clever but never explained. Add a
comment block or a `tests/README.md`.

---

## 5. Suggested release order

If this is a public-release punch list and you want to ship in a few days:

1. **Day 0 (must):** 1.1, 1.2, 1.3, 1.6, 2.1, 2.2, 2.3 -- the broken/painful
   first-run experience.
2. **Day 1 (security):** 1.4, 1.5, 2.12, 2.13. Even if you can't ship a full
   firewall, *document* the trust model and the docker-socket caveat
   prominently.
3. **Day 2 (polish):** 2.6, 2.7, 2.8, 2.9, 2.10, 3.x doc cleanups.
4. **Pre-1.0 backlog:** 2.5 (`--detach`), 2.11 (cost reporting), 2.15-16
   (git-backend correctness), 3.14 (configurable system prompt), all of
   section 4.

For comparison, Anthropic's own reference devcontainer ships
`init-firewall.sh` + a Dockerfile + a `devcontainer.json`. The crowded
ecosystem ([AgentManager](https://github.com/simonstaton/AgentManager),
[praktor](https://github.com/mtzanidakis/praktor),
[sandboxed.sh](https://github.com/Th0rgal/sandboxed.sh), built-in
"Agent Teams") all advertise sandboxed isolation as a primary feature. With
1.4 + 1.5 unaddressed, cld is materially weaker on its headline value
proposition than its alternatives.
