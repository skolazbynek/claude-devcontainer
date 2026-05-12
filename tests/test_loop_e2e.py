"""Layer 4: cld loop full E2E -- two real iterations through Docker containers."""

import os
import random
import subprocess

import pytest

from tests.conftest import skip_no_agent_image, _HOST_PROJECT_DIR

_HOST_HOME_AT_IMPORT = os.environ.get("CLD_HOST_HOME", "")

pytestmark = [pytest.mark.e2e, pytest.mark.docker]


def _to_host_path(path):
    if _HOST_PROJECT_DIR and path.startswith("/workspace/origin"):
        return _HOST_PROJECT_DIR + path[len("/workspace/origin"):]
    return path


@skip_no_agent_image
class TestLoopFullE2E:
    """Drive run_loop end-to-end: 2 implementer + 2 reviewer containers."""

    def test_two_iterations_then_clean(
        self, e2e_jj_repo, claude_stub_loop_review, monkeypatch,
    ):
        vcs = e2e_jj_repo

        # Seed a tracked file so the impl phase's diff base is non-empty.
        (vcs.repo_root / "src.py").write_text("def f(): return 1\n")
        vcs.commit("seed src.py")

        from cld import agent as agent_mod
        from cld import loop as loop_mod
        from cld.config import Config

        host_stub_dir = _to_host_path(str(claude_stub_loop_review))
        original_build = agent_mod.build_container_args

        def wrapped(repo_root, session_name, cfg, *, interactive=False):
            args = original_build(repo_root, session_name, cfg, interactive=interactive)
            # Strip --rm so 'docker rm -f' cleanup in finally can be authoritative.
            args = [a for a in args if a != "--rm"]
            return args + ["-v", f"{host_stub_dir}:/tmp/bin:ro"]

        monkeypatch.setattr(agent_mod, "build_container_args", wrapped)
        monkeypatch.setattr(loop_mod, "get_backend", lambda *_a, **_kw: vcs)
        monkeypatch.setattr(
            agent_mod, "find_repo_context", lambda *_a, **_kw: (vcs.repo_root, ""),
        )
        monkeypatch.chdir(vcs.repo_root)

        cfg = Config(
            host_project_dir=_HOST_PROJECT_DIR,
            host_home=_HOST_HOME_AT_IMPORT,
            agent_timeout=180,
            poll_interval=2,
        )

        name = f"e2e{random.randint(10000, 99999)}"
        loop_branch = f"loop_{name}"

        # Track container IDs so we can clean up regardless of test outcome.
        spawned = []
        real_launch_agent = agent_mod.launch_agent

        def tracking_launch_agent(*args, **kwargs):
            result = real_launch_agent(*args, **kwargs)
            spawned.append(result["container_id"])
            return result

        monkeypatch.setattr(loop_mod, "launch_agent", tracking_launch_agent)

        try:
            loop_mod.run_loop(
                cfg,
                task_file=None,
                inline_prompt="Implement something",
                name=name,
                max_iterations=3,
            )

            assert vcs.resolve_revision(loop_branch)
            review2 = vcs.file_show(loop_branch, "CODE_REVIEW_iter2.md")
            assert review2 is not None, "CODE_REVIEW_iter2.md missing on loop branch"
            severity_iter2 = (
                review2.count("\n### ", review2.find("## Critical"), review2.find("## Major"))
                + review2.count("\n### ", review2.find("## Major"), review2.find("## Minor"))
            )
            assert severity_iter2 == 0, f"iter2 should be clean, got:\n{review2}"

            review1 = vcs.file_show(loop_branch, "CODE_REVIEW_iter1.md")
            assert review1 is not None and "Real bug" in review1

            leftover = list(vcs.repo_root.glob(".cld/loop-*"))
            assert leftover == [], f"cleanup left files: {leftover}"

            names = set()
            for line in vcs.list_branches().splitlines():
                token = line.strip().lstrip("* ").split(":")[0].split()[0]
                if "/" not in token:
                    names.add(token)
            for suffix in ("_impl1", "_impl2", "_review1", "_review2"):
                assert f"{loop_branch}{suffix}" not in names, \
                    f"agent branch {loop_branch}{suffix} not cleaned up"
        finally:
            for cid in spawned:
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
