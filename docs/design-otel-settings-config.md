# Telemetry config in Claude Code's `settings.json`

> **Status: implemented and reviewed.** Requested as **T6** on top of
> `docs/review-otel-public-release.md`'s to-do list (T1-T5 have landed).
> Built on `otel-t6`; reviewed against this document by running it, see
> § Review of the implementation at the end. Scope is the `otel/` folder
> only, and the folder's standing rule holds: **no dependency on cld**
> beyond defaulting the state directory to `~/.cld/otel`.

## The problem

Pointing a Claude Code session at the collector is five environment
variables. T3 (`otelctl.sh env`) made that appendable to a shell rc and T2
printed it on `start`, but it is still *shell state*: it lives in a dotfile,
it applies to every terminal or none, it does not travel with a project, and
a session launched by anything other than that shell (an IDE, a launcher, a
desktop app) never sees it. The user wants the configuration in a config
file.

## The target, and why there is no alternative

**Claude Code's own settings file `env` block.** This is decided; the
reasoning is recorded here because it is the whole reason an otel-owned
config file is not on the table:

the variables have to arrive in the *`claude` process's* environment. An
otel-owned file -- say `~/.cld/otel/config.env`, sourced by `otelctl.sh` --
would be read by the wrong program. `otelctl.sh` runs the collector and the
aggregator; it never launches `claude` and has no way to inject anything into
it. Such a file would relocate the problem (now the user has a file *and*
still has to get its contents into the session's environment) rather than
solve it. Claude Code's settings file is the only config file in the picture
that the process which needs the variables actually reads.

## Verified facts this design stands on

Everything below was read out of the live docs during this design (fetched
2026-09-01), not recalled. Each fact is followed by what it forces.

| # | Fact | Source | Consequence |
|---|---|---|---|
| F1 | The `env` block accepts the telemetry variables; the monitoring docs' own example is an `env` block of exactly these keys | `monitoring-usage` § Administrator configuration | The fragment we emit is `{"env": {...}}`, nothing more |
| F2 | *"When the same variable is set in both your shell and a settings file `env` block, the settings file value applies. Claude Code writes each `env` entry into the process environment, replacing the value inherited from the shell."* | `env-vars` § In settings files | A real replacement, not a competing layer. A shell export shadowed by a file entry is **dead**, and `doctor` must say so |
| F3 | Precedence, highest first: managed -> `claude --settings` -> `.claude/settings.local.json` -> `.claude/settings.json` -> `~/.claude/settings.json`. An `env` block is an ordinary key and follows those levels; when more than one file sets a variable, the highest-precedence one applies | `settings` § Settings precedence, `settings-reference` § `env` | The resolver merges **per variable**, not per file |
| F4 | Scopes: `~/.claude/settings.json` = you everywhere; `.claude/settings.json` = everyone in the project, committed; `.claude/settings.local.json` = you in this project, gitignored; managed = org-wide | `settings` § Settings files and who they affect | `install` must make the user pick |
| F5 | *"a feature that reads its variables once at startup, such as OpenTelemetry monitoring, keeps its startup values until you relaunch"* | `env-vars` § In settings files | The relaunch caveat (D21) |
| F6 | *"Claude Code doesn't pass `OTEL_*` environment variables to the subprocesses it spawns,"* including the Bash tool, hooks, MCP servers and language servers | `monitoring-usage` | `doctor`'s process-environment view is **blind by design** inside Claude Code (D16) |
| F7 | `aggregate.py:100` -- `_stats_path` is `output_dir / service_name / filename`; sessions separate by session id *inside* that folder | this repo | A project-wide `service.name` in a committed file groups a team under one folder; it does not collapse sessions. Confirmed, no action (D9) |
| F8 | Settings files are **strict JSON**: *"a `//` comment or a trailing comma is a syntax error, and Claude Code reports the file as a Settings Error at the next start"* | `settings` § Edit a settings file | A rewrite loses no comments (there can be none), so a parse-merge-write cycle is safe (D5). A file that does not parse means **none** of its settings apply -> `fail`, not `warn` (D18) |
| F9 | *"To cancel a shell export, set the variable to `""`. Claude Code treats an empty value as unset"* | `settings-reference` § `env` | An empty string is an explicit *unset*, and it can silently switch telemetry off (D19) |
| F10 | Telemetry variables are in the class Claude Code applies *"at startup from every settings file"*. The variables project and local settings **cannot** set are `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_TMPDIR`, OS directory variables, `OTEL_LOG_RAW_API_BODIES`, the `ENABLE_BETA_TRACING_DETAILED`/`BETA_TRACING_ENDPOINT` pair, and the start/sync variables | `settings-reference` § When Claude Code applies `env` values, § Variables Claude Code ignores in `env` | Our five variables are honoured from **every** tier, including a committed project file, and (being in the safe class) at startup rather than after the trust dialog |
| F11 | Managed settings **remove** developer-set OTLP variables at startup: a managed generic `OTEL_EXPORTER_OTLP_ENDPOINT` removes every developer-set per-signal endpoint, likewise protocol and credentials, *"and logs a warning you can see with `claude --debug`"* | `monitoring-usage` § How managed settings lock the OTLP destination | The resolver's "per-signal beats generic" rule inverts under a managed generic value (D20) |
| F12 | Managed settings file paths: macOS `/Library/Application Support/ClaudeCode/managed-settings.json`, Linux and WSL `/etc/claude-code/managed-settings.json`, Windows `C:\Program Files\ClaudeCode\` (the legacy `C:\ProgramData\ClaudeCode\...` is **not** read). There is also an optional `managed-settings.d/` directory, plus MDM, registry and server-managed sources that are not files | `managed-settings` § Choose a delivery mechanism | The two POSIX paths are asserted; the non-file sources are declared invisible (D20) |
| F13 | `CLAUDE_CONFIG_DIR` overrides the config directory (default `~/.claude`); settings, session history and plugins live under it | `env-vars` | The user-tier path is `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json` |
| F14 | `CLAUDECODE=1` is set in subprocesses Claude Code spawns **and** in IDE integrated terminals. `CLAUDE_CODE_CHILD_SESSION=1` is set *only* by Claude Code itself when it launches a Bash/PowerShell/Monitor subprocess, a hook or a status line (v2.1.172+) | `env-vars` | `CLAUDE_CODE_CHILD_SESSION` is the precise "my `OTEL_*` view was deliberately withheld" signal; `CLAUDECODE` is not (D16) |
| F15 | `--settings` takes a file path *or* an inline JSON string, for one session | `cli-reference` | That tier is unreadable by `doctor`, by construction (D12) |
| F16 | `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` defaults to `delta`; `OTEL_METRIC_EXPORT_INTERVAL` defaults to 60000; per-signal endpoint/protocol override the generic ones; Claude Code has **no** default protocol | `monitoring-usage` § Common configuration variables | Check 4's existing verdict table is confirmed unchanged on these points |

Not verified, and therefore not asserted anywhere in this design: whether
Claude Code searches *parent* directories for `.claude/settings.json`. The
docs state only that a permission approval written to
`.claude/settings.local.json` from a subdirectory lands at the repository
root. D12 handles this by reporting rather than guessing.

## Scope and non-goals

- **Additive.** `otelctl.sh env` keeps its exact current output and grammar.
  It is still the right answer for a one-off terminal, for `.envrc`, and for
  a container that has no `~/.claude`.
- **No new dependency.** The `$PYTHON` the script already resolves, and
  stdlib `json`. No `jq`.
- **No new shipped file.** Everything lands in `otelctl.sh`, as with
  `doctor`. (`tests/test_pack_otel.py` derives its manifest from the
  directory listing, so it would tolerate a new file -- the constraint is a
  design principle, not a test.)
- **`doctor` still never repairs.** Reading settings files is a read;
  `settings install` is a separate, explicitly invoked command.
- **Not in scope:** teaching `cld` to write settings files instead of
  exporting env vars, `otelHeadersHelper`, logs/traces signals, and any
  attempt to enumerate running Claude Code sessions.

---

# Part 1 -- emitting and installing the fragment

## D1. A `settings` subcommand, not `env --json`

Rejected: `otelctl.sh env --json`.

`env`'s contract today is *text you can source*: `eval "$(./otelctl.sh
env)"` and `./otelctl.sh env >> ~/.bashrc` are both documented
(`otel/README.md:65-68`, `otel/QUICK-START.md`). A `--json` flag makes one
command emit two mutually incompatible languages under the same name, and
the failure mode of getting it wrong is silent: `eval` of a JSON blob, or a
JSON fragment appended to a `.bashrc`. Worse, the install action *writes a
file*, which does not belong under a command whose whole contract is "prints
to stdout".

Chosen: a sibling subcommand named after the artifact the user is looking
for.

```
otelctl.sh settings [--docker] [--service-name NAME]
otelctl.sh settings install (--user | --project | --local | --file PATH)
                            [--docker] [--service-name NAME] [--force] [--dry-run]
```

- Bare `settings` prints the merge-ready fragment to **stdout** and nothing
  else, so `./otelctl.sh settings > /tmp/frag.json` is exact.
- Advice that a human needs but a pipe must not receive -- "edit
  `service.name`", "relaunch `claude` to apply" -- goes to **stderr**.
- `install` is the only mutating verb in the whole script other than
  `start`/`stop`.

Dispatch mirrors `logs`: `settings) settings_cmd "${@:2}" ;;`, with the
action as `$2`. Unknown flag or missing target exits **2** with the usage
line, matching the existing convention (`otel/otelctl.sh:919-928`).

`env --help` gains one "see also" line, and `env`'s own output is untouched.

## D2. Output shape

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
    "OTEL_RESOURCE_ATTRIBUTES": "service.name=my-session"
  }
}
```

Two-space indent, trailing newline, keys in the order above (the order the
docs' own example uses, and the order `env` prints). All values are strings,
including `"1"` -- F1's example and the `env` key's declared type
(*"object mapping variable names to string values"*) are both strings, and a
JSON `1` is a type error.

`--docker` swaps `localhost` for `host.docker.internal`, exactly as
`env --docker` does. `--service-name NAME` substitutes the placeholder;
without it the placeholder stays `my-session` and stderr says to edit it.

## D3. One source of truth for the five variables

There must not be a second copy of the variable list. Factor the pair
`NAME=VALUE` out of `env_cmd` into a helper -- `telemetry_vars <host>`,
emitting five `NAME=VALUE` lines -- and let both renderers consume it:

- `env_cmd` prefixes `export ` and keeps its `# edit this...` comment line,
  so its output is byte-identical to today's.
- the JSON emitter (in `$PYTHON`) reads those lines and quotes them.

`print_env_block` (T2) already documents this rule in a comment
(`otel/otelctl.sh:143-145`); this extends it rather than adding an exception.

## D4. `install` requires an explicit target

No default scope. The three named targets are:

| Flag | File | F4 scope |
|---|---|---|
| `--user` | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json` | you, every project |
| `--project` | `$PWD/.claude/settings.json` | everyone in the repo, **committed** |
| `--local` | `$PWD/.claude/settings.local.json` | you, this project, gitignored |
| `--file PATH` | literal path | escape hatch (a mounted container config dir, a managed file an admin is authoring) |

Guessing here is how a personal endpoint ends up committed to someone's
repository, or how a team-wide file gets a `service.name` that only makes
sense on one laptop. The cost of requiring a flag is one line of usage text.

`--project` additionally prints to stderr that the file is meant to be
committed and that, per F7, a shared `service.name` groups the team's
sessions under one `stats/<name>/` folder (they still split by session id, so
nothing is lost -- see D9).

## D5. The merge algorithm, exactly

All of it in `$PYTHON`, stdlib `json`. In order; any `abort` changes nothing
on disk and exits 1.

1. **Target missing.** Create parent directories, write
   `{"env": {...}}`. Report `created <path>`.
2. **Target unreadable** (permissions, is a directory): abort, naming the
   OS error.
3. **Target does not parse as JSON:** abort, printing
   `json.JSONDecodeError`'s message with line and column. Do not attempt a
   repair, a comment strip or a trailing-comma fix. Per F8 the user's whole
   settings file is already inert for Claude Code; `install`'s job is to say
   so, not to guess at what they meant. The message points at the
   line/column and at `claude --debug`.
4. **Top level is not a JSON object** (a list, a string, `null`): abort.
5. **`env` exists but is not an object:** abort. Never replace it.
6. **Merge, per key, only inside `settings["env"]`.** For each of the five:
   - absent -> insert.
   - present and **equal** to what we would write -> leave it, count it as
     already-configured.
   - present and **different** -> a conflict; see D6.
   Every other key of `settings["env"]`, and every other top-level key
   (`permissions`, `hooks`, `plugins`, ...), is carried through untouched.
   Key insertion order is preserved (`json.load` keeps document order), so
   the diff of the file is confined to the keys actually touched plus
   whatever reindenting `json.dump(indent=2)` implies.
7. **Nothing to do** (all five already equal): print
   `already configured, no change` and exit **0**. Re-running `install` must
   be a no-op, not a rewrite.
8. **Write** per D7.
9. **Report** what changed: one line per key, `+ KEY = value` for an
   insert, `~ KEY: old -> new` for a forced overwrite, `= KEY` for an
   unchanged one, then the path written.

`--dry-run` performs 1-7 and prints the resulting *whole file* to stdout
instead of writing, so a cautious user can diff it themselves.

## D6. A conflicting telemetry key refuses, and `--force` overwrites

The specified question: an existing `OTEL_EXPORTER_OTLP_ENDPOINT` in the
target file whose value differs from ours.

Rejected -- **silently overwrite**: the value could be a deliberate
corporate collector, and a tool that quietly redirects a team's telemetry to
localhost because someone ran a convenience command is indefensible.
Rejected -- **keep the existing value and report `ok`**: `install` would
claim success while the session still points somewhere else.
Rejected -- **merge/append**: these are scalars; there is nothing to merge.

Chosen: **abort with a per-key report, and offer exactly one escape hatch.**

```
otelctl: refusing to change 1 existing value in /home/zet/.claude/settings.json
  ~ OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel.corp:4318" -> "http://localhost:4318"
  (2 keys would be added, 2 already match)
re-run with --force to overwrite, or edit the file yourself
```

Exit 1, nothing written -- including the non-conflicting keys, so the file
never lands in a half-merged state. `--force` applies every key and prints
the same table with `~` lines marked as applied.

Note the interaction with D5.7: this only triggers on a *differing* value,
so the idempotent re-run never asks for `--force`.

## D7. Atomic write, then verify, and no backup files

`install` writes to a temporary file in the *same directory* and
`os.replace()`s it into place, so an interrupted or failing write can never
leave a truncated `settings.json`. Mode is preserved when the file existed
(`shutil.copymode`), and `0600` for a newly created one.

Then **verify, after the replace**: re-read the file, parse it, and assert

1. it parses;
2. every top-level key present before is still present and deep-equal,
   except `env`;
3. inside `env`, every key that was not one of our five is deep-equal.

If any assertion fails, restore the original bytes (held in memory since
step 2 of D5) and exit 1 with a bug report request. This is deliberately
chosen *over* a `settings.json.bak-<timestamp>` file: a backup guards only
against a bug in our own merge, litters the user's config directory, and
puts a second copy of a file that may contain tokens on disk. A read-back
verification catches the same class of bug, fixes it automatically, and
leaves nothing behind.

## D8. `install --docker` is refused unless the target is `--file`

`--docker` produces `host.docker.internal`, which resolves only *inside* a
container. A host's own `~/.claude/settings.json` with that endpoint
configures every host session to export to a name it cannot resolve -- the
exact false configuration `doctor` had to grow a special case for
(`docs/design-otel-doctor.md`, check 4 trap 1).

So `install --docker` with `--user`/`--project`/`--local` exits 2:

```
otelctl: --docker writes host.docker.internal, which only resolves inside a
container. That endpoint in a host settings file breaks every host session.
For a container, either point --file at that container's config dir, or run
`otelctl.sh settings --docker` and paste it inside the container.
```

`--file` is allowed, since a mounted or otherwise reachable container
config directory is a real case and the user has named the path explicitly.
Print-only `settings --docker` is always allowed.

## D9. `service.name`, and F7 confirmed

`_stats_path` (`otel/aggregate.py:100-102`) uses `service.name` as a
*folder* and the session id as the filename. So a `service.name` shared by a
whole team in a committed `.claude/settings.json` produces
`stats/<team-name>/session-<id>.json` per session: sessions are **grouped**,
not collapsed. That is a reasonable reading of a project-scoped name, and no
change follows from it. It is worth one stderr line on `install --project`
so nobody expects per-developer folders from a shared file.

Two further notes, documentation only:

- A session that cld launches already gets `service.name` from cld's own
  injection. Per F2, a `settings.json` entry in the container would *win*
  over that export -- so a cld user who installs into a container's user
  settings will see every session in that container filed under one name.
  `otel/` takes no action on this; it is stated in the README so the
  surprise is documented rather than discovered.
- `OTEL_RESOURCE_ATTRIBUTES` values may not contain spaces (`monitoring-usage`
  is explicit, and check 4 already fails on it). `--service-name` therefore
  rejects a value containing whitespace, a comma or an `=`, rather than
  writing a value Claude Code will reject.

## D10. `start`/`restart` output gains one line, not a third block

`print_env_block` already prints two labelled blocks. A third would push the
useful part of `start`'s output off a short terminal. Instead, one line after
the two blocks:

```
Prefer a config file to shell exports? `./otelctl.sh settings --help`
```

---

# Part 2 -- `doctor` check 4 stops being blind

## D11. What breaks, in both directions

Check 4 today inspects `doctor`'s **own process environment**
(`otel/otelctl.sh:392-502`). Once the configuration lives in a settings
file, that is wrong in three distinct ways:

1. **From a plain terminal:** the user configured `~/.claude/settings.json`
   and exported nothing. Check 4 reports `warn -- no telemetry variables set
   in this shell` and the summary says the pipeline is healthy but this
   shell will not export to it. Telemetry is in fact flowing. A warning that
   fires on a *correct* setup is the same defect class as the
   `host.docker.internal` false-fail that the doctor design fought to
   eliminate.
2. **From inside Claude Code's Bash tool:** per F6, `OTEL_*` is withheld
   from subprocesses on purpose -- but `CLAUDE_CODE_ENABLE_TELEMETRY` is
   *not* an `OTEL_*` variable and is passed through. So `doctor` sees
   exactly one telemetry variable set, which today's rule reads as "this
   shell is claiming to be configured" and validates **strictly**: two or
   three hard `fail`s (`OTEL_METRICS_EXPORTER is not set`,
   `OTEL_EXPORTER_OTLP_PROTOCOL is not set`, `..._ENDPOINT is not set`), a
   non-zero exit, and a "telemetry is NOT being collected" verdict, on a
   session that is exporting perfectly. This one is worse than a missed
   warning: it is a confident false failure.
3. **Contradiction is invisible.** A shell export shadowed by a settings
   file (F2) is dead, and today's check reports the dead value as the live
   one.

## D12. The source ladder, and what `doctor` cannot see

Resolve in F3's order, highest precedence first, per variable:

| Tag | Source | Path |
|---|---|---|
| `managed` | managed settings file | `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), `/etc/claude-code/managed-settings.json` (Linux, WSL) -- F12 |
| *(none)* | `claude --settings` | **unreadable**: per-invocation, may be inline JSON (F15) |
| `local` | project local | `$PWD/.claude/settings.local.json` |
| `project` | project shared | `$PWD/.claude/settings.json` |
| `user` | user | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json` (F13) |
| `shell` | `doctor`'s own process environment | -- |

Deliberately **not** read, and named as such in one caveat line rather than
silently omitted: `--settings`, MDM/registry/server-managed sources,
`managed-settings.d/` drop-ins, and any settings file belonging to a
different working directory or a different machine.

Project tiers are read **from `doctor`'s current directory only**. Whether
Claude Code searches ancestor directories is not documented (see § Verified
facts), so `doctor` does not pretend to know: if the repository root differs
from the current directory and has its own `.claude/settings*.json`, those
paths are *listed* in the caveat as files that may apply instead, and are
not merged. Repository root is `git rev-parse --show-toplevel` /
`jj workspace root` if either resolves, otherwise the check is simply not
made -- `otel/` must not require a VCS.

Per-variable merge, not per-file (F3): a `user` endpoint and a `project`
`service.name` coexist, and each reported line names its own source.

## D13. The seam: a second invocation of the *existing* seam, not a new one

JSON parsing goes in `$PYTHON`. Hand-rolled JSON parsing in bash is a defect
farm and `jq` is banned.

It cannot go inside the *existing* python block, though: that block runs
after check 4 in the report (chain order -- env before port), and check 4
needs the resolved values *before* it prints anything. Folding check 4 into
that block would either reorder the report or move ~110 lines of tested bash
verdict logic into python for no reason.

So: the **same seam, invoked a second time**, earlier. Same technique, same
gotcha, same `DOCTOR_*`-env-in convention (`docs/design-otel-doctor.md` D6),
and the protocol is a strict superset of the existing one -- two record
kinds, discriminated by the first field:

```
report|STATE|LABEL|MESSAGE|DETAIL      # rendered by doctor_report, as today
cfg|VAR|SOURCE_TAG|VALUE|PATH          # data for check 4
tier|SOURCE_TAG|PATH|FOUND             # which tiers exist, for the legend line
```

(`PATH` and the `tier` record were added during implementation and are
ratified here: the legend of D12 and the contradiction detail of D17 both
have to name the file a value came from, and re-deriving that in bash would
duplicate the resolver's tier logic.)

Read with process substitution, never a pipe -- a pipe puts the array
appends and the report counters in a subshell and loses both. The `|` and
newline scrubbing that `report()` already does applies to `cfg` values too;
a settings value containing a literal `|` becomes `/`, which only affects
display.

Resolver responsibilities: locate the files, parse them, apply F3
precedence + F11's managed inversion, emit `cfg` records for the effective
config, and emit `report` records for the file-level problems only it can
see (unparseable file, `env` not an object, non-string value, unreadable
file). It performs **no** validation of the telemetry values themselves --
that stays in bash, where it is already tested.

## D14. No associative arrays, no `eval`

macOS ships bash 3.2 and is a first-class target for this folder (the whole
`host.docker.internal` / BSD `date -j` / `stat -f %m` apparatus exists for
it), so `declare -A` is out. `eval` of file-derived strings is out too: a
settings file is user data, and there is no reason to hand it to the shell
parser.

Instead, three parallel indexed arrays filled by the read loop
(`CFG_NAMES`, `CFG_SRCS`, `CFG_VALS`) plus two lookup helpers doing a linear
scan over at most a dozen entries:

- `doctor_cfg_get NAME` -- effective value, empty if none.
- `doctor_cfg_src NAME` -- source tag, empty if none.
- `doctor_cfg_path NAME` -- the file it came from, empty for shell/absent.

(Named with the `doctor_` prefix rather than a bare `cfg_get`, to stay clear
of the `cfg()` record emitter inside the python seam.)

**`doctor_cfg_get` falls back to `${!NAME:-}`** (the process environment) when the
resolver produced no record for that name, with `cfg_src` reporting `shell`.
That fallback is what makes the whole change degrade gracefully: with
`PYTHON_OK=0`, or in a unit test that never runs the resolver, check 4
behaves exactly as it does today.

## D15. Advisory vs strict, adapted (D5 of the doctor design)

The old rule -- *nothing set: one advisory warn; anything set: validate
strictly* -- was right for one source. Its purpose was that `doctor`'s shell
is usually not `claude`'s shell, so shell state is weak evidence. A settings
file is not weak evidence: it applies to every session that starts, so a
broken value in one is a *certain* failure.

The rule becomes a function of **where the effective config came from**:

| Situation | States | Verdict clause when these are the only failures |
|---|---|---|
| At least one value from a settings file | strict: contradictions are `fail` | "Claude Code's telemetry config is broken" -- **not** softened |
| No settings-file value, some process-env value | strict: contradictions are `fail` (unchanged) | "the pipeline is healthy, but this shell will not export to it" (unchanged) |
| Nothing anywhere, and not a Claude Code child | one `warn`, plus the ladder of files searched | n/a (warnings do not fail) |
| Nothing readable because we are a Claude Code child (D16) | one `warn` | n/a |
| `PYTHON_OK=0` | `skip` for the settings tiers, then the process-env rules above | unchanged |

Non-regression, which was the explicit constraint: **a healthy pipeline is
still never red because `doctor` ran in a different terminal.** All four
paths to "no config visible" are `warn` or `skip`, never `fail`, and
`doctor` keeps exiting 0 on warnings. What *has* changed is that a genuinely
broken settings file now fails and exits 1 -- correctly, since that file
breaks every session on the machine, not just this terminal.

## D16. Running inside Claude Code: the process environment is not evidence

If `CLAUDE_CODE_CHILD_SESSION=1` (F14 -- the precise signal;
`CLAUDECODE` is not, because IDE integrated terminals set it without any
withholding), then per F6 this process was *deliberately* denied the
`OTEL_*` variables. So:

- Drop `shell` as a source for the `OTEL_*` names entirely. Their absence
  carries no information and must never produce a `fail` or a `warn`.
- `CLAUDE_CODE_ENABLE_TELEMETRY` alone, visible without any `OTEL_*`, must
  **not** trip strict mode. This is D11's case 2, and it is the single
  most important false-failure this change removes.
- If the settings files supplied nothing either, report one line and stop:

```
[warn] telemetry cfg     running inside Claude Code -- OTEL_* is withheld from tool subprocesses
                         -> this session's telemetry config cannot be read from here; run
                            `otelctl.sh doctor` in a plain terminal, or check the settings files above
```

- If the settings files *did* supply values, validate them strictly and
  ignore the process environment: that is exactly the case where the files
  are the whole truth.

Older Claude Code versions (< v2.1.172) do not set
`CLAUDE_CODE_CHILD_SESSION`. Treat `CLAUDECODE=1` without it as a weaker
signal: still drop `shell` as a source for `OTEL_*` (the cost of being
wrong is a missing advisory, versus a confident false failure the other
way), and say which signal was used in the detail line.

## D17. A contradiction between sources is a `warn`, never a `fail`

Per F2 the file wins and the shell export is dead. Two sub-cases:

- The effective (file) value is broken -> the strict validation already
  fails it. The contradiction adds *why the user is confused*, not a second
  failure.
- Both values are individually valid but different -> nothing is broken,
  yet the user believes a value that is not in play. The classic version is
  two different `service.name`s: stats land in a folder the user is not
  looking in.

So: one aggregated `warn` naming the shadowed variables and the winner, and
never a `fail`, because a `fail` here would turn a *working* pipeline red --
the exact regression D15 forbids.

```
[warn] telemetry cfg     2 shell exports are shadowed by ~/.claude/settings.json and have no effect
                         -> OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_RESOURCE_ATTRIBUTES -- the file's
                            values are the ones in use (settings files override shell exports)
```

## D18. A settings file that does not parse is a `fail`

Per F8, Claude Code reports the file as a Settings Error at the next start,
so **none** of its keys apply -- including permissions and hooks that have
nothing to do with telemetry. `doctor` is holding the answer to a question
the user has not thought to ask yet, and it costs one line:

```
[fail] telemetry cfg     ~/.claude/settings.json is not valid JSON (line 12 column 3: Expecting ',')
                         -> Claude Code applies none of that file's settings until it parses
```

Its telemetry values are then simply absent from the merge -- matching what
Claude Code itself will do.

Adjacent, lesser file problems: `env` present but not an object -> `warn`
(Claude Code will reject or ignore it; we do not assert which). A non-string
value such as `"CLAUDE_CODE_ENABLE_TELEMETRY": 1` -> `warn`, quoting the
declared type from F1; the resolver still uses `str(value)` for the merge so
the rest of the report stays useful.

## D19. An empty string is an explicit unset

F9: `"VAR": ""` cancels a shell export and counts as unset. This is a
genuine footgun -- an empty entry in a high-precedence file silently turns
telemetry off for every session, and a shell export that looks correct stays
dead. The resolver emits such a variable with a distinct source tag
(`user:cleared`), and check 4 words it as what it is:

```
[fail] telemetry cfg     CLAUDE_CODE_ENABLE_TELEMETRY is cleared to "" by ~/.claude/settings.json
                         -> an empty value counts as unset; remove the entry or set it to "1"
```

`fail` rather than `warn` when other telemetry variables are configured
(strict mode's existing rule for a half-configured setup), and it correctly
does *not* fire when nothing else is set either.

## D20. The managed tier

Read the two POSIX paths from F12. Emit `cfg` records tagged `managed`.
Nothing special is needed for the common consequence -- a managed endpoint
pointing at a corporate collector simply fails the existing effective-value
checks (port mismatch, or a host that is neither loopback nor
`host.docker.internal` nor a literal IP) -- but the *detail* line must say
where it came from and that the user cannot override it:

```
[fail] telemetry cfg     endpoint host "otel.corp" is not this collector (not loopback,
                         host.docker.internal, or a literal IP)
                         -> set by managed settings (/etc/claude-code/managed-settings.json); your
                            organization's policy wins and nothing you set overrides it
```

One real subtlety must be implemented, because ignoring it produces a
confident wrong answer: per **F11**, when managed settings supply the
*generic* `OTEL_EXPORTER_OTLP_ENDPOINT`, Claude Code **removes**
developer-set per-signal endpoints at startup. Ordinary resolution prefers
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` over the generic one (F16), so a naive
resolver would report the per-signal value that Claude Code will have
discarded. The rule, applied to endpoint and protocol:

> if the managed tier supplies the generic variable, per-signal values from
> every lower tier are dropped, and the drop is reported as a `warn` naming
> F11's behaviour.

This is version-dependent behaviour (the docs note it changed in v2.1.217),
so the warn says so rather than asserting the reader's version.

Invisible managed sources (MDM, registry, server-managed, `managed-settings.d/`)
appear only in D12's caveat line. `doctor` never claims a machine has no
managed policy -- only that it found no managed *file*.

## D21. The relaunch caveat: an unconditional note, and no fake check

F5: OpenTelemetry reads its variables once at startup. Edit the settings
file and every already-running session keeps its old values. This *will*
confuse people: they will fix an endpoint, run `doctor`, see all green, and
still get no stats from the session they have had open all afternoon.

So whenever any value came from a settings file, the config check's detail
line carries:

```
-> settings-file values are read once at startup: sessions already running keep the values
   they started with. Relaunch `claude` to apply a change.
```

Deliberately **not** implemented: any attempt to detect the stale case.
Everything `doctor` could measure fails to distinguish "a session started
before the edit" from an innocent explanation:

- *settings mtime vs. the newest stats-file mtime* is backwards. On a busy
  machine an export lands every 60 s, so the comparison stays quiet exactly
  when a stale session is running, and fires when nothing is running at all
  -- the harmless case.
- *the configured `service.name` missing from `stats/`* false-warns
  permanently on any host whose collector also receives from containers or
  other names (i.e. every cld host, and the author's own machine).
- session start times are not knowable from `otel/` without reading Claude
  Code transcript internals, which is a dependency this folder should not
  take for a warning.

A stated caveat that is always true beats a check that is wrong in both
directions. This is the same standard the doctor design applies to
unexercised checks: a known gap ships; a false signal does not.

## D22. Label and verdict wording

The label `shell env` becomes **`telemetry cfg`** (13 chars, inside the
existing `%-17s` column). Keeping `shell env` on a line whose value came
from `~/.claude/settings.json` is precisely the misreporting this change
exists to remove, and a single check must keep one label -- the report
grammar is one label per check, so alternating `settings`/`shell env` within
check 4 would read as two different checks.

Consequences, both deliberate:

- `doctor_summary`'s softening branch compares fail labels against the
  literal `"shell env"` (`otel/otelctl.sh:520`); it compares against
  `"telemetry cfg"` instead, and now also consults whether any value came
  from a file (D15) to choose between the two verdict clauses. A new
  variable `DOCTOR_CFG_FROM_FILE` (0/1), set by check 4, carries that.
- ~20 test assertions hardcode `shell env` in the middle field. The change
  is mechanical (§ Effect on the existing tests).

If that churn is judged not worth it, the fallback is to keep the label and
add the source in each message -- everything else in this design is
unaffected. Recorded so the choice is reversible without redesign.

---

## Report format

`doctor`'s existing grammar, unchanged: `[state] label message`, with `->`
continuation lines. Check 4 gains a leading legend line so every later
`[tag]` is self-explanatory.

**Config in the user settings file, plain terminal, healthy:**

```
[ ok ] telemetry cfg     resolved from: user=~/.claude/settings.json (5 values), shell (0)
                         -> not visible to doctor: `claude --settings`, MDM/server-managed policy,
                            and any settings file outside /home/zet/work/api
[ ok ] telemetry cfg     telemetry enabled [user]
[ ok ] telemetry cfg     OTEL_METRICS_EXPORTER includes otlp [user]
[ ok ] telemetry cfg     protocol http/json [user]
[ ok ] telemetry cfg     endpoint http://localhost:4318 [user]
[ ok ] telemetry cfg     resource attributes: service.name=api-work [user]
[ ok ] telemetry cfg     export interval 60000ms
                         -> a new session's first stats file can take that long to appear;
                            settings-file values are read once at startup, so relaunch `claude`
                            to apply a change
```

**Run from inside Claude Code's Bash tool, config in a project file:**

```
[ ok ] telemetry cfg     resolved from: project=.claude/settings.json (5 values)
                         -> running inside Claude Code (CLAUDE_CODE_CHILD_SESSION): OTEL_* is
                            withheld from tool subprocesses, so the shell is not a source here
[ ok ] telemetry cfg     telemetry enabled [project]
...
```

**The regression this change prevents** -- same situation as above, but with
today's check 4:

```
[fail] shell env         OTEL_METRICS_EXPORTER is not set
[fail] shell env         OTEL_EXPORTER_OTLP_PROTOCOL is not set
[fail] shell env         OTEL_EXPORTER_OTLP_ENDPOINT is not set

7 ok, 3 failures -- the pipeline is healthy, but this shell will not export to it
```

**Broken user file shadowing a good shell export:**

```
[fail] telemetry cfg     ~/.claude/settings.json is not valid JSON (line 4 column 5: Expecting ',')
                         -> Claude Code applies none of that file's settings until it parses
[ ok ] telemetry cfg     resolved from: shell (5 values); user file unreadable (see above)
...
9 ok, 1 failure -- the pipeline is healthy, but this shell will not export to it
next: fix the JSON syntax in /home/zet/.claude/settings.json
```

Summary rules are otherwise as designed in `docs/design-otel-doctor.md`:
counts in `ok, warnings, failures` order, one `next:` naming the earliest
failing link, exit 1 if any `fail`.

## Effect on the existing tests

41 tests in `tests/test_otelctl_doctor_sh.py` and `tests/test_pack_otel.py`.

**Unaffected (no change expected):**

- All of `tests/test_pack_otel.py` -- the manifest is derived from the
  directory listing and no file is added or removed.
- `TestDoctorCheckCollectorMountsDrift` (4 tests) -- untouched code path.
- `TestDoctorRoundTrip` (2 tests) -- check 5 is untouched. Note the
  resolver runs in these end-to-end invocations: it must therefore be
  robust to an absent `~/.claude`, and it must not read the *developer's
  real* user settings file in a way that changes an assertion. It does not:
  those tests assert on the `port` and `round trip` lines only.
- `TestDoctorSummary`: 5 of 7 unaffected.

**Deliberately changed, with the justification for each:**

| Test | Change | Why |
|---|---|---|
| `test_nothing_set_is_advisory_warn_only` | message becomes "no telemetry configuration found" and lists the files searched; still exactly one `warn` line, still exit 0 | the old message asserts a claim about the shell that is now only part of the truth. The *state* -- one advisory warn, never a fail -- is the behaviour under test and does not change |
| ~19 `TestDoctorCheckEnv` assertions | `shell env` -> `telemetry cfg` in the middle field | mechanical consequence of D22 |
| `test_all_failures_shell_env_softens_verdict`, `test_mixed_failures_including_non_shell_env_is_strict` | `fail_labels` use the new label | same |
| `test_all_failures_shell_env_softens_verdict` | gains a sibling that sets `DOCTOR_CFG_FROM_FILE=1` and asserts the *unsoftened* clause | D15's core rule: a settings-file failure is authoritative and must not be softened |

Every `TestDoctorCheckEnv` test keeps working *as a test of the same logic*
because of D14's fallback: the harness supplies values through the process
environment, the resolver never runs, and `cfg_get` reads `${!NAME}`. Source
tags appear as a `[shell]` suffix, which the existing `startswith` /
`in` assertions tolerate.

**One hazard to fix while touching this file:** `_run_env` builds an
environment of `{PATH, PORT}` with **no `HOME`**. With HOME unset, bash `~`
expands from `getpwuid`, so a naive settings lookup inside
`doctor_check_env` would read the *developer's own* `~/.claude/settings.json`
and make the suite machine-dependent. Two defences, both required: the file
lookup lives in the resolver (never called by these tests), and the resolver
honours `CLAUDE_CONFIG_DIR` plus a `DOCTOR_SETTINGS_*` override so new tests
can point every tier at a `tmp_path`.

## New tests

Following the same precedent (drive the real bash/python out of
`otelctl.sh`, never reimplement it):

1. **Resolver precedence** -- write settings files across `user`, `project`,
   `local`, `managed` (all under `tmp_path`, all tiers overridden), assert
   the emitted `cfg` records: per-variable merge, highest tier wins, correct
   source tags.
2. **Resolver robustness** -- unparseable file -> `report|fail`; `env` not
   an object -> `warn`; non-string value -> `warn`; unreadable file ->
   `report`; missing files -> silence, not an error.
3. **Empty string** -> `cleared` tag and D19's `fail` wording.
4. **F11 inversion** -- managed generic endpoint + user per-signal endpoint
   -> the per-signal value is dropped, with the warn.
5. **Claude Code child** -- `CLAUDE_CODE_CHILD_SESSION=1` plus
   `CLAUDE_CODE_ENABLE_TELEMETRY=1` and no `OTEL_*`: exactly one `warn`,
   **no `fail`**, exit 0. This is the regression test for D11 case 2 and the
   single most valuable new test.
6. **Contradiction** -- shell and user file both set the endpoint: one
   `warn`, the file's value validated, no `fail`.
7. **`settings` output** -- valid JSON, five string values, `--docker`
   swaps the host, `--service-name` substitutes, stdout is pure JSON
   (stderr carries the advice).
8. **`settings install`** -- the whole merge table: create-from-nothing;
   unrelated top-level keys (`permissions`, `hooks`) and unrelated `env`
   keys survive byte-for-byte in content; idempotent re-run reports no
   change and rewrites nothing; conflicting value refuses with exit 1 and
   an unchanged file; `--force` applies it; invalid JSON aborts with the
   file untouched; `--dry-run` writes nothing; `install --docker --user`
   exits 2.
9. **Verification path** -- the D7 read-back. Per
   `feedback_verify_that_a_check_can_fail`: prove the guard can fail by
   pointing the merge at a stub that drops a key, and assert the original
   bytes are restored.

## Also part of this change

- `otel/README.md`: a subsection under "Point a Claude Code session at it"
  covering the settings-file route, the scope table (F4), the relaunch
  caveat (F5), the subprocess carve-out (F6, since it explains why `doctor`
  from a Bash tool says what it says), and the cld interaction from D9.
- `otel/QUICK-START.md`: step 2 gains the settings-file alternative, two
  lines, with `env` staying first for the one-off terminal.
- `otel/otelctl.sh`'s header usage line and the two usage strings.
- `docs/design-otel-doctor.md`: a short note at check 4 and D5 pointing
  here, so the older document does not read as current.

## Implementation notes

- `set -euo pipefail` is active. Every probe needs `if ! ...` or `|| true`,
  and `local x=$(cmd)` masks `cmd`'s status -- declare, then assign.
- Process substitution, not a pipe, for **both** python blocks. A pipe puts
  the `CFG_*` array appends and the report counters in a subshell.
- The resolver must never fail the run: any exception inside it becomes a
  `report|warn` line, and the exit status is ignored. A `doctor` that dies
  because a settings file was odd is worse than one that cannot read it.
- Do not make check 5 depend on check 4 -- unchanged from the doctor design.
- `install` and the resolver share the file-locating logic; put it in one
  python helper string used by both, so the paths cannot drift.
- Report an unexercised check as untested, never as passing.

## What was verified, and how

**Verified against the live docs** (fetched 2026-09-01,
`code.claude.com/docs/en/{settings,settings-reference,env-vars,monitoring-usage,managed-settings,cli-reference}`):
every row of § Verified facts, including the two managed-settings paths of
F12 (asserted only for macOS and Linux/WSL) and the strict-JSON rule of F8.

**Verified against this repo:** `_stats_path`
(`otel/aggregate.py:100-102`); check 4's current env-only implementation
(`otel/otelctl.sh:392-502`); the `shell env` label coupling in
`doctor_summary` (`otel/otelctl.sh:519-527`) and in ~21 test assertions;
`tests/test_pack_otel.py`'s manifest being derived from the directory
listing; `_run_env`'s `{PATH, PORT}`-only environment and its missing `HOME`.

**Not verified, and left as a stated limitation rather than a guess:**
whether Claude Code searches ancestor directories for
`.claude/settings.json` (D12 reports instead of assuming), and the Windows
managed path's behaviour under Git Bash (out of scope -- WSL uses the Linux
path).

**Not exercised at all:** anything needing a live collector or a real Claude
Code session. This container has no docker daemon, so no end-to-end run of
`doctor` against a real collector, and no proof that a Claude Code session
reading these variables from a settings file exports to the collector. The
fake-collector technique in `TestDoctorRoundTrip` covers the pipeline half;
the "Claude Code actually honours this file" half rests on F1/F2/F10 from
the documentation and should be confirmed by hand once, on a host, before
this is called done.

---

## Review of the implementation

Reviewed by running the built script out of `otel-t6`, not by reading it
alone. What was executed, and what it showed:

- **`env` output is byte-identical** to the pre-change script for both `env`
  and `env --docker` (diffed). `print_env_block` gained exactly one line.
- **The merge matrix**, against a `settings.json` holding `permissions`,
  `hooks`, `plugins` and an unrelated `env` key: a conflicting endpoint is
  refused with the before/after table and the file is unchanged; `--force`
  applies it; every unrelated key and the key order survive; a second run
  prints `already configured, no change` and rewrites nothing; invalid JSON,
  a non-object `env` and a non-object top level each abort with the file
  untouched; `install --docker --user` exits 2; `--dry-run` writes nothing
  and `--dry-run --force` previews the merged file, which is the way out of a
  refusal; a created file is mode 0600; no temp files are left behind.
- **D11 case 2, in a live Claude Code child.** This container is one:
  `CLAUDE_CODE_CHILD_SESSION=1`, `CLAUDE_CODE_ENABLE_TELEMETRY` present, and
  **zero** `OTEL_*` variables even though the launcher injected them into the
  container environment -- F6 empirically confirmed, including that the
  withholding strips inherited values. The pre-change check 4 run here emits
  three hard `fail`s on a healthy pipeline; the new one emits a single `warn`
  naming the signal. That is the defect this change exists to fix, observed
  rather than argued.
- **Precedence and provenance**, with real files at the user and project
  tiers: the project `service.name` wins, each line is tagged with its own
  source, and a live shell export shadowed by a file value produces one
  `warn` with the winning path -- no `fail`.
- **The D15 test can fail.** Reverting `doctor_summary` to the pre-change
  two-way branch (softening on label alone) turns
  `test_all_failures_telemetry_cfg_from_file_is_not_softened` red while its
  two siblings stay green; restoring makes it green again. The D7
  verification guard is likewise fault-injected two ways (drop an unrelated
  key, truncate the write) with an unfaulted sanity check alongside.
- **The suite**: 53 tests in `tests/test_otelctl_doctor_sh.py` pass. In
  `tests/test_pack_otel.py`, 11 pass in-repo; run from an out-of-tree copy
  one of them fails because `pack.sh` reads the source revision from the VCS
  and finds none -- an artifact of the copy, confirmed by the same test
  passing in the repo.

Open items, none blocking, all minor:

1. `settings [install] --service-name` with no value dies from `shift 2`
   under `set -e`: exit 1, no message. The convention elsewhere is exit 2
   plus the usage line.
2. `--service-name ""` silently becomes the `my-session` placeholder *and*
   suppresses the "edit service.name" hint, because the empty value still
   counts as given. Reject an empty name instead.
3. The contradiction warning reads "1 shell export shadowed ... and **have**
   no effect"; the pluralisation covers the noun but not the verb.
4. In the merge, the D7 verification baseline (`baseline = existing`) is the
   same object the merge mutated in place. It is correct today, because only
   the five telemetry keys are mutated and the comparison skips exactly
   those -- but it means the guard could not catch a future edit that touched
   any other key, which is the whole thing it exists to catch. A `deepcopy`
   before mutating, or re-parsing `original_bytes`, restores that.

Still untested anywhere, unchanged from the section above: no live collector
(no docker daemon in either container), and no proof that a real Claude Code
session reads these variables out of a settings file. The second one needs
one manual check on a host.
