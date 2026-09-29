#!/usr/bin/env python3
# cld host broker -- the sshd ForceCommand target for the restricted broker key.
# sshd ignores whatever command the client asked for and delivers it in
# $SSH_ORIGINAL_COMMAND, which must have the shape:
#
#     <action> <session> <base64-argv>
#
# The broker serves ANY repo that has a running task-agent or ticket
# container -- no per-repo config, no whitelist. It resolves the target
# repo from the calling container's host-set labels (established at launch,
# not caller input): the single `org.cld.repo-root` for v1 kinds, or -- for a
# multi-repo ticket container -- the flat `org.cld.repo.<name>` labels, picked
# by a leading `--repo <name>` in the run-tests/graphql argv
# (LabelCaller.repo). The caller controls only: the action, a validated
# session id, and the decoded argv. Nothing is ever eval'd.
#
# Two domains, joined only by the Caller protocol: a caller source answers
# who called and what they may reach, and the actions consume those answers as
# plain values. Two sources exist: LabelCaller (docker labels) for the sshd
# route, and CliCaller (command-line flags, via `cld-brokerctl run`) for
# running an action from the host without any container.
#
# v1 sessions are all named `cld_agent_*` (task-agents -- the prefix is a
# leftover of the removed standing repo agent; kind is a label, not a name, see
# cld/docker.py:task_agent_container_name), plus the v2 ticket containers. Any
# of these may call `run-tests` / `list-containers`. The `task-agent` launcher
# action gates on validate_target, which since the removal of the master role
# (the only kind that ever carried an `org.cld.targets` allowlist) accepts
# nothing but the caller's own `org.cld.repo-root` -- and a container resolves
# its cwd to a container path, not that host path, so in practice the launcher
# route is unusable until it is reworked ticket-scoped.
#
# The regex below is a format check only, not an authorization boundary --
# the session doubles as the docker container name, and the label read below
# (set at launch, trusted) is what actually gates repo/target access.
#
# ADD AN ACTION: define a function named `action_<name>` (hyphens in <name>
# become underscores, so `run-tests` -> `action_run_tests`). It receives the
# Caller the dispatcher built and the decoded argv as a list, and asks the
# Caller only for what it needs (repo, revision, targets) -- run-tests does it
# via `resolve_test_context`.
#
# Standard library only: the host's python3 runs this file directly, with no
# venv and no `cld` import.
import argparse
import base64
import http.client
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, NamedTuple, NoReturn, Protocol, Self, TextIO

# Broker settings: the process environment overlaid with broker.conf (load_conf).
CONF: dict[str, str] = {}

_DEFAULTS = {
    "RUNTESTS_IMAGE": "runtests:latest",
    "GRAPHQL_IMAGE": "graphqlserver:latest",
    # poetry install dominates every graphql start/restart (slow path, no fast
    # restart yet -- see docs/impl-graphql-broker-plan.md §11), so the readiness
    # budget has to cover it, not just a normal server boot.
    "GRAPHQL_START_TIMEOUT": "300",
    "GRAPHQL_QUERY_TIMEOUT": "30",
    "GRAPHQL_OUTPUT_MAX_BYTES": "65536",
    # GRAPHQL_URL_ALLOWLIST is deliberately left out here (see check_url_allowlisted):
    # empty means "no raw URLs at all", and that has to be the default, not a value
    # defaulted in code that's easy to lose track of.
}

_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)


class Caller(Protocol):
    """The caller-derived inputs of every action. Methods are lazy, so an action
    never pays for -- or fails on -- an input it does not use."""

    name: str  # session id: names the caller's own resources (graphql server, task-agent parent)

    def repo(self, selector: str) -> str:
        """The host repo to act on; *selector* is the `--repo` name, "" if absent."""
        ...

    def revision(self, repo: str) -> str | None:
        """The caller's current commit in *repo*; None if it cannot be resolved."""
        ...

    def targets(self) -> list[str]:
        """Host repos the caller may launch task-agents for."""
        ...


class Scope(NamedTuple):
    """A resolved (session, repo) pair: what the graphql ops act on."""

    session: str
    repo: str


class SecretsEnv(NamedTuple):
    project_subdir: str
    env_file: str


class GraphqlConfig(NamedTuple):
    command: str
    port: str
    health_path: str


def _die(message: str, code: int) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(code)


def _setting(key: str) -> str:
    return CONF.get(key) or _DEFAULTS.get(key, "")


def parse_env_file(path: str) -> dict[str, str]:
    """`[export] KEY=VALUE` lines of a shell-style env file, with the shell's
    quoting rules. Nothing is expanded or executed: `$VAR` stays literal, and
    any other line is skipped."""
    values = {}
    for line in Path(path).read_text(errors="surrogateescape").splitlines():
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if tokens[:1] == ["export"]:
            tokens = tokens[1:]
        if tokens and (match := _ASSIGNMENT_RE.fullmatch(tokens[0])):
            values[match[1]] = match[2]
    return values


def load_conf() -> None:
    """Overlay broker.conf on the environment. A conf assignment to a variable
    that is already exported (PATH above all) reaches child processes, the way
    sourcing it into a shell would; anything else stays broker-internal."""
    path = os.environ.get("CLD_BROKER_CONF") or "/etc/cld/broker.conf"
    try:
        parsed = parse_env_file(path)
    except OSError:
        parsed = {}
    CONF.update(os.environ, **parsed)
    os.environ.update({key: value for key, value in parsed.items() if key in os.environ})


# ---- process helpers -------------------------------------------------------

def _capture(argv: Sequence[str], stderr: int | None = subprocess.DEVNULL) -> subprocess.CompletedProcess[bytes]:
    """Run *argv* with stdout captured. A missing binary is exit 127, as in a shell."""
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=stderr)
    except OSError as e:
        if stderr is None:
            print(f"{argv[0]}: {e.strerror}", file=sys.stderr)
        return subprocess.CompletedProcess(argv, 127, b"")


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    # Trailing newlines stripped, like a shell command substitution.
    return os.fsdecode(result.stdout).rstrip("\n")


def _out(*argv: str) -> str | None:
    """stdout of *argv*, stderr discarded; None if it failed."""
    result = _capture(argv)
    return None if result.returncode else _text(result)


def _out_strict(*argv: str, stderr: int | None = subprocess.DEVNULL) -> str:
    """stdout of *argv*; exits the broker with its status if it failed. Kept for
    the lookups whose failure has always ended the request with no message."""
    result = _capture(argv, stderr)
    if result.returncode:
        sys.exit(result.returncode)
    return _text(result)


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def _exec(argv: list[str]) -> NoReturn:
    """Replace the broker with *argv*, so its stdout/stderr/exit code flow straight back."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        _die(f"{argv[0]}: {e.strerror}", 127 if isinstance(e, FileNotFoundError) else 126)


def _emit(data: bytes, stream: TextIO) -> None:
    stream.flush()
    stream.buffer.write(data)
    stream.buffer.flush()


def _label(container: str, key: str) -> str | None:
    return _out("docker", "inspect", container, "--format", f'{{{{index .Config.Labels "{key}"}}}}')


def _is_repo(path: str) -> bool:
    return os.path.isdir(f"{path}/.jj") or os.path.isdir(f"{path}/.git")


def _rm_container(name: str) -> None:
    _capture(["docker", "rm", "-f", name])


def _forget_workspace(repo: str, workspace: str) -> None:
    _capture(["jj", "-R", repo, "--ignore-working-copy", "workspace", "forget", workspace])


# Read a single quoted-string key from a flat cld TOML config (e.g. .cld/config.toml).
# Empty if the file or key is absent. Avoids a TOML parser in the broker.
def cld_conf_get(file: str, key: str) -> str:
    try:
        lines = Path(file).read_text(errors="surrogateescape").splitlines()
    except OSError:
        return ""
    pattern = re.compile(rf'\s*{re.escape(key)}\s*=\s*"([^"]*)"')
    return next((match[1] for line in lines if (match := pattern.match(line))), "")


# ---- caller sources -------------------------------------------------------

@dataclass
class LabelCaller:
    """The sshd route's Caller: the session container's host-set docker labels
    (org.cld.kind, org.cld.repo-root or org.cld.repo.<name>, org.cld.targets)
    and its jj workspace. Labels are set at launch, never by the caller, which
    is what makes them an authorization source."""

    name: str
    kind: str
    repo_root: str  # empty for a ticket, which carries org.cld.repo.<name> labels instead

    # The session doubles as the container name. A ticket's repo stays unresolved
    # here: it depends on the action's --repo, see repo().
    @classmethod
    def from_session(cls, session: str) -> Self:
        if (kind := _label(session, "org.cld.kind")) is None:
            _die(f"no cld container for session {session}", 3)
        repo_root = ""
        if kind != "ticket":
            repo_root = _label(session, "org.cld.repo-root") or ""
            if not (repo_root and _is_repo(repo_root)):
                _die(f"no task-agent container for session {session}", 3)
        return cls(session, kind, repo_root)

    # Repo selection (docs/design-ticket-containers.md section 6.1): a ticket
    # container mounts several repos, resolved against its flat
    # `org.cld.repo.<name>` labels -- the flat encoding keeps this to plain line
    # parsing. No selector with exactly one labeled repo uses it (the single-repo
    # convenience); several without one is an error. A v1 kind keeps its
    # repo-root and refuses a selector. Names are matched against enumerated
    # labels, never spliced into an inspect format string, so a hostile name
    # cannot template-inject.
    def repo(self, selector: str) -> str:
        if self.kind != "ticket":
            if selector:
                _die(f"denied: --repo is only for ticket containers ({self.name} is '{self.kind or 'unlabeled'}')", 2)
            return self.repo_root
        labels = _capture(["docker", "inspect", self.name, "--format",
                           "{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}"])
        mounts = [line.partition("=") for line in os.fsdecode(labels.stdout).splitlines()
                  if line.startswith("org.cld.repo.")]
        names = [key.removeprefix("org.cld.repo.") for key, _, _ in mounts]
        mounted = " ".join(names) or "none"
        if not selector:
            if len(mounts) != 1:
                _die(f"denied: {self.name} mounts {len(mounts)} repos -- pass --repo <name> (mounted: {mounted})", 3)
            repo = mounts[0][2]
        else:
            repo = next((path for key, _, path in reversed(mounts) if key == f"org.cld.repo.{selector}"), "")
        if not repo:
            _die(f"denied: repo '{selector}' is not mounted in {self.name} (mounted: {mounted})", 3)
        if not _is_repo(repo):
            _die(f"denied: repo path '{repo}' for {self.name} is not a repo", 3)
        return repo

    # "<session>@" is the *workspace* tip, not the session bookmark. The bookmark
    # is set once at first container launch
    # (imgs/claude-devcontainer/entrypoint-claude-devcontainer.sh) and never moved
    # again, so it freezes at the container's first `jj commit` -- the normal state
    # once an agent starts working, which left this resolving one change behind
    # the code the caller just wrote. Workspace name == bookmark name == container
    # name == session (verified via `jj workspace list`), and `<session>@` is
    # the same workspace-scoped-`@` idiom `cld/vcs/detect.py` already uses.
    # --ignore-working-copy matters here too: without it, `jj -R <repo> log`
    # snapshots the *host's* default workspace as a side effect, which would
    # violate the broker's store-reading-only posture (docs/design-cld-broker.md
    # §9) for no freshness gain -- watchman already wrote the container's own
    # snapshot independently.
    def revision(self, repo: str) -> str | None:
        return _out("jj", "-R", repo, "--ignore-working-copy", "log", "--no-graph", "-n1",
                    "-r", f"{self.name}@", "-T", "commit_id")

    # The caller's own repo-root plus its org.cld.targets allowlist. Nothing sets
    # org.cld.targets since the master role was removed (it was the only kind
    # that carried one), so today this is the repo-root alone; the read stays
    # for the ticket-scoped rework of the task-agent route.
    def targets(self) -> list[str]:
        return [target for target in (self.repo_root, *(_label(self.name, "org.cld.targets") or "").split(":"))
                if target]


@dataclass
class CliCaller:
    """The host route's Caller: every input given on the command line by the
    operator (`cld-brokerctl run`), no container involved. The operator already
    holds everything the broker guards, so nothing here is an authorization
    boundary."""

    name: str
    repo_path: str
    revset: str
    extra_targets: list[str]

    # One repo, given by -C: a ticket's --repo selector has nothing to select here.
    def repo(self, selector: str) -> str:
        if selector:
            _die(f"denied: --repo selects a ticket container's mounted repo -- on the host pass -C <path> "
                 f"(using {self.repo_path})", 2)
        return self.repo_path

    # No --ignore-working-copy, unlike LabelCaller: the default revset `@` is the
    # operator's own working copy, and snapshotting it first is what makes
    # uncommitted edits part of the run.
    def revision(self, repo: str) -> str | None:
        return _out("jj", "-R", repo, "log", "--no-graph", "-n1", "-r", self.revset, "-T", "commit_id")

    def targets(self) -> list[str]:
        return [self.repo_path, *self.extra_targets]


# ---- actions --------------------------------------------------------------
# Each runs on the host as your user and receives the decoded pytest/tool argv.
# `_exec` so the child's stdout/stderr/exit code flow straight back.

# Shared by resolve_test_context, resolve_graphql_config, and graphql alias
# lookup: the project's own .env, following pyproject_dir the same way
# PROJECT_SUBDIR does for run-tests -- pyproject.toml and .env are assumed to
# share a directory (docs/design-cld-broker.md §13). No revision resolution
# here -- callers that only need the secrets path (e.g. a graphql alias lookup
# for a repo whose graphql_command isn't even set, or a git-backed repo where
# jj would exit 3) must not pay for jj resolution they don't need.
def resolve_secrets_env_file(repo: str) -> SecretsEnv:
    subdir = cld_conf_get(f"{repo}/.cld/config.toml", "pyproject_dir") or "."
    if subdir.startswith("/"):
        return SecretsEnv(subdir, f"{subdir}/.env")
    if subdir == ".":
        return SecretsEnv(subdir, f"{repo}/.env")
    return SecretsEnv(subdir, f"{repo}/{subdir}/.env")


def resolve_revision(caller: Caller, repo: str) -> str:
    if (rev := caller.revision(repo)) is None:
        _die(f"cannot resolve revision for workspace {caller.name} in {repo}", 3)
    return rev


# run-tests needs the session's current revision + the project's .env; resolve
# those lazily here (not in the shared dispatcher) so read-only actions like
# list-containers don't depend on the session's revision being resolvable.
def resolve_test_context(caller: Caller, repo: str) -> tuple[str, SecretsEnv]:
    return resolve_revision(caller, repo), resolve_secrets_env_file(repo)


def _secret_mount(secrets: SecretsEnv) -> list[str]:
    return ["-v", f"{secrets.env_file}:/secrets/.env:ro"] if os.path.isfile(secrets.env_file) else []


def run_tests(repo: str, rev: str, secrets: SecretsEnv, argv: list[str]) -> NoReturn:
    _exec([
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        "-v", f"{repo}:/repo", *_secret_mount(secrets),
        "-e", f"REVISION={rev}", "-e", f"PROJECT_SUBDIR={secrets.project_subdir}",
        _setting("RUNTESTS_IMAGE"), *argv,
    ])


def action_run_tests(caller: Caller, argv: list[str]) -> None:
    name, argv = parse_repo_arg(argv)
    repo = caller.repo(name)
    run_tests(repo, *resolve_test_context(caller, repo), argv)


# Enumerate cld containers for the messenger / `cld task-agent status`.
# Read-only: the sole argv is an optional kind filter
# (task-agent|ticket, or the legacy agent). Emits one
# tab-separated `name<TAB>kind<TAB>repo<TAB>raw-status` line per container.
def action_list_containers(caller: Caller, argv: list[str]) -> None:
    kind = argv[0] if argv else ""
    label = f"label=org.cld.kind={kind}" if kind else "label=org.cld.kind"
    result = _capture(["docker", "ps", "-a", "--filter", label, "--format", "{{.Names}}\t{{.Status}}"],
                      stderr=None)
    for line in _text(result).splitlines():
        name, _, status = line.partition("\t")
        if not name:
            continue
        labels = _out("docker", "inspect", name, "--format",
                      '{{index .Config.Labels "org.cld.kind"}}|{{index .Config.Labels "org.cld.repo-root"}}')
        if labels is None:
            continue
        ckind, _, repo = labels.partition("|")
        print(f"{name}\t{ckind}\t{repo}\t{status}")
    sys.exit(result.returncode)


# The task-agent launcher: a container that pushes a deliverable branch
# needs the host user's ssh-agent, and `stage_ssh_agent` (cld/docker.py) forwards
# whatever socket $SSH_AUTH_SOCK names. sshd builds a fresh session environment,
# so that variable only exists here if broker.conf sets it -- and a conf
# assignment is not exported, hence this. Agent *forwarding* over the broker
# connection is not an alternative: that socket dies with the ssh session, while
# the container outlives it. Never fatal, matching stage_ssh_agent: a dead path
# is warned about and the launch proceeds without a key, as it did before.
def stage_agent_socket() -> None:
    if not (sock := CONF.get("SSH_AUTH_SOCK")):
        return
    try:
        live = stat.S_ISSOCK(os.stat(sock).st_mode)
    except OSError:
        live = False
    if live:
        os.environ["SSH_AUTH_SOCK"] = sock
    else:
        print(f"[cld-broker] SSH_AUTH_SOCK={sock} is not a live socket -- "
              "launching without agent forwarding", file=sys.stderr)
        os.environ.pop("SSH_AUTH_SOCK", None)


# run-tests / graphql take an optional leading `--repo <name>`, the selector a
# multi-repo ticket caller passes to Caller.repo. Consume it off an action's
# argv, returning the name ("" if absent) and the rest.
def parse_repo_arg(argv: list[str]) -> tuple[str, list[str]]:
    match argv:
        case ["--repo", *rest]:
            name, rest = (rest[0], rest[1:]) if rest else ("", [])
        case [first, *rest] if first.startswith("--repo="):
            name = first.removeprefix("--repo=")
        case _:
            return "", argv
    if not name:
        _die("denied: --repo needs a repo name", 2)
    return name, rest


# <target> is validated against Caller.targets, never trusted from the argv
# alone -- so an action can only ever run for a repo the caller source sanctioned.
def validate_target(caller: Caller, target: str) -> None:
    if target not in caller.targets():
        _die(f"denied: target '{target}' not registered for {caller.name}", 3)
    if not _is_repo(target):
        _die(f"denied: target '{target}' is not a repo", 3)


# Launch / manage a task-scoped agent for one of the caller's registered repos
# (docs/design-task-agents.md §9). Target validation as above, plus three
# argv rules that make this safe to expose to a container:
#
#   --force   denied outright. Overriding a reap-readiness refusal is a human act;
#             a container must not be able to discard uncaptured work or break a
#             third agent's edge (§7).
#   --parent  denied from the caller and appended by us as the validated session,
#             so an agent's recorded owner is host-set and cannot be forged.
#   prompts   every positional must be an `@ref`. Refs are resolved host-side and their
#             text composed into the new container's brief, so a bare path would let a
#             container read any host file the user can. The container-side client folds
#             its own local files into `-p` instead (docs/design-prompt-chaining.md §4);
#             cld.prompts contains the escape check for the refs themselves.
def action_task_agent(caller: Caller, argv: list[str]) -> None:
    if len(argv) < 2:
        _die("denied: task-agent needs <target> <op>", 2)
    target, op, *args = argv
    # Exactly the ops the container route delegates. `transcript` is absent on
    # purpose: the mailbox is bind-mounted into the caller, never needing the host.
    if op not in ("start", "status", "logs", "shutdown"):
        _die(f"denied: bad task-agent op '{op}'", 2)
    validate_target(caller, target)
    stage_agent_socket()

    # Both spellings of each denial: click accepts `--opt=value`, so matching the bare
    # token alone would let `--parent=<other-master>` through and forge an owner.
    for arg in args:
        if arg == "--force" or arg.startswith("--force="):
            _die("denied: --force is host-only", 2)
        if arg == "--parent" or arg.startswith("--parent="):
            _die("denied: --parent is set by the broker", 2)

    # start's positionals are prompt refs, and only `@refs` may cross: they name files
    # in the *target repo's* prompts tree, which the host resolves. Option values are
    # still not ref-checked -- task text legitimately contains slashes, so checking them
    # would deny ordinary work like -p "fix cld/cli.py" -- but arity is enumerated, not
    # guessed: only these options of `cld task-agent start` take a *separate* value, so
    # only they swallow the next token. Every other `-*` token (a flag, or the
    # `--opt=value` form, which carries its value inside the token) consumes nothing;
    # assuming it did skipped the positional after it, letting a bare path cross.
    if op == "start":
        skip_value = False
        for arg in args:
            if skip_value:
                skip_value = False
            elif arg in ("-n", "--name", "-p", "--prompt", "--branch", "-m", "--model",
                         "-r", "--revision", "--peer"):
                skip_value = True
            elif not arg.startswith(("-", "@")):
                _die(f"denied: prompt ref '{arg}' must be an @ref, not a path", 2)

    try:
        os.chdir(target)
    except OSError:
        _die(f"cannot cd to {target}", 3)
    # --parent is what makes ownership host-set: it stamps the spawn, scopes the roster
    # and gates the reap. `logs` has no such option (a read needs no authority), so
    # appending it there would just make cld reject the argv.
    parent = [] if op == "logs" else ["--parent", caller.name]
    _exec(["cld", "task-agent", op, *args, *parent])


# ---- graphql: server lifecycle + credentialed queries over the broker -----
# See docs/impl-graphql-broker-plan.md for the design; this block implements
# §3-§6. One server per session (`cld_gql_<session>`, jj workspace
# `gql-<session>`), started from the `graphqlserver` image (§2) at the
# session's current revision, ops: start/stop/restart/status/logs (server
# lifecycle) and query/introspect/endpoints (client, credentialed by the
# broker so the container never holds a token).

# Per-repo graphql config (§3), soft: does NOT require graphql_command, so
# status/logs/stop/endpoints still work (or report "not configured") for a
# repo that has never been wired up. Only resolve_graphql_context (below,
# used by `start`) hard-requires it -- that's the op about to spend a
# poetry install on it. cld_conf_get parses double-quoted strings only:
# `graphql_port = 8000` silently yields nothing, it must be `= "8000"`.
def resolve_graphql_config(repo: str) -> GraphqlConfig:
    conf = f"{repo}/.cld/config.toml"
    return GraphqlConfig(
        cld_conf_get(conf, "graphql_command"),
        cld_conf_get(conf, "graphql_port") or "8000",
        cld_conf_get(conf, "graphql_health_path") or "/graphql",
    )


# start's context: config (hard-required -- no guessed default, the old MCP's
# `poetry run python manage.py` default was a lide-api-shaped guess and is not
# being carried over) + the secrets path.
def resolve_graphql_context(repo: str) -> tuple[SecretsEnv, GraphqlConfig]:
    secrets = resolve_secrets_env_file(repo)
    config = resolve_graphql_config(repo)
    if not config.command:
        _die(f"denied: graphql_command is not set in {repo}/.cld/config.toml -- add "
             'graphql_command = "..." (quotes required; cld_conf_get silently '
             "ignores an unquoted value)", 3)
    return secrets, config


# GRAPHQL_BIND resolved lazily (only `start` publishes a port) and only if the
# operator hasn't pinned one in broker.conf. Bridge gateway, not 0.0.0.0 --
# binding the wildcard address would expose a real-credentials server to the
# whole LAN, mirroring the sshd's own bridge-only posture
# (broker/sshd_cld_broker.conf).
def resolve_graphql_bind() -> str:
    if bind := CONF.get("GRAPHQL_BIND"):
        return bind
    gateway = _out("docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}")
    return "172.17.0.1" if gateway is None else gateway


# Nothing reaps what `start` spawns: cld-brokerctl.sh manages only the sshd,
# and no host-side hook fires when a session container is reaped. Run at the
# top of `start` and `status` so staleness never survives past the next
# lookup, and exposed standalone as `cld-brokerctl.sh graphql-sweep` for an
# operator to reap after a host reboot without starting any session. Never
# fatal -- a sweep failure must not block the caller's own request.
def sweep_gql_orphans() -> None:
    servers = _capture(["docker", "ps", "-a", "--filter", "label=org.cld.gql-session", "--format", "{{.Names}}"],
                       stderr=None)
    for container in _text(servers).split():
        if (session := _label(container, "org.cld.gql-session")) is None:
            continue
        # Deliberately "exists", not "running": a stopped-but-not-removed
        # session container is expected to resume (cld persistence model, see
        # CLAUDE.md), and reaping its graphql server on every stop would
        # defeat the point of this being an *orphan* sweep -- only a session
        # that is actually gone (container removed) should cost its server.
        if not _capture(["docker", "inspect", session]).returncode:
            continue  # owning session exists -- keep
        repo = _label(container, "org.cld.gql-repo")
        _rm_container(container)
        if repo:
            _forget_workspace(repo, f"gql-{session}")


# Two host-side filters applied to everything logs/query/introspect return --
# nothing but run-tests bounds broker output today (docs/impl-graphql-broker-plan.md
# §6). Masking happens here, not client-side in the container's MCP: a server
# holding real DB credentials can echo a DSN or token in an error body, and by
# the time the container sees it, it's too late. Deliberately duplicates
# cld/log.py's mask_secrets() patterns (cross-reference:
# cld/log.py:_SECRET_KV_RE / _SECRET_PATH_RE / _SECRET_URL_RE) -- the broker
# must not depend on `cld` being importable by the host's python3. Applied line
# by line, so no pattern ever matches across a newline.
_MASKS = (
    (re.compile(r"(TOKEN|KEY|SECRET|PASSWORD)=[^\s,'\"]+", re.IGNORECASE), r"\1=<redacted>"),
    (re.compile(r"/run/secrets/[A-Za-z0-9._/-]+"), "/run/secrets/<redacted>"),
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]*@"), r"\1<redacted>@"),
    (re.compile(r'"(token|key|secret|password)"\s*:\s*"[^"]*"', re.IGNORECASE), r'"\1": "<redacted>"'),
)


def _mask_line(line: str) -> str:
    for pattern, replacement in _MASKS:
        line = pattern.sub(replacement, line)
    return line


def mask_output(data: bytes) -> bytes:
    text = data.decode(errors="surrogateescape")
    return "\n".join(map(_mask_line, text.split("\n"))).encode(errors="surrogateescape")


# Cap output to *max_bytes*. `keep`=head for query (the response starts with
# `data`), `keep`=tail for logs (recent lines matter most). Trailing newlines
# are dropped before measuring. Truncation notice goes to stderr, never mixed
# into the capped stdout.
def cap_output(data: bytes, max_bytes: int, keep: str) -> None:
    data = data.rstrip(b"\n")
    if len(data) > max_bytes:
        print(f"[cld-broker] graphql output truncated to {max_bytes} of {len(data)} bytes "
              "(raise GRAPHQL_OUTPUT_MAX_BYTES for more)", file=sys.stderr)
        data = data[:max_bytes] if keep == "head" else data[-max_bytes:]
    _emit(data, sys.stdout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: http.client.HTTPMessage, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes] | None:
    """POST the way `curl -sS` did: every HTTP status (a redirect included, which
    is not followed) is a response, URL userinfo becomes basic auth, and only a
    transport failure -- reported on stderr -- returns None. Shared by the
    readiness probe and query execution."""
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None:
        credentials = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers = {**headers, "Authorization": f"Basic {base64.b64encode(credentials.encode()).decode()}"}
        url = urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (OSError, ValueError, http.client.HTTPException) as e:
        print(f"[cld-broker] graphql request failed: {e}", file=sys.stderr)
        return None


def _alias_key(alias: str) -> str:
    return alias.upper().replace("-", "_")


# Aliases come from the repo's own .env (the same per-repo secrets file
# run-tests already mounts) -- no new secret store, no new broker config:
#   CLD_GRAPHQL_URL_<ALIAS>    = https://dev.example/graphql
#   CLD_GRAPHQL_AUTH_<ALIAS>   = Bearer eyJ...        # full Authorization value
#   CLD_GRAPHQL_COOKIE_<ALIAS> = session=...          # full Cookie value
# Parsed into a local dict over the broker's own settings, so the broker's
# environment is never polluted by whatever else the repo's .env defines.
def lookup_alias(alias: str, secrets_env_file: str) -> tuple[str, str, str]:
    values = dict(CONF)
    if os.path.isfile(secrets_env_file):
        values.update(parse_env_file(secrets_env_file))
    key = _alias_key(alias)
    return (values.get(f"CLD_GRAPHQL_URL_{key}", ""), values.get(f"CLD_GRAPHQL_AUTH_{key}", ""),
            values.get(f"CLD_GRAPHQL_COOKIE_{key}", ""))


# If the broker will POST to any URL the container names, the container gains
# the host as an egress channel and SSRF pivot -- it could reach an internal
# service it cannot itself route to. It cannot leak a credential that way
# (raw URLs get no auth, see resolve_target), but the channel is new. Gated on
# an explicit hostname allowlist; empty (the default) means no raw URLs at
# all, aliases only. Exact hostname match, no substring/prefix/wildcard.
def check_url_allowlisted(target: str) -> None:
    if target.startswith("http://"):
        host = target.removeprefix("http://")
    elif target.startswith("https://"):
        host = target.removeprefix("https://")
    else:
        _die(f"denied: unsupported URL scheme in '{target}' (http/https only)", 2)
    host = host.split("/", 1)[0]     # strip path
    host = host.split("?", 1)[0]     # strip query (in case no path preceded it)
    host = host.split("#", 1)[0]     # strip fragment
    host = host.rpartition("@")[2]   # strip userinfo (user:pass@) -- must precede the port strip
    host = host.split(":", 1)[0]     # strip port
    if host not in [h for h in _setting("GRAPHQL_URL_ALLOWLIST").split(" ") if h]:
        _die(f"denied: host '{host}' is not in GRAPHQL_URL_ALLOWLIST -- set it in "
             "broker.conf to permit raw-URL graphql targets (empty = none allowed)", 3)


# Resolves <target> (§5.1) into (url, authorization, cookie):
#   local              this session's own server, discovered via `docker port`
#   [A-Za-z0-9_-]+     an alias, looked up in the repo's .env -- auth attached
#   http(s)://…        the URL verbatim, gated on the allowlist -- never any auth
#   anything else      denied
def resolve_target(scope: Scope, target: str) -> tuple[str, str, str]:
    if target == "local":
        config = resolve_graphql_config(scope.repo)
        host_port = _first_line(_out("docker", "port", f"cld_gql_{scope.session}", f"{config.port}/tcp") or "")
        if not host_port:
            _die(f"denied: no local graphql server running for {scope.session} -- start one first", 3)
        return f"http://{host_port}{config.health_path}", "", ""
    if target.startswith(("http://", "https://")):
        check_url_allowlisted(target)
        return target, "", ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", target):
        _die(f"denied: bad target '{target}'", 2)
    secrets_env_file = resolve_secrets_env_file(scope.repo).env_file
    url, auth, cookie = lookup_alias(target, secrets_env_file)
    if not url:
        _die(f"denied: unknown graphql alias '{target}' -- set CLD_GRAPHQL_URL_{_alias_key(target)} in "
             f"{secrets_env_file}", 3)
    return url, auth, cookie


# Argv: query <target> <query-string> [variables-json]. The body is built with
# json.dumps (never string concatenation, so a query containing quotes can't
# corrupt it) and never appears in any child process's argv, where it would be
# visible in the host's process table.
def do_graphql_query(scope: Scope, target: str, query: str, variables: str = "") -> None:
    url, auth, cookie = resolve_target(scope, target)

    try:
        body = json.dumps({"query": query, "variables": json.loads(variables) if variables else {}})
    except json.JSONDecodeError as e:
        print(f"bad variables JSON: {e}", file=sys.stderr)
        _die("denied: malformed variables JSON for graphql query", 2)

    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    if cookie:
        headers["Cookie"] = cookie

    if (response := http_post(url, body.encode(), headers, float(_setting("GRAPHQL_QUERY_TIMEOUT")))) is None:
        _die(f"graphql request to {url} failed (connection error)", 3)
    status, data = response

    cap_output(mask_output(data), int(_setting("GRAPHQL_OUTPUT_MAX_BYTES")), "head")
    print()

    if not 200 <= status <= 299:
        _die(f"graphql endpoint returned HTTP {status}", 1)


# Kept broker-side so the container doesn't need to ship the introspection
# document itself.
_GRAPHQL_INTROSPECTION_QUERY = "{__schema{queryType{name} mutationType{name} types{name kind description fields{name description type{name kind ofType{name kind ofType{name kind}}} args{name type{name kind ofType{name kind}}}} inputFields{name type{name kind ofType{name kind ofType{name kind}}}} enumValues{name description}}}}"


# alias names only, one per line, discovered by scanning .env for
# CLD_GRAPHQL_URL_* -- never any value. Printed in the alias's canonical
# lower-case/hyphenated spelling (_alias_key is lossy -- both `-` and `_`
# fold to `_` -- so this is *a* valid spelling that round-trips through
# lookup_alias, not necessarily byte-identical to however the operator
# originally wrote it in .env).
def do_graphql_endpoints(scope: Scope) -> None:
    secrets_env_file = resolve_secrets_env_file(scope.repo).env_file
    try:
        text = Path(secrets_env_file).read_text(errors="surrogateescape")
    except OSError:
        return
    for alias in re.findall(r"^CLD_GRAPHQL_URL_([A-Za-z0-9_]+)", text, re.MULTILINE):
        print(alias.replace("_", "-").lower())


# One tab-separated line: state<TAB>port<TAB>endpoint<TAB>revision<TAB>container<TAB>stale.
# state in running|starting|exited|not_started. stale is "true"/"false" when
# there's a serving revision to compare against the session's current tip,
# empty otherwise (not_started, exited, or the tip couldn't be resolved).
# Parsed client-side the same way _parse_container_line (cld/broker.py)
# parses list-containers.
def print_gql_status_line(state: str, port: str, endpoint: str, revision: str, container: str,
                          stale: str) -> None:
    print("\t".join((state, port, endpoint, revision, container, stale)))


def _probe(url: str, timeout: float) -> bool:
    # {__typename} is valid at the root of every GraphQL schema, unlike the old
    # in-process MCP's `query { hello }` probe, which could never succeed
    # against a real schema (docs/impl-graphql-broker-plan.md §4.3).
    response = http_post(url, b'{"query":"{__typename}"}', {"Content-Type": "application/json"}, timeout)
    return response is not None and response[0] == 200 and b'"errors"' not in response[1]


def do_graphql_status(scope: Scope, tip: str | None) -> None:
    sweep_gql_orphans()
    config = resolve_graphql_config(scope.repo)
    cname = f"cld_gql_{scope.session}"
    if _capture(["docker", "inspect", cname]).returncode:
        print_gql_status_line("not_started", "", "", "", "", "")
        return
    if _out_strict("docker", "inspect", cname, "--format", "{{.State.Running}}") != "true":
        print_gql_status_line("exited", "", "", "", cname, "")
        return
    # Read the actually-serving revision back off the container's own env
    # rather than re-resolving the session's *current* tip -- they can differ
    # once the caller has kept editing since this server started.
    env = _out_strict("docker", "inspect", cname, "--format", "{{range .Config.Env}}{{println .}}{{end}}")
    rev = "\n".join(line.removeprefix("REVISION=") for line in env.splitlines()
                    if line.startswith("REVISION="))
    # Compare against the current tip so the caller can tell a running server
    # is serving stale code without a second call. The tip is optional --
    # status has to keep answering on a git-backed repo, or after
    # graphql_command was removed, same as stop does -- so an unresolvable tip
    # (None) leaves stale empty rather than aborting the op.
    stale = str(rev != tip).lower() if tip and rev else ""
    host_port = _first_line(_out_strict("docker", "port", cname, f"{config.port}/tcp"))
    # docker port publishing early (or `poetry install` still running behind
    # it) doesn't mean the server itself answers yet -- a live probe is the
    # only way to tell "running" from "starting" here, same probe do_graphql_start
    # waits on.
    if not host_port or not _probe(f"http://{host_port}{config.health_path}", 3):
        print_gql_status_line("starting", "", "", rev, cname, stale)
        return
    print_gql_status_line("running", host_port.rpartition(":")[2], f"http://{host_port}{config.health_path}",
                          rev, cname, stale)


# `docker rm -f` sends SIGKILL, so the entrypoint's own `trap EXIT` (and the
# fact that it `exec`s into $GQL_COMMAND, which drops the trap even on a
# graceful exit) never gets a chance to forget the jj workspace -- it would
# leak into the user's store. So `stop` forgets it explicitly, unconditionally
# (docs/impl-graphql-broker-plan.md §4.4). Deliberately does NOT call
# resolve_graphql_context / resolve_graphql_config -- teardown must work even
# for a repo whose graphql_command was since unset or removed.
def do_graphql_stop(scope: Scope) -> None:
    cname = f"cld_gql_{scope.session}"
    # Forget the workspace in the repo the server was STARTED for (its own
    # org.cld.gql-repo label), not scope.repo: a multi-repo ticket can pass a
    # different --repo to stop than it did to start. Falls back to scope.repo for
    # a container without the label (or already gone).
    gql_repo = _label(cname, "org.cld.gql-repo")
    _capture(["docker", "stop", "-t", "10", cname])
    _rm_container(cname)
    _forget_workspace(gql_repo or scope.repo, f"gql-{scope.session}")


def _dump_logs(cname: str) -> None:
    logs = _capture(["docker", "logs", "--tail", "50", cname], stderr=subprocess.STDOUT)
    _emit(mask_output(logs.stdout), sys.stderr)


# Port allocation: let Docker do it. No probing for a free port, no deriving
# one from a hash -- publish with an empty host port and read it back via
# `docker port`. Race-free, and no port range to own.
def do_graphql_start(scope: Scope, rev: str) -> None:
    sweep_gql_orphans()
    secrets, config = resolve_graphql_context(scope.repo)
    cname, wsname = f"cld_gql_{scope.session}", f"gql-{scope.session}"

    # start is idempotent: a running server already answers, so return its
    # existing endpoint rather than erroring -- it's what makes the MCP's
    # start_server safe to call without checking status first. Idempotence is
    # per repo, though: a multi-repo ticket asking for a different repo than
    # the running server's own org.cld.gql-repo label must be refused, not
    # handed the wrong repo's endpoint. An unlabeled container (pre-label
    # image) keeps the old always-idempotent behavior.
    if not _capture(["docker", "inspect", cname]).returncode:
        if _out_strict("docker", "inspect", cname, "--format", "{{.State.Running}}") == "true":
            if (gql_repo := _label(cname, "org.cld.gql-repo")) and gql_repo != scope.repo:
                _die(f"denied: graphql server for {scope.session} is already running for repo "
                     f"{gql_repo}, not {scope.repo} -- `graphql stop` it first", 3)
            do_graphql_status(scope, rev)
            return
        # Stopped/crashed leftover from a previous start -- clear it before
        # relaunching so `docker run --name` below doesn't collide.
        _rm_container(cname)
        _forget_workspace(scope.repo, wsname)

    bind = resolve_graphql_bind()

    # -v <repo>:/repo is read-write, as run-tests already does -- `jj
    # workspace add` writes store objects. The server serves from its own
    # workspace and never touches the caller's @ or bookmarks.
    _out_strict(
        "docker", "run", "-d", "--name", cname,
        "--label", f"org.cld.gql-session={scope.session}",
        "--label", f"org.cld.gql-repo={scope.repo}",
        "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        "-v", f"{scope.repo}:/repo", *_secret_mount(secrets),
        "-e", f"REVISION={rev}", "-e", f"PROJECT_SUBDIR={secrets.project_subdir}",
        "-e", f"GQL_WORKSPACE={wsname}", "-e", f"GQL_COMMAND={config.command}",
        "-e", f"GQL_PORT={config.port}",
        "-p", f"{bind}:0:{config.port}",
        _setting("GRAPHQL_IMAGE"),
        stderr=None,
    )

    host_port = _first_line(_out_strict("docker", "port", cname, f"{config.port}/tcp", stderr=None))
    if not (host_port := host_port.rpartition(":")[2]):
        print(f"denied: could not read back the published port for {cname}", file=sys.stderr)
        _rm_container(cname)
        sys.exit(3)

    timeout = int(_setting("GRAPHQL_START_TIMEOUT"))
    deadline = time.monotonic() + timeout
    ready = died = False
    while time.monotonic() < deadline:
        # Check the container is still alive before probing it -- otherwise a
        # crash (bad graphql_command, poetry install failure) just busy-loops
        # the probe against a dead port until the full timeout elapses.
        if _out("docker", "inspect", cname, "--format", "{{.State.Running}}") != "true":
            died = True
            break
        if _probe(f"http://{bind}:{host_port}{config.health_path}", 5):
            ready = True
            break
        time.sleep(2)

    if died:
        print(f"[cld-broker] graphql server for {scope.session} exited before becoming healthy "
              "-- last logs:", file=sys.stderr)
        _dump_logs(cname)
        _rm_container(cname)
        _forget_workspace(scope.repo, wsname)
        sys.exit(3)

    if not ready:
        print(f"[cld-broker] graphql server for {scope.session} did not become healthy within "
              f"{timeout}s -- last logs:", file=sys.stderr)
        _dump_logs(cname)
        do_graphql_stop(scope)
        sys.exit(3)

    do_graphql_status(scope, rev)


# Only start (and restart) needs the caller's revision -- a hard requirement
# there; status takes it softly for the stale field; the other ops never ask.
def action_graphql(caller: Caller, argv: list[str]) -> None:
    name, argv = parse_repo_arg(argv)
    scope = Scope(caller.name, caller.repo(name))
    op, args = (argv[0], argv[1:]) if argv else ("", [])
    match op:
        case "start":
            do_graphql_start(scope, resolve_revision(caller, scope.repo))
        case "stop":
            do_graphql_stop(scope)
            print("stopped")
        case "restart":
            do_graphql_stop(scope)
            do_graphql_start(scope, resolve_revision(caller, scope.repo))
        case "status":
            do_graphql_status(scope, caller.revision(scope.repo))
        case "logs":
            tail = args[0] if args and args[0] else "50"
            if not re.fullmatch(r"[0-9]+", tail):
                _die(f"denied: bad tail count '{tail}'", 2)
            if int(tail) > 2000:  # a debugging aid, not a log export
                tail = "2000"
            logs = _capture(["docker", "logs", "--tail", tail, f"cld_gql_{scope.session}"], stderr=subprocess.STDOUT)
            cap_output(mask_output(logs.stdout), int(_setting("GRAPHQL_OUTPUT_MAX_BYTES")), "tail")
            sys.exit(logs.returncode)
        case "query":
            if len(args) < 2:
                _die("denied: query needs <target> <query-string> [variables-json]", 2)
            do_graphql_query(scope, *args[:3])
        case "introspect":
            do_graphql_query(scope, args[0] if args and args[0] else "local", _GRAPHQL_INTROSPECTION_QUERY)
        case "endpoints":
            do_graphql_endpoints(scope)
        case _:
            _die(f"denied: bad graphql op '{op}'", 2)


# Template for a second action -- copy, rename, point at its image, enable by
# uncommenting. Callable from the container as `cld broker lint …`.
# def action_lint(caller: Caller, argv: list[str]) -> None:
#     repo = caller.repo("")
#     rev, secrets = resolve_test_context(caller, repo)
#     _exec(["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
#            "-v", f"{repo}:/repo", "-e", f"REVISION={rev}",
#            "-e", f"PROJECT_SUBDIR={secrets.project_subdir}",
#            _setting("LINT_IMAGE") or "runlint:latest", *argv])


# ---- dispatch -------------------------------------------------------------

def decode_argv(payload: str) -> list[str]:
    """Arbitrary argv, decoded from base64(NUL-joined argv). Never eval'd, so it
    can only ever become arguments to the action's command."""
    try:
        data = base64.b64decode(payload)
    except ValueError:
        _die("denied: bad argv payload", 2)
    args = data.split(b"\0")
    if args[-1] == b"":
        args.pop()
    return [os.fsdecode(arg) for arg in args]


_SESSION_RE = re.compile(r"cld_[A-Za-z0-9_-]+")


# Resolve the action to an action_* function. The charset check keeps the
# constructed name to [a-z0-9_], and the function must actually exist.
def resolve_action(action: str) -> Callable[[Caller, list[str]], None]:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", action):
        _die("denied: bad action", 2)
    if not callable(fn := globals().get(f"action_{action.replace('-', '_')}")):
        _die(f"denied: unknown action '{action}'", 2)
    return fn


def main_ssh() -> None:
    command = _first_line(os.environ.get("SSH_ORIGINAL_COMMAND", "")).strip(" \t")
    action, session, payload = (re.split(r"[ \t]+", command, maxsplit=2) + ["", ""])[:3]
    fn = resolve_action(action)
    if not _SESSION_RE.fullmatch(session):
        _die("denied: bad session id", 2)

    # The sshd route's caller source: the calling container's host-set labels.
    # The container name == session, and the labels are set at launch (trusted),
    # so the caller cannot point at an arbitrary host path -- only a repo the host
    # mounted for it. Everything past the labels (repo selection, revision,
    # targets) is resolved lazily inside each action_* function, so read-only
    # actions don't pay for -- or fail on -- resolution they don't need.
    fn(LabelCaller.from_session(session), decode_argv(payload))


def _repo_root(path: str) -> str:
    start = Path(path).resolve()
    if not (root := next((str(p) for p in (start, *start.parents) if _is_repo(str(p))), "")):
        _die(f"no repo at or above {start} -- pass -C <repo>", 3)
    return root


def main_cli(argv: list[str]) -> None:
    """The host route (`cld-brokerctl run`): the Caller comes from the command
    line instead of docker labels. Every token after the action goes to it as
    given, so its own flags (pytest's -k, -r, ...) never clash with these."""
    parser = argparse.ArgumentParser(prog="cld-brokerctl run",
                                     description="Run one broker action as the host operator.")
    parser.add_argument("-C", dest="repo", default=".", help="repo to act on (default: the one containing cwd)")
    parser.add_argument("-r", dest="revset", default="@", help="jj revset to act on (default: @, your working copy)")
    parser.add_argument("-s", dest="session", help="session name for the caller's own resources "
                                                   "(default: cld_host_<repo dir>)")
    parser.add_argument("-t", dest="targets", action="append", default=[],
                        help="extra repo the task-agent action may target (repeatable)")
    parser.add_argument("action")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    fn = resolve_action(args.action)
    repo = _repo_root(args.repo)
    session = args.session or "cld_host_" + re.sub(r"[^A-Za-z0-9_-]", "-", Path(repo).name)
    if not _SESSION_RE.fullmatch(session):
        _die(f"denied: bad session id '{session}'", 2)
    fn(CliCaller(session, repo, args.revset, [str(Path(t).resolve()) for t in args.targets]), args.argv)


# sshd's ForceCommand runs this file with no arguments and delivers the client's
# request in $SSH_ORIGINAL_COMMAND; a client cannot add arguments. So arguments
# can only come from a host shell, which makes them a safe route selector.
def main() -> None:
    load_conf()
    if len(sys.argv) > 1:
        main_cli(sys.argv[1:])
    else:
        main_ssh()


if __name__ == "__main__":
    main()
