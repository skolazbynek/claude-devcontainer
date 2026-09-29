# runtests

A standalone, single-job container: run a project's **pytest** suite against a
specific **jujutsu (jj)** revision in an isolated workspace, without disturbing
the origin repo's current change (`@`).

It has no dependency on any other tooling — it knows only a jj store, a revision,
a secrets file, and pytest arguments.

## How it works

The repo whose jj store holds the change is bind-mounted at `/repo`. The
entrypoint runs:

```
jj workspace add --name runtests-<id> -r "$REVISION" "$HOME/rt-workspace"
```

which creates an **independent** working copy under `$HOME` sharing only
`/repo/.jj/repo/store`. jj never moves the origin's default-workspace `@` or its
bookmarks, so the change under test is materialized without touching whatever the
user is working on. The workspace is `forget`-ten on exit (even on crash). It
lives under `$HOME` (not `/`) so it is writable when the container runs with
`--user`.

Secrets (DB hosts/passwords etc.) are read from a mounted env file and sourced
into pytest's environment only.

## Interface

| Input | Via | Default | Meaning |
|---|---|---|---|
| jj store | `-v <repo>:/repo` (rw) | — | repo whose store holds the change |
| `REVISION` | `-e` | `@` | revset to test (any jj revset) |
| secrets | `-v <file>:/secrets/.env:ro` | — | env file, sourced into pytest env |
| `SECRETS_FILE` | `-e` | `/secrets/.env` | override secrets path |
| `PROJECT_SUBDIR` | `-e` | `.` | dir holding `pyproject.toml` |
| `POETRY_INSTALL_ARGS` | `-e` | `--all-extras --all-groups` | `poetry install` flags (installs test deps however declared); set e.g. `--no-root` or `--with dev` to narrow |
| `PYTEST_ADDOPTS` | `-e` | `--tb=short --disable-warnings -q --maxfail=30` | default pytest opts; explicit argv still overrides them |
| `OUTPUT_MAX_BYTES` | `-e` | `65536` | cap on returned output; only the last N bytes (the summary) are emitted |
| venv cache | `-v <dir>:/cache` (rw) | — | host cache holding `venvs/`, `poetry/`, `locks/`; see [Venv cache](#venv-cache) |
| `VENV_CACHE_IMAGE_ID` | `-e` | — | image id, part of the cache key; the cache is skipped without it |
| `VENV_CACHE` | `-e` | `on` | `on`, `off`, or `rebuild` (rebuild this key's venv) |
| `VENV_CACHE_KEEP` | `-e` | `10` | most recently used venvs kept in the cache |
| pytest args | container **argv** | none | passed straight to `pytest`, override `PYTEST_ADDOPTS` |

Requires **Poetry 2.x** (the image ships 2.4.1): it reads PEP 621 `[project]`
metadata, which Poetry 1.x does not.

The container exits with pytest's exit code.

## Build

```
./build.sh                     # -> runtests:latest
RUNTESTS_IMAGE=runtests:dev ./build.sh
```

`build.sh` passes `../scripts` as the extra build context `scripts` (for
`venv-cache.sh`), which needs BuildKit (the default builder since Docker 23).

## Venv cache

Before `poetry install`, `venv-cache.sh` seeds the venv at `/opt/venv` from a
host cache mounted at `/cache` (the broker mounts `~/.cld/cache`):

- The key hashes `pyproject.toml`, `poetry.lock`, `POETRY_INSTALL_ARGS`, the
  Python and Poetry versions and `VENV_CACHE_IMAGE_ID`.
- **Hit:** `venvs/<key>` is copied to `/opt/venv`. A venv only works at the
  absolute path it was built at, so every venv is built and used at `/opt/venv`.
- **Miss:** the dependencies (not the project) are installed into `/opt/venv`,
  which is then published to `venvs/<key>`. One build runs at a time across all
  keys; the least recently used venvs beyond `VENV_CACHE_KEEP` are evicted.
- `poetry install` still runs afterwards: it installs the project itself and
  repairs any drift, taking seconds when the venv already matches.
- Any cache error falls back to a normal install. Without the mount, a
  lock file or the image id, the cache is skipped.

Poetry's download cache lives at `/cache/poetry`, so a miss downloads only new
packages.

## Run

```
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /path/to/repo:/repo \
  -v /path/to/repo/.env:/secrets/.env:ro \
  -e REVISION=@ \
  runtests:latest -k login -x tests/unit
```

Run the whole suite by passing no pytest args. `--user "$(id -u):$(id -g)"`
keeps any jj store writes owned by the host user; `HOME=/tmp` because that uid
may not exist in the image's `/etc/passwd`. For the same reason the entrypoint
defaults `USER`/`LOGNAME` to `runtests` if unset -- tools that call
`getpwuid()` on a passwd-less uid (e.g. Python's `getpass.getuser()`) would
otherwise fail.

## Notes

- The image bakes internal CA certs into the system trust store so TLS to the
  databases and PyPI works without a host mount.
- Non-root `pyproject.toml`? Set `PROJECT_SUBDIR`.
- Reading the revision while another process (e.g. an interactive session)
  writes the same store concurrently is supported by jj's op-log; a
  store-reading caller may lag the latest edit by a snapshot interval.
