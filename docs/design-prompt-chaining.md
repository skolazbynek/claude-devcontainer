# One chainable prompt interface for every command

> Requested by `docs/next-steps-todo.md` § "Chaining prompts". This document is
> the design; the implementation lands with it.

## Goal

Every command that takes a persona or a task file takes instead an **ordered list
of prompt refs** — personas and task files are the same kind of thing — plus one
inline task description:

```bash
cld run @personas/architect @personas/agent ./tasks/task_description.md \
    -p "When finished, reply to the master"
```

Refs are appended in the order given. A ref is either `@<path-under-prompts>` or a
filesystem path. `-p` is the inline description and comes last.

## What exists today

Three shapes for the same idea:

| command | persona | task | inline |
|---|---|---|---|
| `cld` (bare), `cld run` | none in the CLI (only `launch_run(system_prompt_file=…)`, used by chain) | `[task_file]` positional, path or `@ref` | `-p` |
| `cld task-agent start` | `<persona>` positional, bare name only (`persona_resolve`) | `[task_file]` positional | `-p` |
| chain step (YAML) | `persona: <name>` | — | `prompt: <text>` |

and three composition sites, each with its own glue text:

- `entrypoint-claude-devcontainer.sh`: `/config/task.md` + `"## Additional Instructions"` + `$AGENT_INLINE_PROMPT`
- `entrypoint-claude-run.sh`: `$AGENT_SYSTEM_PROMPT_FILE` (defaulting to the baked `run-system-prompt.md`) + VCS note + `"TASK INSTRUCTIONS:"` + task
- `agent_loop.compose_kickoff`: lifecycle preamble + `/config/persona.md` + `"# Your task"` + task text

Note what the run image already does: the "system prompt" file is concatenated into
the single `claude -p` **user** prompt. No command uses a real system-prompt channel,
so unifying refs into one prompt loses nothing.

## Design

### 1. One ref grammar, one resolver

`cld/prompts.py` exposes a single entry point:

```python
def resolve_prompt_ref(ref: str, repo_root: Path, cld_root: Path) -> tuple[Path, str]
```

returning `(path, kind)` with `kind` ∈ `{"persona", "task"}` derived from location
(under `prompts/personas/` → persona). *This is the signature two currently-failing
tests in `tests/test_prompts.py` already expect.* `kind` is display metadata only —
it decides the roster's `Persona` column, never behaviour.

Ref forms:

- **`@<path-under-prompts>`** — `@personas/architect`, `@personas/architect.md`,
  `@fix-conflicts`. Resolution order: (1) exact relative path under
  `<repo_root>/prompts/`, then `<cld_root>/prompts/`, appending `.md` when the ref
  has no extension; (2) failing that, today's basename search
  (`find_prompt_matches`), with the existing ambiguity error listing every
  candidate. The resolved path must stay inside the prompts root
  (`resolve().is_relative_to(root.resolve())`), so `@../../../etc/passwd` is
  refused.
- **a filesystem path** — `./tasks/x.md`, `/abs/x.md`; must exist. Host-side only
  (see §4).

`persona_resolve` is **deleted**. Its bare-name rule existed to stop a container
from making the host mount an arbitrary file; the containment check above is
stronger and applies to every ref, and §4 keeps paths off the broker wire entirely.

A cap of **8 refs** per command guards against `cld run prompts/*` glob accidents
(`MAX_PROMPT_REFS` in `prompts.py`).

### 2. One composition, host-side

```python
def compose_brief(refs: Sequence[Path], inline: str) -> str
```

- each ref body has its frontmatter stripped (`strip_frontmatter`), because
  frontmatter is discovery metadata, never prompt content;
- blocks are joined with a blank line, **in argument order**, with no invented
  headers — with N ordered blocks the order *is* the semantics, so
  `"## Additional Instructions"` and `"TASK INSTRUCTIONS:"` go away;
- the inline `-p` text is appended last, **verbatim**.

Host-side composition is forced, not chosen: two fixed container slots
(`/config/persona.md`, `/config/task.md`) cannot express *ordered N* blocks — e.g. a
persona that must come *after* a task file. Once composition moves host-side, one
composed artifact is the only thing left to ship.

Placeholder substitution (`${CONTAINER_NAME}`, `${DELIVERABLE_BRANCH}`, …) stays
where it is: applied by the supervisor to the **mode-owned preamble layer** only
(`prompts/personas/task-agent.md`, `agent.md` — the only files that use it). The
brief stays verbatim, so a `$` in a task description survives, exactly as today.

### 3. One channel into the container

The brief travels in the **existing anchor scratch envelope** (`AGENT_SCRATCH` →
`.cld-run/brief.md`, committed as part of anchor B), which
`docs/design-anchor-change.md` already describes as the carrier for "task
descriptions, personas, patches".

Retired: the `/config/persona.md` and `/config/task.md` mounts,
`AGENT_INLINE_PROMPT`, `AGENT_SYSTEM_PROMPT_FILE`, `AGENT_PERSONA_FILE`,
`INSTRUCTION_FILE`. Kept: `AGENT_PERSONA` (the first persona-kind ref's stem, for
the roster) and the image-owned frames (`run-system-prompt.md`, the lifecycle
preamble) — those are the image's layer, not the user's.

Why the envelope and not a composed temp file mounted at `/config/brief.md`:

- no host temp file to own — `cld run` is detached, so a mounted temp file must
  outlive the launch, i.e. leak;
- no `to_host_path` translation of prompt paths, which is what makes the in-master
  path awkward today;
- the brief lands *in* the anchor commit: auditable, and the agent can re-read it at
  `/workspace/current/.cld-run/brief.md` mid-task;
- one less bind mount on every launch.

Consequence for `entrypoint-claude-run.sh`: its "no task file and no inline prompt"
check currently runs *before* the workspace exists. It moves to after staging (the
brief is only readable then), and the host guarantees a non-empty brief anyway.

### 4. Container → host (the broker hop)

`task-agent start` inside master rebuilds argv for the broker. Generalizing today's
rule to N refs:

- **`@refs` are forwarded verbatim**, so the *host* resolves them against the target
  repo's prompts trees — the point of the `@` form.
- **filesystem paths are read container-side** and folded, in order, into the inline
  text. A path that resolves in master (its own workspace) resolves to nothing on
  the host, and reading its own file is exactly what the container is entitled to
  do. This preserves today's behaviour, generalized.
- Therefore the broker's argv policy tightens from *"start's first positional must
  be a bare persona name"* to **"every positional must start with `@`"**: after
  folding, nothing else is legitimate, and `@refs` are containment-checked by the
  resolver. Persona-name policing in `action_task_agent` disappears.

### 5. Resulting surfaces

```bash
cld [-p TEXT]                                  # ephemeral devcontainer
cld master [-p TEXT]
cld run [refs…] [-p TEXT]
cld task-agent start [refs…] -n <slug> [-p TEXT] [--branch …] [-m …] [-r …] [--peer …]
cld chain run <file> [refs…] [-p TEXT]
```

- At least one of (refs, `-p`) is required — today's rule, unchanged.
- Bare `cld` and `cld master` take `-p` only. Both are Typer *group callbacks*, and
  click resolves a group's first positional as a subcommand name, so a ref there can
  only ever be `No such command '@personas/architect'`. Refs stay on the real
  commands; nothing else routes positionals past a group without a custom group class.
- The persona stops being a **required** positional for `task-agent start`: the
  lifecycle preamble is always present, so a role-less task-agent is coherent. The
  roster shows the first persona-kind ref, or `-`.
- Chain steps: `persona: <name>` + `prompt: <text>` becomes
  `prompts: [<ref>, …]` + `prompt: <text>`. The six bundled `chains/*.yaml` are
  migrated (`persona: architect` → `prompts: ["@personas/architect"]`); no second
  spelling is kept.
- `cld prompts` prints refs in the `@personas/architect` form, so its output is
  copy-pasteable into the interface it documents.

## Tests

- `test_prompts.py`: the two `(path, kind)` tests pass; new coverage for relative
  refs, `.md` appending, containment refusal, ambiguity, kind classification,
  `compose_brief` ordering / frontmatter stripping / verbatim inline / ref cap.
- `test_cli.py`: variadic positionals for bare `cld`, `run`, `task-agent start`;
  the "at least one of refs/-p" refusal; the cap.
- `test_run.py` / `test_docker.py`: the brief rides in the scratch envelope; no
  `/config/*` prompt mounts; `AGENT_PERSONA` still set.
- `test_agent_loop.py`: `compose_kickoff` reads the brief from the workspace,
  preamble substitution unchanged, brief verbatim.
- `test_chain.py`: `prompts:` list validation and the migrated bundled chains.

## Out of scope

- A real system-prompt channel (`--append-system-prompt`). Nothing uses one today;
  if one is wanted it is a separate option, not a positional.
- Interpolating values into user briefs (see §2).
- `cld agent`'s kickoff persona (`agent_kickoff_persona` config): a standing repo
  agent takes no task, so it has no brief.
