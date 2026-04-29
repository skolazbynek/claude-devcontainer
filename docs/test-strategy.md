# Test Suite Summary

## Codebase Under Test

`cld` — Python CLI (typer) for running Claude Code in Docker containers with
jujutsu (jj) workspace isolation. Modules:

- `cld/cli.py` — typer app (`agent`, `devcontainer`, `review`, `loop`)
- `cld/docker.py` — container arg building, image management, path translation
- `cld/agent.py` — `launch_agent`, `launch_review`
- `cld/loop.py` — automated implement-review loop
- `cld/mcp/orchestrator.py` — MCP server exposing agent/jj tools

Techstack: Python 3.11+, Poetry, typer 0.24, mcp 1.x, pytest 8.x. Heavy
subprocess orchestration of `docker` and `jj`; several `os.execvp` paths that
replace the current process.

## Design

**Framework:** pytest (already in dev deps). `typer.testing.CliRunner` for CLI
validation.

**Scope:** unit tests of pure and filesystem-only logic. No Docker, no jj, no
claude required to run the suite. Subprocess orchestration and `os.execvp`
paths are intentionally out of scope — covering them would require a real
Docker daemon, built images, and a real jj repository, and would turn the
suite into integration tests that are slow and brittle.

**Layout:**

```
tests/
  __init__.py
  conftest.py           autouse fixture strips leaky env vars
                        (WORKSPACE_ORIGIN, CLD_HOST_PROJECT_DIR, CLD_HOST_HOME, CLD_MYSQL_CONFIG)
  test_docker.py        build_session_name, find_jj_root, load_dotenv,
                        _to_host_path, mount_home_path
  test_agent.py         _build_task_file (file-only, inline-only, both, neither)
  test_loop.py          _parse_review_severity, _format_duration
  test_orchestrator.py  _parse_description, _is_host_visible
  test_cli.py           agent/loop argument validation via CliRunner
```

## How to Run

```bash
poetry install                 # install dev deps once
poetry run pytest              # run full suite
poetry run pytest -v           # verbose
poetry run pytest tests/test_docker.py::TestFindJjRoot  # target a class
```

No environment setup required. Tests use `tmp_path` for filesystem and
`monkeypatch` for env vars, so no host state is mutated.

## Results

47 tests, all pass, run in ~1s.

## Principles Applied

- **Pure logic first** — every tested function is deterministic or purely
  filesystem-driven. No subprocess mocking.
- **Minimal fixtures** — a single autouse `clean_env` fixture; tests use
  pytest built-ins (`tmp_path`, `monkeypatch`) otherwise.
- **AAA structure** — arrange / act / assert kept terse; no setup methods.
- **Parametrize for input variation** — `_format_duration` and
  `_is_host_visible` use `@pytest.mark.parametrize` with explicit `ids`.
- **One class per function** — grouping by the function under test mirrors
  the `TestX` convention in `CLAUDE.md` style guide.
- **No over-specification** — tests assert on observable behavior (return
  values, exit codes, file contents, key messages), not on internals.

## What Is Not Covered

Deliberate omissions — adding these later is fine if requirements appear:

- `ensure_image`, `require_docker`, `run_container` — subprocess to Docker
- `launch_agent`, `launch_review` — end-to-end container launch
- `run_loop` orchestration — `time.sleep` + `docker ps` polling
- All `mcp.orchestrator` tools that shell out to `jj` or `docker`
- `build_container_args` — doable but fragile (patches on `Path.is_dir`,
  `/etc/ssl/certs`, `/var/run/docker.sock`). Skipped pending a real need.

## Iteration Count

Review iterations performed: 1. No major issues found; no REVIEW_SUMMARY.md
produced because nothing surfaced beyond the design choices already documented
here.
