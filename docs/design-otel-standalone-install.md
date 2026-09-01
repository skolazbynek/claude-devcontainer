# A portable, self-contained copy of `otel/`

> Requested by `docs/review-otel-public-release.md` § "To-do" **T1. Standalone
> install** — "make `otel/` runnable from a single install step, without
> cloning the whole `cld` repo." That review flagged this item as *needs
> planning*; this document is the plan. Implementation lands separately.

## Problem

`otel/` is already standalone *as code*: five flat files, no import of cld, no
cld env var, `otelctl.sh` resolves its own directory and mounts its own config
by absolute path, `aggregate.py` is stdlib-only. The only tie to cld is
`~/.cld/otel` as the default state directory, an accepted exception.

It is not standalone *as an artifact*. Getting it onto a machine today means
cloning a 25k-line docker-orchestration repo — `cld/`, `broker/`,
`graphqlserver/`, `runtests/`, a poetry project — for 30 KB of files that need
none of it. There is no single thing to hand someone.

## Constraints

Non-negotiable, from the task framing:

1. **No build step.** `aggregate.py` stays stdlib-only Python 3, `otelctl.sh`
   stays plain bash. The install mechanism must not introduce a packaging or
   build toolchain of its own.
2. **`otel/` in this repo stays the single source of truth.** No second copy
   that is separately maintained and can drift. Whatever ships must be
   *derived* from these exact bytes.
3. **No *dependence* on hosting.** The mechanism must work for a recipient
   with no network access and no repo access. The task framing originally said
   to assume nothing about publication; the remote has since been confirmed
   public (see § Hosting), which *adds* an on-ramp but does not relax this
   constraint — the offline recipient still rests on it.
4. **The install step must not depend on cld either.** A recipient machine
   with no cld, no docker-orchestration tooling and no repo access must be
   able to consume whatever we produce.

Two facts found while researching, both load-bearing below:

- **This working copy is a non-colocated jj repo.** `.jj/` exists, `.git/`
  does not. `git archive HEAD otel/` — the obvious zero-code answer — does not
  run here at all: `git -C otel rev-parse HEAD` fails with "not a git
  repository". jj has no `archive` command (`jj sparse` manages which paths
  are materialised in *your own* working copy; it fetches nothing from a
  remote).
- **`otel/` is small enough to be a text file.** 30 KB on disk before this
  change, 10 KB gzipped, 14 KB base64'd. A finished artifact measures ~26 KB
  (the packer itself travels in the payload, adding ~13 KB) — still pasteable
  into a wiki page or a chat message, not just attachable.

## Options considered

### A. Hosted one-liner (`curl … | tar xz`)

The best UX for anyone with network access: one command, nothing to
hand-carry, always current with whatever is on the published branch.

The remote is public — confirmed, not assumed (§ Hosting) — so this is
**adopted as an additional, documented on-ramp**, and it leads the README
because it is the simpler path for the common case.

It is *not* the mechanism, for reasons that survive the confirmation intact:
it needs a working network route to `github.com`, it delivers whatever is on
the published branch rather than the tree in front of you, and it gives a
recipient nothing to re-share onward. Constraint 4's recipient — no cld, no
repo access, possibly no network — is still served only by option D. The two
are complements, and D does not shrink because A exists.

### B. Package index (pip / pipx / Homebrew / npm)

Rejected on constraint 1. `aggregate.py` has no dependencies to resolve, so a
distribution would exist purely to move three files; and it would drag in
exactly what the constraint forbids — a `pyproject.toml` for the otel subtree,
a wheel build, a version-bump-and-publish ritual, a registry account, and a
release step that must be re-run every time `otelctl.sh` changes. It also
answers the wrong question: pip installs Python, and the entry point here is a
bash script that shells out to `docker`.

### C. Documented VCS partial checkout

Tell the user to fetch only `otel/`:

```
git clone --filter=blob:none --sparse <url> && git sparse-checkout set otel
```

Attractive because it is pure documentation — zero new code, zero drift
possible. Rejected on three counts:

- It still needs `git`, a network route and a clone on the target machine.
  The repo being public removes the credential problem but not the rest, and
  it is strictly more typing than option A's one-liner for the same result.
- `git archive --remote=…`, the variant that would avoid a clone entirely, is
  not served by GitHub at all (`git-upload-archive` has never been enabled
  there), and cannot be run from this working copy anyway, which has no
  `.git`.

Worth one line in the README as a convenience for people who *do* have repo
access, but it is not an install path for the person in the problem statement.

### D. A packer committed to this repo that emits a portable artifact

Someone who already has a checkout runs one script; out comes a single
self-contained file that runs on any machine with bash, tar and python3, and
travels over whatever channel already exists between the two people (scp, a
shared drive, a wiki attachment, a chat paste).

This satisfies every constraint: no build toolchain (tar, gzip and base64 are
not a toolchain), the artifact is generated from the live files on every run so
it cannot drift, it assumes no hosting, and it depends on nothing from cld.

**Chosen.** Two sub-decisions follow.

#### D1. Where the packer lives

| Option | Verdict |
|---|---|
| `scripts/pack-otel.sh` (repo tooling, next to `split_failures.py`) | Works, but the packer can then only ever be run from a cld checkout — a recipient cannot re-share what they were given. |
| A new `otelctl.sh pack` subcommand | Self-replicating and discoverable via `--help`, but mixes "run the pipeline" with "distribute the pipeline" in the one script that three sibling branches (T2, T3, T5) are editing right now. High conflict cost for no user-visible gain. |
| **`otel/pack.sh`, included in its own payload** | **Chosen.** |

`otel/pack.sh` sits with the files it packs, is discoverable by anyone who
lists the directory, and — because it is part of the payload — a recipient can
re-pack and pass it on without ever having repo access. It only ever reads its
own directory, so it works identically in a checkout and in an extracted copy.
It stays out of `otelctl.sh`, which keeps the runtime script focused and
avoids conflicting with the sibling tasks.

#### D2. Artifact shape

**Default: a self-extracting bash script** (`otel-standalone-<date>-<rev>.sh`,
~26 KB measured) — a readable header, then a base64 payload after a marker
line. One file, text-safe end to end: survives mail clients, wiki attachments,
and being
pasted into a chat window. Prior art is `makeself`; we do not vendor it, since
that would be a ~1000-line external build dependency to produce ~60 lines of
`awk | base64 -d | tar xz`.

**Secondary: `--tarball`**, emitting the inner `.tar.gz` directly. Some people
will not run a shell script they were sent, and answering that with "run the
script with `--list` first" misses the point. A tarball is inspectable with
tools they already trust. Same code path, ~5 extra lines: the self-extracting
form is just the tarball with a header bolted on.

Rejected within D2: appending raw gzip bytes and slicing them off with `tail
-c +N` (makeself's own trick). It is smaller — base64 costs ~35% — but a
binary tail is exactly what breaks when the file goes through a wiki, a
pastebin or a mail gateway, which is the channel this design exists to serve.

## Decision

Two on-ramps, one of which is code:

1. **A documented `curl … | tar xz` one-liner** (option A) — documentation
   only, no new code, for anyone who can reach `github.com`. Leads the README.
2. **`otel/pack.sh`** (option D) — one new file, plus tests and a README
   section. Running `./pack.sh` inside any copy of `otel/` produces a single
   self-extracting installer containing a verbatim copy of that directory. The
   recipient runs it, gets an `otel/` folder, and follows the existing
   `QUICK-START.md`. No network, no build, no second maintained copy, and the
   extracted files are bit-identical to the ones on the packing machine.

`pack.sh` is what makes the design hold when the network does not: offline and
air-gapped machines, no GitHub access, "someone handed me a file", and
re-sharing onward. Its scope is not reduced by the existence of the one-liner.

## Hosting

**The remote is public.** Verified by unauthenticated read from inside this
container:

```
$ curl -fsS https://api.github.com/repos/skolazbynek/claude-devcontainer
  "full_name": "skolazbynek/claude-devcontainer",
  "private": false,
  "visibility": "public",
  "archived": false,
  "default_branch": "main",
```

So the canonical location is **`skolazbynek/claude-devcontainer`, branch
`main`**. This resolves what an earlier draft carried as an open question for
the human; per that draft's own reasoning it changes what we *document*, not
what we *build*.

> **Note for whoever maintains these docs:** the URLs below bake in that
> account and repo name. Update this document, `otel/README.md` and
> `otel/QUICK-START.md` if the canonical location ever changes — a renamed
> account leaves GitHub's redirect in place for a while, but not forever, and a
> stale `curl` one-liner fails as a 404 that reads like a bug.

### The verified one-liner

Both commands below were run from this container against the live repo. The
extracted files are byte-identical to what `main` holds, and the `+x` bits on
`otelctl.sh` and `aggregate.py` survive the round trip.

**Document this form** — it is the portable one:

```
curl -fsSL https://codeload.github.com/skolazbynek/claude-devcontainer/tar.gz/main \
  | tar xz --strip-components=1 claude-devcontainer-main/otel
```

GitHub's codeload tarball wraps everything in a single top-level
`<repo>-<ref>/` directory, so `claude-devcontainer-main` is the deterministic
prefix for `main`, and `--strip-components=1` drops it.

**The prefix embeds the ref**, so the command is per-branch: documenting a tag
or a SHA instead means the prefix changes to match
(`claude-devcontainer-<tag>/otel`). Worth a line in the README next to the
command, since a reader swapping `main` for a tag will otherwise get "Not
found in archive".

Why the literal member name rather than a glob:

- **`--wildcards` is GNU-only, and GNU tar requires it for a glob member.**
  Tested here on GNU tar 1.35: `tar xz --strip-components=1 '*/otel'` prints
  "Pattern matching characters used in file names / Use --wildcards to enable
  pattern matching", extracts nothing, and exits 2. Adding the flag makes the
  same command work (also tested).
- **A literal path needs no pattern matching**, so it avoids the GNU-only flag
  entirely rather than betting on which tar the reader has. `--strip-components`
  is common to both implementations. This is the reasoning behind the choice,
  not a cross-platform test result.
- **Not verified on macOS.** There is no `bsdtar` in this container, so
  everything above was run on GNU tar only. libarchive documents bsdtar as
  treating members as patterns by default and as never having implemented
  `--wildcards`, which is what makes a glob form unportable in the other
  direction — but that is documentation, not a run here. Anyone with a mac
  should confirm the literal form once. If it does misbehave, the fallback to
  document is two steps (`curl -o repo.tar.gz …`, then `tar xzf repo.tar.gz
  --strip-components=1 claude-devcontainer-main/otel`), which changes nothing
  about member matching but separates a download failure from an extract
  failure.

### What the one-liner does *not* give you

Verified while testing, and worth stating in the README rather than
discovering later: **`main` can be behind your working copy.** Right now the
published `main` predates the `/rename` work — its `aggregate.py` is 8423
bytes against 16196 here, and its `README.md` differs too. The one-liner
delivers the published branch; `pack.sh` delivers the tree in front of you.
Concretely, `pack.sh` itself is not fetchable by `curl` until this change is
merged and pushed to `main`, and the same is true of T2–T5.

That is not a flaw in either path, but it is the reason the README must say
plainly what each one is for rather than presenting them as two spellings of
the same thing.

## What to build

### 1. `otel/pack.sh` (new)

Plain bash, `set -euo pipefail`, no dependency outside `tar`, `gzip`,
`base64`, `fold`, `awk`, `mktemp` and a sha256 tool.

```
usage: pack.sh [--out PATH] [--tarball] [--quiet] [-h]

  --out PATH   write the artifact here (default: ./<generated name> in $PWD)
  --tarball    emit the plain .tar.gz instead of a self-extracting script
  --quiet      suppress the summary; print only the artifact path
```

**Own directory.** `HERE="$(cd "$(dirname "$0")" && pwd)"`. Deliberately *not*
`readlink -f` as `otelctl.sh` uses: `readlink -f` is GNU-only on older macOS,
and `pack.sh` is the one script in this folder that a sender is likely to run
on a mac. (`otelctl.sh`'s own use of it is pre-existing and out of scope here —
see § Out of scope.)

**File discovery.** Every *regular file* directly in `$HERE` (non-recursive —
`otel/` is flat, and a subdirectory would be a design change, not an
accident), sorted with `LC_ALL=C sort`, minus:

- anything starting with `.`
- `otel-standalone-*.sh`, `*.tar.gz`, `*.tgz` — pack.sh's own output, if a
  previous run wrote it here
- `aggregate.log`, `aggregate.pid` — runtime state, if someone pointed
  `$CLD_OTEL_DIR` at this directory
- `PROVENANCE` — regenerated on every pack (see below)

Discovery by listing, not by a hard-coded list, is the point: the T2/T3/T4/T5
changes to `otelctl.sh` and a future systemd unit or `doctor` helper ride along
with no edit to `pack.sh`.

**Sanity guard.** Abort with a clear message if any of `otelctl.sh`,
`aggregate.py`, `otel-collector-config.yaml`, `README.md` is missing from the
discovered set — that means pack.sh was moved somewhere it does not belong,
and shipping a partial pipeline is worse than failing.

**Provenance.** Best-effort, and *never* fatal — a VCS probe that fails must
not abort a pack (guard each with `|| true`):

- jj, if `jj root` succeeds from `$HERE`:
  `rev="$(jj log -r @ --no-graph -T 'commit_id.short(12)')"` plus
  `change_id.short(12)`; source `jj`. No dirty flag: jj snapshots the working
  copy on every command, so `@`'s commit id *is* the tree being packed —
  query it before reading the files, so the snapshot happens first.
- git, else, if `git -C "$HERE" rev-parse --short=12 HEAD` succeeds: that
  rev, suffixed `-dirty` when `git -C "$HERE" status --porcelain -- "$HERE"`
  is non-empty; source `git`.
- Neither, but a `PROVENANCE` file is present (i.e. we are re-packing an
  already-extracted copy): carry its `Source rev:` forward verbatim and mark
  the new one `(re-packed from a standalone copy)`. Keeps the chain honest
  when an artifact is passed hand to hand.
- Otherwise `unknown`.

Remote URL, informational only: first field-2 of `jj git remote list`, else
`git remote get-url origin`, else omit. Recorded as provenance, never used to
fetch anything.

**Build.**

1. `staging="$(mktemp -d)"`, `trap 'rm -rf "$staging"' EXIT`; `mkdir
   "$staging/otel"`.
2. `cp -p` each discovered file in (mode preserved, so `otelctl.sh` and
   `aggregate.py` keep their `+x` through tar).
3. Write `$staging/otel/PROVENANCE` (format below).
4. `tar czf "$staging/payload.tar.gz" -C "$staging" otel` — the `otel/`
   prefix inside the archive means the `--tarball` artifact is not a tarbomb,
   and lets the installer extract-then-move rather than rely on
   `--strip-components`, which is not POSIX.
5. `--tarball`: move the payload to the output path and stop.
6. Otherwise: emit the header, then `base64 "$staging/payload.tar.gz" | fold -w
   76 >> "$out"`, then `chmod +x "$out"`.

**Emitting the header — the one likely implementation bug.** The installer
body is full of `$0`, `$@`, `${var}` and `$(…)`. Write it in *two* heredocs:
a short **unquoted** one carrying only the substituted metadata (rev, date,
checksum, manifest, source URL) as shell assignments and comments, then the
static logic in a **quoted** (`<<'EOS'`) heredoc so nothing in it expands at
pack time. Do not try to substitute into the logic block.

**Default output name.** `otel-standalone-<YYYYMMDD>-<rev>.sh`, or
`…-norev.sh` when no revision could be determined; `.tar.gz` under
`--tarball`. Written to `$PWD`, not to `$HERE` — packing must not litter the
directory being packed.

**Summary on success** (stdout, suppressed by `--quiet`): artifact path, byte
size, file count, source rev, and the two lines the recipient needs —
`bash <artifact>` then `cd otel && ./otelctl.sh start`.

**sha256 helper**, used for the manifest and the payload digest, in this
order: `sha256sum` → `shasum -a 256` → `openssl dgst -sha256` (take `$NF`) →
empty string. An empty digest is not fatal on the packing side; it makes the
manifest's checksum column read `unavailable` and the installer skip
verification with a warning.

### 2. The generated installer (produced by `pack.sh`, not committed)

`#!/usr/bin/env bash`, `set -euo pipefail`. The payload requires bash to run
anyway (`otelctl.sh`), so bash is not an added prerequisite.

**Header**, human-readable before any code: what this is, that it is a
verbatim copy of `otel/` from the cld repo, the source rev and remote, the
build timestamp, the payload's sha256, and the manifest — one `sha256  bytes
name` line per file, as comments.

**Interface.**

| flag | behaviour |
|---|---|
| *(none)* | verify, extract to `./otel`, print next steps |
| `--dir PATH` | extract there instead |
| `--force` | proceed even if the target exists and is non-empty |
| `--list` | print provenance + manifest from the header; extract nothing |
| `--check` | verify the payload digest only; exit 0 or 4 |
| `--no-verify` | skip the digest check on extract |
| `-h`, `--help` | usage |

**Extraction.**

1. Refuse if `$0` is not a readable file — `cat x.sh | bash` cannot read its
   own payload. Error message: run it as a file, `bash otel-standalone-*.sh`.
2. Digest the payload region and compare to the header value; mismatch →
   exit 4 with "payload does not match its checksum — the file was likely
   mangled in transit; ask for it again". No sha256 tool → warn, continue.
3. Target exists and is non-empty and no `--force` → exit 3, naming the
   directory and suggesting `--dir`/`--force`.
4. `tmp="$(mktemp -d)"`, `trap … EXIT`; `awk '/^__OTEL_PAYLOAD_BELOW__$/{f=1;next}
   f' "$0" | _b64d | tar xzf - -C "$tmp"`.
5. `mkdir -p "$dir"` then `cp -pR "$tmp/otel/." "$dir/"`. Extract-then-copy so
   a corrupt archive cannot half-overwrite an existing directory, and so no
   `--strip-components` is needed.
6. `chmod +x` on `otelctl.sh`, `aggregate.py`, `pack.sh` — defensive, against
   a tar or umask that dropped the mode.
7. Print: N files extracted to `<dir>`; requires docker and python3; then
   `cd <dir> && ./otelctl.sh start`, and a pointer to `QUICK-START.md`.

**Exit codes:** 0 ok · 1 internal/tool failure · 2 usage · 3 target not empty
· 4 checksum mismatch.

**base64 decode shim.** GNU takes `-d`, macOS/BSD takes `-D`, and both modern
implementations accept `--decode`; older macOS does not. Probe at runtime
rather than sniffing versions — a rejected flag exits non-zero even on empty
input (verified: GNU coreutils 9.7 rejects `-D </dev/null`):

```
_b64d() {
    if base64 --decode </dev/null >/dev/null 2>&1; then base64 --decode
    elif base64 -d </dev/null >/dev/null 2>&1; then base64 -d
    elif base64 -D </dev/null >/dev/null 2>&1; then base64 -D
    else openssl base64 -d
    fi
}
```

Encoding needs no shim: plain `base64` with no flags works everywhere, and
`fold -w 76` makes the wrapping deterministic across GNU (wraps at 76) and BSD
(does not wrap), which also keeps the `openssl base64 -d` fallback happy.

### 3. `PROVENANCE` (generated into every artifact)

Plain text, no format to parse except the `Source rev:` line pack.sh reads
back when re-packing:

```
otel standalone copy -- provenance

Packed:        2026-09-01T13:22:41Z
Source:        cld repo, otel/ (verbatim copy)
Source rev:    jj 3d94e4a458e3 (change zrutnytspmrx)
Source remote: git@github.com:skolazbynek/claude-devcontainer.git
                 (informational only -- may not be reachable from here)
Packed by:     otel/pack.sh v1

These files are a byte-for-byte copy of otel/ in the cld repo at the revision
above; that directory is the single source of truth. To update, get a newer
artifact from someone with repo access -- or re-run ./pack.sh here to pass
this exact copy on. Local edits will be overwritten by the next extraction.

Files (sha256  bytes  name):
  e3b0c442...  4986  otelctl.sh
  …
```

### 4. Documentation

Keep this minimal and append-only — T2, T3 and T5 all have in-flight edits to
`otel/README.md`, and T2/T3 to `QUICK-START.md`.

- **`otel/README.md`**: one new section appended at end of file, "Getting this
  on another machine", with the two on-ramps in this order and each labelled
  by what it is *for*:

  1. **"If you can reach github.com"** — the `curl … | tar xz` one-liner from
     § Hosting, verbatim, plus two short lines: it fetches whatever is on
     `main` (not your local tree), and the `claude-devcontainer-main/` prefix
     follows the ref, so swapping `main` for a tag means changing the prefix
     too. Do not reproduce the GNU-vs-BSD tar discussion in the README — the
     literal member name exists precisely so a reader never has to care which
     tar they have.
  2. **"If you can't, or you want to hand someone a file"** — `./pack.sh`
     produces one self-extracting file (~26 KB); the recipient runs `bash
     otel-standalone-*.sh` and gets an `otel/` directory. `--list` and
     `--check` inspect it without extracting, `--tarball` emits a plain
     archive instead. Note that this packs *your* copy, not `main`, and that
     the artifact carries `pack.sh`, so the recipient can pass it on.

  Curl leads because it is genuinely shorter for the common case. `pack.sh`
  is not a fallback in the apologetic sense — it is the only path that works
  offline, air-gapped, without GitHub access, or from a copy someone was
  handed — and the section should say so in those words.

  A closing line may mention `git sparse-checkout set otel` for people who
  want a live checkout (option C), flagged as a convenience, not an install
  path.
- **`otel/README.md`, one existing line**: the intro's "see `stage_otel()` in
  `../cld/docker.py`" becomes "see `stage_otel()` in cld's `cld/docker.py`".
  The relative path dangles in a standalone copy; naming the repo instead
  reads correctly in both places. This is the *only* edit to an existing line
  anywhere in this change — required, because a test asserts no
  `../cld`-shaped path survives into the artifact (see below).
- **`otel/QUICK-START.md`**: one line at the end, pointing at that README
  section for "got this from a teammate / want to pass it on".
- The top-level `README.md` does not mention `otel/` at all today; adding it
  is out of scope.

### 5. `tests/test_pack_otel.py` (new)

Follows `tests/test_broker_sh.py`'s convention — pytest driving the real
script through `subprocess`, no bespoke shell harness. Mark
`@pytest.mark.integration` (filesystem + real VCS probes).

- **`test_pack_includes_every_file_verbatim`** — the anti-drift test, and the
  mechanical guarantee behind constraint 2. Run `pack.sh --tarball`, extract,
  assert the extracted name set equals the non-excluded contents of `otel/`
  plus `PROVENANCE`, and that every extracted file's bytes are identical to
  the repo copy. A new file added to `otel/` needs no test change; a packer
  that silently drops one fails here.
- **`test_self_extracting_roundtrip`** — pack, run the artifact with `--dir
  <tmp>`, assert the file set, that `otelctl.sh` and `aggregate.py` are
  executable, and that `PROVENANCE` names a source rev.
- **`test_check_detects_tampering`** — mutate one base64 character; `--check`
  exits 4, and a plain extract also refuses and creates nothing.
- **`test_refuses_nonempty_target`** — exits 3 without `--force`, succeeds
  with it.
- **`test_list_extracts_nothing`** — `--list` prints the manifest and creates
  no files.
- **`test_artifact_has_no_cld_paths`** — grep the extracted tree for
  `../cld` (path-shaped references only; the word "cld" legitimately appears
  in prose and in `$CLD_OTEL_DIR`). Enforces constraint 4 mechanically, and
  is what makes the one-line README edit above obligatory rather than
  cosmetic.
- **No test for the `curl` one-liner.** It would need network in CI to assert
  something we do not control (GitHub's tarball layout) about a branch state
  that legitimately drifts. Verified by hand instead, with the commands and
  the tar findings recorded in § Hosting so a future reader can re-run them.
- **`test_repack_from_extracted_copy`** — run `pack.sh` inside an extracted
  copy (no VCS anywhere above it), assert it succeeds and that the new
  `PROVENANCE` carries the original rev forward. This is the "recipient
  re-shares it" path, and the one most likely to break.

## How it is used

**Anyone with network access** — no sender involved, nothing to hand-carry:

```
$ curl -fsSL https://codeload.github.com/skolazbynek/claude-devcontainer/tar.gz/main \
    | tar xz --strip-components=1 claude-devcontainer-main/otel
$ cd otel && ./otelctl.sh start
```

Everything below is the other path: no network, no GitHub, or passing on a
copy you were given.

**Sender** (has a checkout, has never had to think about publishing):

```
$ cd otel && ./pack.sh
otel-standalone-20260901-3d94e4a458e3.sh  (15.1 KB, 6 files, rev jj 3d94e4a458e3)
give it to someone with:  bash otel-standalone-20260901-3d94e4a458e3.sh
```

Then scp it, attach it to the wiki page, drop it in the chat — whatever
channel already exists.

**Recipient** (no cld, no repo access, never heard of either):

```
$ bash otel-standalone-20260901-3d94e4a458e3.sh
extracted 6 files to ./otel  (needs docker + python3)
next:  cd otel && ./otelctl.sh start
       see otel/QUICK-START.md
```

## Why this cannot drift

Constraint 2 is the one worth being explicit about, since "portable copy" and
"no second copy" sound contradictory:

- The artifact is **generated on every run** from whatever is on disk. There
  is no committed copy of the payload, no vendored duplicate, nothing to
  update in lockstep.
- The file list is **discovered**, not enumerated. Nobody has to remember to
  add a file to a manifest.
- A test asserts the extracted tree is **byte-identical** to `otel/`. Any
  transform introduced later — a substitution, a "standalone variant" of a
  doc — fails CI, which is exactly the pressure we want.
- Recipients hold **snapshots with a stated provenance**, not forks. The
  `PROVENANCE` file says which revision they have and that local edits get
  overwritten. A stale copy is visibly stale; it is not a competing source of
  truth.

## Consequences

- One new file in `otel/` (`pack.sh`), one new test module, one README
  section, one edited README line. Nothing in `otelctl.sh`,
  `aggregate.py` or `otel-collector-config.yaml` changes — so this does not
  collide with T2/T3/T4/T5, all four of which touch `otelctl.sh`.
- `otel/` grows from 5 files to 6, and the artifact carries its own packer, so
  distribution is transitive: whoever has a copy can pass it on.
- The recipient's copy is inert with respect to updates: `PROVENANCE` tells
  them which revision they have, and getting a newer one means either the
  `curl` one-liner (if they can reach GitHub) or a fresh artifact from someone
  who can.
- Two documented URLs now name `skolazbynek/claude-devcontainer` explicitly
  (here and in `otel/README.md`). They need updating if the canonical location
  moves; nothing detects that automatically, which is why § Hosting says so
  out loud.
- If `otel/` ever gains a subdirectory, `pack.sh`'s non-recursive discovery
  must be revisited. The guard fails loudly (missing-file abort) only for the
  four essentials, so this is called out here rather than defended in code.

## Out of scope

- **Autostart, retention, rollup, team-wide aggregation, image pinning** —
  review items #2, #5, #6, #7, #8. Unrelated to install.
- **T2/T3/T4/T5** — env-block printing, `otelctl.sh env`, `doctor`, actionable
  startup errors. They land on their own branches and ride along in the
  payload for free.
- **Byte-reproducible artifacts.** GNU `tar --sort=name --mtime=…` has no
  portable BSD equivalent, and the value it buys (diffing two artifacts) is
  already served by the per-file manifest. Two packs of the same tree produce
  different bytes and identical contents; the tests assert contents.
- **Packing a revision other than the working copy.** `pack.sh` packs what is
  checked out. A maintainer wanting an older revision checks it out first.
- **`otelctl.sh`'s `readlink -f`**, which is GNU-only on older macOS and would
  bite a mac recipient. Pre-existing, unrelated to install, and it belongs
  with T5's error-handling work rather than here.
