"""Tests for pure helpers in cld.docker."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cld.config import Config, _load_dotenv
from cld.docker import (
    _INSPECT_FORMAT,
    MAILBOX_MOUNT,
    TaskAgentSpec,
    agent_container_name,
    allocate_task_agent_name,
    assert_task_agent_capacity,
    build_container_args,
    build_session_name,
    docker_task_agent_list,
    find_repo_root,
    in_master_container,
    parse_peers_env,
    resolve_master_target,
    resolve_anchor_checked,
    stage_home_ro,
    stage_broker,
    task_agent_container_name,
    to_host_path,
)


def _ps(names: str, rc: int = 0):
    return type("R", (), {"returncode": rc, "stdout": names, "stderr": ""})()


class TestBuildSessionName:
    def test_explicit_suffix(self):
        assert build_session_name("agent", "feature") == "agent_feature"

    def test_auto_suffix_is_hex(self):
        prefix, _, suffix = build_session_name("cld").partition("_")
        assert prefix == "cld"
        assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)

    def test_auto_suffix_varies(self):
        # secrets.token_hex(3) -> 6 hex chars; collisions in 20 picks are astronomical
        assert len({build_session_name("x") for _ in range(20)}) > 1


class TestFindJjRoot:
    def test_finds_in_start_dir(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        assert find_repo_root(tmp_path) == tmp_path

    def test_walks_up_from_nested(self, tmp_path):
        (tmp_path / ".jj").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path

    def test_workspace_origin_env_takes_priority(self, tmp_path, monkeypatch):
        origin = tmp_path / "origin"
        (origin / ".jj").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / ".jj").mkdir(parents=True)
        monkeypatch.setenv("WORKSPACE_ORIGIN", str(origin))
        assert find_repo_root(elsewhere) == origin

    def test_exits_when_not_found(self, tmp_path):
        with pytest.raises(SystemExit):
            find_repo_root(tmp_path)


class TestAgentContainerName:
    def test_no_sha_disambiguator(self, tmp_path):
        repo = tmp_path / "myrepo"
        assert agent_container_name(repo) == "cld_agent_myrepo"

    def test_deterministic(self, tmp_path):
        repo = tmp_path / "myrepo"
        assert agent_container_name(repo) == agent_container_name(repo)


class TestLoadDotenv:
    def test_loads_key_value(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        _load_dotenv(env)
        assert os.environ["FOO"] == "bar"

    def test_ignores_comments_and_blanks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BAZ", raising=False)
        env = tmp_path / ".env"
        env.write_text("# comment\n\n   \nBAZ=qux\n")
        _load_dotenv(env)
        assert os.environ["BAZ"] == "qux"

    def test_strips_whitespace(self, tmp_path, monkeypatch):
        monkeypatch.delenv("K", raising=False)
        env = tmp_path / ".env"
        env.write_text("  K  =  v  \n")
        _load_dotenv(env)
        assert os.environ["K"] == "v"

    def test_missing_file_is_noop(self, tmp_path):
        _load_dotenv(tmp_path / "nonexistent")


class TestToHostPath:
    def test_workspace_current_not_translated(self):
        # /workspace/current lives inside the container's ephemeral filesystem
        # and has no host equivalent; to_host_path leaves it alone.
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/workspace/current/file.py", cfg) == "/workspace/current/file.py"

    def test_translates_workspace_origin(self):
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/workspace/origin/.jj", cfg) == "/host/proj/.jj"

    def test_translates_home(self):
        from cld.docker import CONTAINER_HOME
        cfg = Config(host_home="/home/host")
        assert to_host_path(f"{CONTAINER_HOME}/.claude", cfg) == "/home/host/.claude"

    def test_no_env_no_translation(self):
        assert to_host_path("/anywhere/else", Config()) == "/anywhere/else"

    def test_non_matching_path_untouched(self):
        cfg = Config(host_project_dir="/host/proj")
        assert to_host_path("/unrelated/path", cfg) == "/unrelated/path"


class TestStageHomeRo:
    def test_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert stage_home_ro(".missing", Config()) == []

    def test_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".gitconfig").write_text("x")
        args = stage_home_ro(".gitconfig", Config())
        assert args[0] == "-v"
        assert args[1].endswith(":/tmp/host-config/.gitconfig:ro")
        assert str(tmp_path / ".gitconfig") in args[1]

    def test_existing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".config" / "anthropic").mkdir(parents=True)
        args = stage_home_ro(".config/anthropic", Config())
        assert args[0] == "-v"
        assert args[1].endswith(":/tmp/host-config/.config/anthropic:ro")

    def test_nested_rel_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".local" / "state" / "nvim").mkdir(parents=True)
        args = stage_home_ro(".local/state/nvim", Config())
        assert args[1].endswith(":/tmp/host-config/.local/state/nvim:ro")

    def test_to_host_path_translation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".bashrc").write_text("x")
        cfg = Config(host_home="/host/home")
        # tmp_path stands in for $CONTAINER_HOME via HOME env; to_host_path
        # only rewrites paths starting with CONTAINER_HOME, which tmp_path does
        # not, so the host-translated string is just the resolved tmp path.
        args = stage_home_ro(".bashrc", cfg)
        assert args[1].startswith(str(tmp_path.resolve()) + "/.bashrc:")


class TestResolveMasterTarget:
    def test_errors_when_not_in_master(self, tmp_path):
        # clean_env fixture already unsets HUB_MODE
        with pytest.raises(RuntimeError, match="not running inside a cld master"):
            resolve_master_target(tmp_path, Config())

    def test_own_repo_via_workspace_origin(self, monkeypatch):
        monkeypatch.setenv("HUB_MODE", "1")
        cfg = Config(host_project_dir="/host/side/cld")
        # /workspace/current is master's ephemeral workspace path. Path.resolve
        # is lenient about non-existent paths so this works even on the host.
        from pathlib import Path
        assert resolve_master_target(Path("/workspace/current"), cfg) == "/host/side/cld"
        assert resolve_master_target(Path("/workspace/origin/sub"), cfg) == "/host/side/cld"

    def test_own_repo_errors_without_host_project_dir(self, monkeypatch):
        monkeypatch.setenv("HUB_MODE", "1")
        from pathlib import Path
        with pytest.raises(RuntimeError, match="CLD_HOST_PROJECT_DIR is unset"):
            resolve_master_target(Path("/workspace/current"), Config())

    def test_matches_master_targets_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUB_MODE", "1")
        target = tmp_path / "projects" / "foo"
        (target / "subdir").mkdir(parents=True)
        monkeypatch.setenv("MASTER_TARGETS", f"{target}:{tmp_path}/other")
        assert resolve_master_target(target, Config()) == str(target)
        assert resolve_master_target(target / "subdir", Config()) == str(target)

    def test_matches_via_container_mirror(self, monkeypatch):
        # Placeholder dirs live at the container mirror ($HOME/...) of a host
        # target; resolve translates cwd back to the host path before matching.
        monkeypatch.setenv("HUB_MODE", "1")
        host_target = "/home/host/projects/foo"
        monkeypatch.setenv("MASTER_TARGETS", host_target)
        cfg = Config(host_home="/home/host")
        from pathlib import Path
        from cld.docker import CONTAINER_HOME
        mirror = Path(f"{CONTAINER_HOME}/projects/foo")
        assert resolve_master_target(mirror, cfg) == host_target
        assert resolve_master_target(mirror / "subdir", cfg) == host_target

    def test_unknown_cwd_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUB_MODE", "1")
        monkeypatch.setenv("MASTER_TARGETS", "")
        elsewhere = tmp_path / "unrelated"
        elsewhere.mkdir()
        with pytest.raises(RuntimeError, match="not a registered target"):
            resolve_master_target(elsewhere, Config())


class TestEnsureImageNested:
    def test_raises_inside_master_no_daemon(self, monkeypatch):
        # Inside master there is no docker daemon (socket removed) and container
        # launches are delegated to the host broker, so ensure_image must never
        # be reached; if it is, it fails clearly rather than touching docker.
        monkeypatch.setenv("HUB_MODE", "1")
        import cld.docker as docker_mod
        from pathlib import Path
        from unittest.mock import patch
        with patch.object(docker_mod.subprocess, "run") as run_mock:
            with pytest.raises(RuntimeError, match="cannot be ensured from inside a master"):
                docker_mod.ensure_image(
                    "missing:img",
                    Path("/opt/cld/imgs/x/Dockerfile"),
                    Path("/opt/cld"),
                )
        run_mock.assert_not_called()  # never touches the (absent) daemon


class TestInMasterContainer:
    def test_true_when_hub_mode_set(self, monkeypatch):
        monkeypatch.setenv("HUB_MODE", "1")
        assert in_master_container() is True

    def test_false_when_only_master_mode_set(self, monkeypatch):
        # MASTER_MODE alone (without HUB_MODE) should not happen in practice --
        # build_container_args always sets both for master -- but this pins the
        # actual check to HUB_MODE, not MASTER_MODE.
        monkeypatch.setenv("MASTER_MODE", "1")
        assert in_master_container() is False

    def test_false_when_unset(self):
        assert in_master_container() is False


class TestStageBroker:
    def test_no_key_is_noop(self):
        assert stage_broker(Config()) == []

    def test_missing_key_returns_empty(self, tmp_path):
        cfg = Config(broker_key=str(tmp_path / "nope"))
        assert stage_broker(cfg) == []

    def test_key_only_wires_gateway_and_endpoint(self, tmp_path):
        key = tmp_path / "broker_key"
        key.write_text("k")
        cfg = Config(broker_key=str(key), broker_endpoint="host.docker.internal:2222")
        args = stage_broker(cfg)
        assert args[:2] == ["--add-host", "host.docker.internal:host-gateway"]
        assert "-v" in args and f"{key}:/run/secrets/broker-key:ro" in args
        assert "-e" in args and "CLD_BROKER_ENDPOINT=host.docker.internal:2222" in args
        # No known_hosts mount when it isn't configured.
        assert not any("broker-known-hosts" in a for a in args)

    def test_known_hosts_mounted_when_present(self, tmp_path):
        key = tmp_path / "broker_key"; key.write_text("k")
        known = tmp_path / "known_hosts"; known.write_text("h")
        cfg = Config(broker_key=str(key), broker_known_hosts=str(known))
        args = stage_broker(cfg)
        assert f"{known}:/run/secrets/broker-known-hosts:ro" in args

    def test_endpoint_override_passed_through(self, tmp_path):
        key = tmp_path / "broker_key"; key.write_text("k")
        cfg = Config(broker_key=str(key), broker_endpoint="me@1.2.3.4:2200")
        args = stage_broker(cfg)
        assert "CLD_BROKER_ENDPOINT=me@1.2.3.4:2200" in args


class TestDockerKindList:
    """Enumeration is label-driven; `docker inspect` reads .Config.Labels."""

    def test_inspect_format_uses_config_labels(self):
        # Regression: a top-level .Labels field does not exist on a container and
        # fails the whole template, which would silently drop every container.
        assert ".Config.Labels" in _INSPECT_FORMAT
        assert "index .Labels" not in _INSPECT_FORMAT

    def test_records_carry_kind_parent_task(self):
        inspect = _ps("/home/u/repoA|cld_agent_repoA_add-oauth|task-agent|cld_master_repoA_ab12|add-oauth||\n")
        with patch("cld.docker.subprocess.run", side_effect=[_ps("cld_agent_repoA_add-oauth\n"), inspect]):
            assert docker_task_agent_list() == [{
                "name": "cld_agent_repoA_add-oauth",
                "repo_root": "/home/u/repoA",
                "session": "cld_agent_repoA_add-oauth",
                "kind": "task-agent",
                "parent": "cld_master_repoA_ab12",
                "task": "add-oauth",
                "anchor": "",
                "anchor_mode": "",
            }]

    def test_missing_labels_become_empty(self):
        with patch("cld.docker.subprocess.run", side_effect=[_ps("c1\n"), _ps("/repo|c1|agent|||\n")]):
            rec = docker_task_agent_list()[0]
        assert (rec["parent"], rec["task"]) == ("", "")

    def test_running_only_filters_docker_side(self):
        calls = []

        def spy(cmd, **_kwargs):
            calls.append(cmd)
            return _ps("")

        with patch("cld.docker.subprocess.run", side_effect=spy):
            docker_task_agent_list(running_only=True)
        assert "status=running" in calls[0]
        assert "label=org.cld.kind=task-agent" in calls[0]

    def test_no_status_filter_by_default(self):
        calls = []

        def spy(cmd, **_kwargs):
            calls.append(cmd)
            return _ps("")

        with patch("cld.docker.subprocess.run", side_effect=spy):
            docker_task_agent_list()
        assert "status=running" not in calls[0]

    def test_ps_failure_returns_empty(self):
        with patch("cld.docker.subprocess.run", return_value=_ps("", rc=1)):
            assert docker_task_agent_list() == []

    def test_inspect_failure_skips_container(self):
        with patch("cld.docker.subprocess.run", side_effect=[_ps("c1\nc2\n"), _ps("", rc=1), _ps("/r|c2|agent||\n")]):
            assert [c["name"] for c in docker_task_agent_list()] == ["c2"]


class TestTaskAgentContainerName:
    def test_repo_and_slug(self, tmp_path):
        assert task_agent_container_name(tmp_path / "myrepo", "add-oauth") == "cld_agent_myrepo_add-oauth"

    def test_suffix_appended_from_two(self, tmp_path):
        repo = tmp_path / "myrepo"
        assert task_agent_container_name(repo, "x", 1) == "cld_agent_myrepo_x"
        assert task_agent_container_name(repo, "x", 2) == "cld_agent_myrepo_x-2"

    @pytest.mark.parametrize("slug", ["Add-OAuth", "add oauth", "-lead", "add_oauth", "", "add/oauth"])
    def test_invalid_slug_rejected(self, tmp_path, slug):
        with pytest.raises(ValueError, match="invalid task slug"):
            task_agent_container_name(tmp_path / "r", slug)


class TestAllocateTaskAgentName:
    def test_returns_base_when_free(self, tmp_path):
        with patch("cld.docker._docker_status", return_value="absent"):
            assert allocate_task_agent_name(tmp_path / "r", "task") == "cld_agent_r_task"

    def test_skips_taken_names(self, tmp_path):
        with patch("cld.docker._docker_status", side_effect=["running", "stopped", "absent"]):
            assert allocate_task_agent_name(tmp_path / "r", "task") == "cld_agent_r_task-3"


class TestTaskAgentSpec:
    def test_peers_env_encoding(self):
        spec = TaskAgentSpec(slug="t", peers={"cld_agent_r_b": 5, "cld_agent_r_a": 15})
        assert spec.peers_env() == "cld_agent_r_a:15,cld_agent_r_b:5"

    def test_peers_env_empty(self):
        assert TaskAgentSpec(slug="t").peers_env() == ""


class TestBuildContainerArgsTaskAgent:
    """Task-agent role wiring. No daemon needed -- build_container_args only
    inspects the filesystem and cfg."""

    def _args(self, tmp_path, **kwargs):
        spec = TaskAgentSpec(
            slug="add-oauth",
            parent_master="cld_master_repoA_ab12",
            deliverable_branch="add-oauth-login",
            peers={"cld_agent_repoA_contract": 15},
            **kwargs,
        )
        return build_container_args(
            tmp_path, "cld_agent_repoA_add-oauth", Config(mailbox_root=str(tmp_path / "mb")),
            task_agent=spec,
        )

    def test_labels_and_name(self, tmp_path):
        args = self._args(tmp_path)
        assert "--name" in args and "cld_agent_repoA_add-oauth" in args
        assert "org.cld.kind=task-agent" in args
        assert "org.cld.task=add-oauth" in args
        assert "org.cld.parent-master=cld_master_repoA_ab12" in args
        assert f"org.cld.session=cld_agent_repoA_add-oauth" in args

    def test_agent_mode_plus_task_modifier(self, tmp_path):
        args = self._args(tmp_path)
        assert "AGENT_MODE=1" in args
        assert "TASK_AGENT_MODE=1" in args
        assert "MASTER_MODE=1" not in args
        assert "HUB_MODE=1" not in args

    def test_spawn_facts_in_env(self, tmp_path):
        args = self._args(tmp_path)
        assert "AGENT_DELIVERABLE_BRANCH=add-oauth-login" in args
        assert "AGENT_PEERS=cld_agent_repoA_contract:15" in args
        assert "AGENT_PARENT_MASTER=cld_master_repoA_ab12" in args
        assert "AGENT_TASK_SLUG=add-oauth" in args

    def test_turn_cap_propagated(self, tmp_path):
        """In-container Config.from_env() sees no host TOML, so the cap has to be passed."""
        args = build_container_args(
            tmp_path, "cld_agent_r_t",
            Config(mailbox_root=str(tmp_path / "mb"), agent_max_turns=200),
            task_agent=TaskAgentSpec(slug="t"),
        )
        assert "CLD_AGENT_MAX_TURNS=200" in args

    def test_budget_fallbacks_propagated(self, tmp_path):
        spec = TaskAgentSpec(slug="t")
        args = build_container_args(
            tmp_path, "cld_agent_r_t",
            Config(mailbox_root=str(tmp_path / "mb"), peer_absolute_limit=3, root_ask_limit=5),
            task_agent=spec,
        )
        assert "CLD_PEER_ABSOLUTE_LIMIT=3" in args
        assert "CLD_ROOT_ASK_LIMIT=5" in args

    def test_persistent_not_ephemeral(self, tmp_path):
        assert "--rm" not in self._args(tmp_path)

    def test_mailbox_mounted(self, tmp_path):
        args = self._args(tmp_path)
        assert any(a.endswith(f":{MAILBOX_MOUNT}:rw") for a in args)

    def test_broker_key_wired(self, tmp_path):
        """Task-agents get the broker too now -- gated by policy in their
        persona prompt (must ask master first), not by wiring."""
        key = tmp_path / "broker_key"
        key.write_text("k")
        args = build_container_args(
            tmp_path, "cld_agent_r_t",
            Config(mailbox_root=str(tmp_path / "mb"), broker_key=str(key)),
            task_agent=TaskAgentSpec(slug="t"),
        )
        assert any("broker-key" in a for a in args)

    @pytest.mark.parametrize("kwargs", [
        {"master": True, "agent": True},
        {"master": True, "task_agent": TaskAgentSpec(slug="t")},
        {"agent": True, "task_agent": TaskAgentSpec(slug="t")},
    ])
    def test_roles_mutually_exclusive(self, tmp_path, kwargs):
        with pytest.raises(ValueError, match="mutually exclusive"):
            build_container_args(tmp_path, "s", Config(), **kwargs)


class TestBuildContainerArgsBrokerWiring:
    """Broker key reaches every persistent role -- master, agent, task-agent --
    not just master. Access-time policy (master authorization) lives in the
    agent/task-agent persona prompts, not in this wiring."""

    def _cfg(self, tmp_path):
        key = tmp_path / "broker_key"
        key.write_text("k")
        return Config(mailbox_root=str(tmp_path / "mb"), broker_key=str(key))

    def test_master_role_gets_broker(self, tmp_path):
        args = build_container_args(tmp_path, "cld_master_r", self._cfg(tmp_path), master=True)
        assert any("broker-key" in a for a in args)

    def test_agent_role_gets_broker(self, tmp_path):
        args = build_container_args(tmp_path, "cld_agent_r", self._cfg(tmp_path), agent=True)
        assert any("broker-key" in a for a in args)

    def test_task_agent_role_gets_broker(self, tmp_path):
        args = build_container_args(
            tmp_path, "cld_agent_r_t", self._cfg(tmp_path), task_agent=TaskAgentSpec(slug="t"),
        )
        assert any("broker-key" in a for a in args)

    def test_run_role_gets_no_broker(self, tmp_path):
        """`cld run` (no role, non-interactive) never gets the broker -- it's a
        one-shot, unattended container, unlike the bare interactive devcontainer."""
        args = build_container_args(tmp_path, "run_x", self._cfg(tmp_path))
        assert not any("broker-key" in a for a in args)

    def test_bare_interactive_devcontainer_gets_broker(self, tmp_path):
        """Bare `cld` (interactive, no persistent role) is an ephemeral, single-user
        `cld master` in every capability that matters -- it gets the broker too."""
        args = build_container_args(tmp_path, "cld_x", self._cfg(tmp_path), interactive=True)
        assert any("broker-key" in a for a in args)
        assert "--name" in args and "cld_x" in args
        assert any(a == "org.cld.kind=devcontainer" for a in args)

    def test_run_devcontainer_not_named(self, tmp_path):
        """Only the interactive bare devcontainer gets a name/labels; `cld run`
        stays anonymous like before."""
        args = build_container_args(tmp_path, "run_x", self._cfg(tmp_path))
        assert "--name" not in args


def _tasks(*specs):
    """Fake docker_task_agent_list records: (name, parent, repo_root, task)."""
    return [
        {"name": n, "parent": p, "repo_root": r, "task": t, "session": n, "kind": "task-agent"}
        for n, p, r, t in specs
    ]


class TestAssertTaskAgentCapacity:
    def test_under_cap_passes(self):
        with patch("cld.docker.docker_task_agent_list", return_value=_tasks(("a", "m1", "/r", "t"))):
            assert_task_agent_capacity(Config(max_task_agents=2), "m1")

    def test_at_cap_raises_naming_agents(self):
        running = _tasks(("a", "m1", "/r", "task-a"), ("b", "m1", "/r", "task-b"))
        with patch("cld.docker.docker_task_agent_list", return_value=running):
            with pytest.raises(RuntimeError, match="task-agent cap reached") as e:
                assert_task_agent_capacity(Config(max_task_agents=2), "m1")
        assert "task-a" in str(e.value) and "b (task-b)" in str(e.value)

    def test_other_masters_do_not_count(self):
        running = _tasks(("a", "m2", "/r", "t"), ("b", "m2", "/r", "t"))
        with patch("cld.docker.docker_task_agent_list", return_value=running):
            assert_task_agent_capacity(Config(max_task_agents=2), "m1")

    def test_host_launched_group_counted_on_its_own(self):
        running = _tasks(("a", "", "/r", "t"), ("b", "", "/r", "t"))
        with patch("cld.docker.docker_task_agent_list", return_value=running) as m:
            with pytest.raises(RuntimeError, match="host-launched agents"):
                assert_task_agent_capacity(Config(max_task_agents=2), "")
        assert m.call_args.kwargs == {"running_only": True}


def _anchors(*specs, kind="agent"):
    """Fake docker_anchor_list records: (name, repo_root, anchor, anchor_mode)."""
    return [
        {"name": n, "repo_root": r, "anchor": a, "anchor_mode": m, "kind": kind}
        for n, r, a, m in specs
    ]


class TestResolveAnchorChecked:
    """Live-anchor overlap refusal against a real jj repo; the container list is faked."""

    def _commits(self, jj_repo):
        base = jj_repo.resolve_revision("@-")
        jj_repo.run(["new", base])
        (jj_repo.repo_root / "live.txt").write_text("live\n")
        jj_repo.run(["commit", "-m", "live agent anchor"])
        live_anchor = jj_repo.resolve_revision("@-")
        (jj_repo.repo_root / "more.txt").write_text("more\n")
        jj_repo.run(["commit", "-m", "live agent work"])
        inside = jj_repo.resolve_revision("@-")
        return base, live_anchor, inside

    def _fleet(self, tmp_path, jj_repo, anchor, name="cld_agent_r_live", anchor_mode="isolated", kind="agent"):
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        records = _anchors((name, str(jj_repo.repo_root), anchor, anchor_mode), kind=kind)
        return cfg, records

    def test_shared_base_passes_with_live_sibling(self, tmp_path, jj_repo):
        base, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        with patch("cld.docker.docker_anchor_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base) == base

    def test_inside_live_stack_refused_naming_owner(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        with patch("cld.docker.docker_anchor_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach") as e:
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)
        assert "cld_agent_r_live" in str(e.value)

    def test_equal_to_live_anchor_refused(self, tmp_path, jj_repo):
        _, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor)
        with patch("cld.docker.docker_anchor_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, live_anchor)

    def test_other_repo_agents_ignored(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, _records = self._fleet(tmp_path, jj_repo, live_anchor)
        elsewhere = _anchors(("cld_agent_r_live", "/some/other/repo", live_anchor, "isolated"))
        with patch("cld.docker.docker_anchor_list", return_value=elsewhere):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_missing_anchor_label_ignored(self, tmp_path, jj_repo):
        _, _, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, "")
        with patch("cld.docker.docker_anchor_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_no_live_agents_passes(self, tmp_path, jj_repo):
        _, _, inside = self._commits(jj_repo)
        with patch("cld.docker.docker_anchor_list", return_value=[]):
            cfg = Config(mailbox_root=str(tmp_path / "mb"))
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_stale_anchor_label_warns_and_allows(self, tmp_path, jj_repo, caplog):
        # An anchor label no longer resolvable in the store is our own bookkeeping
        # failing, not a real hazard -- warn, don't block.
        _, _, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, "dead" * 10)
        with caplog.at_level("WARNING"), patch("cld.docker.docker_anchor_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside
        assert "live-anchor overlap check" in caplog.text

    def test_git_backend_skips_check(self, tmp_path, git_repo):
        head = git_repo.resolve_revision("HEAD")
        cfg = Config(mailbox_root=str(tmp_path / "mb"))
        with patch("cld.docker.docker_anchor_list") as m:
            assert resolve_anchor_checked(cfg, git_repo.repo_root, head) == head
        m.assert_not_called()

    def test_isolated_sibling_of_live_base_passes(self, tmp_path, jj_repo):
        """Two isolated agents off the same base don't collide (default mode)."""
        base, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, anchor_mode="isolated")
        with patch("cld.docker.docker_anchor_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base, "isolated") == base

    def test_shared_refused_when_live_occupant_inside_tree(self, tmp_path, jj_repo):
        """A shared anchor claiming a tree with a live occupant already inside it is refused."""
        base, live_anchor, _ = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, anchor_mode="isolated")
        with patch("cld.docker.docker_anchor_list", return_value=records):
            with pytest.raises(RuntimeError, match="already live"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, base, "shared")

    def test_shared_passes_with_no_live_occupant(self, tmp_path, jj_repo):
        base, _, _ = self._commits(jj_repo)
        with patch("cld.docker.docker_anchor_list", return_value=[]):
            cfg = Config(mailbox_root=str(tmp_path / "mb"))
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, base, "shared") == base

    def test_live_master_does_not_block_task_agent_nesting(self, tmp_path, jj_repo):
        """A task-agent may anchor inside its own live master's tree."""
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, name="cld_master_r", kind="master")
        with patch("cld.docker.docker_anchor_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_live_devcontainer_does_not_block(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, name="cld_r", kind="devcontainer")
        with patch("cld.docker.docker_anchor_list", return_value=records):
            assert resolve_anchor_checked(cfg, jj_repo.repo_root, inside) == inside

    def test_live_task_agent_still_blocks_nested_spawn(self, tmp_path, jj_repo):
        """A live task-agent's own tree still refuses another spawn on top of it."""
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, name="cld_task_r_a", kind="task-agent")
        with patch("cld.docker.docker_anchor_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)

    def test_live_run_still_blocks(self, tmp_path, jj_repo):
        _, live_anchor, inside = self._commits(jj_repo)
        cfg, records = self._fleet(tmp_path, jj_repo, live_anchor, name="cld_run_r", kind="run")
        with patch("cld.docker.docker_anchor_list", return_value=records):
            with pytest.raises(RuntimeError, match="inside the live reach"):
                resolve_anchor_checked(cfg, jj_repo.repo_root, inside)


class TestParsePeersEnv:
    def test_round_trip(self):
        peers = {"cld_agent_r_a": 15, "cld_agent_r_b": 5}
        assert parse_peers_env(TaskAgentSpec(slug="t", peers=peers).peers_env()) == peers

    def test_empty_is_empty_dict(self):
        assert parse_peers_env("") == {}

    def test_ignores_blank_segments(self):
        assert parse_peers_env("a:1,,b:2,") == {"a": 1, "b": 2}

    @pytest.mark.parametrize("value", ["nocolon", "a:", ":5", "a:x", "a:1.5"])
    def test_malformed_raises(self, value):
        with pytest.raises(ValueError, match="malformed peer spec"):
            parse_peers_env(value)
