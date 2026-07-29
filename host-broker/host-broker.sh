#!/usr/bin/env bash
# cld host broker -- the sshd ForceCommand target for the restricted broker key.
# sshd ignores whatever command the client asked for and delivers it in
# $SSH_ORIGINAL_COMMAND, which must have the shape:
#
#     <action> <session> <base64-argv>
#
# The broker serves ANY repo that has a running master -- no per-repo config,
# no whitelist. It resolves the target repo from the calling master container's
# host-set `org.cld.repo-root` label (established at launch, not caller input),
# so the caller controls only: the action, a validated master session id, and
# the decoded argv. Nothing is ever eval'd.
#
# ADD AN ACTION: define a function named `action_<name>` (hyphens in <name>
# become underscores, so `run-tests` -> `action_run_tests`). It receives the
# decoded argv as "$@" and may use the shared context prepared by the
# dispatcher: $REPO (host repo path), $REV (session's current change),
# $SECRETS_ENV_FILE (may not exist), $PROJECT_SUBDIR, and $RUNTESTS_IMAGE.
set -euo pipefail

CONF="${CLD_BROKER_CONF:-/etc/cld/host-broker.conf}"
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"

: "${RUNTESTS_IMAGE:=runtests:latest}"

# Read a single quoted-string key from a flat cld TOML config (e.g. .cld/config.toml).
# Empty if the file or key is absent. Avoids a TOML dependency in the broker.
cld_conf_get() {
    local file="$1" key="$2"
    [ -r "$file" ] || return 0
    sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$file" | head -n1
}

# ---- actions --------------------------------------------------------------
# Each runs on the host as your user and receives the decoded pytest/tool argv
# as "$@". `exec` so the child's stdout/stderr/exit code flow straight back.

action_run_tests() {
    local secret_args=()
    [ -f "$SECRETS_ENV_FILE" ] && secret_args=(-v "$SECRETS_ENV_FILE:/secrets/.env:ro")
    exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$REPO:/repo" "${secret_args[@]}" \
        -e "REVISION=$REV" -e "PROJECT_SUBDIR=$PROJECT_SUBDIR" \
        "$RUNTESTS_IMAGE" "$@"
}

# Template for a second action -- copy, rename, point at its image, enable by
# uncommenting. Callable from the container as `host-run --action lint …`.
# action_lint() {
#     exec docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
#         -v "$REPO:/repo" -e "REVISION=$REV" -e "PROJECT_SUBDIR=$PROJECT_SUBDIR" \
#         "${LINT_IMAGE:-runlint:latest}" "$@"
# }

# ---- dispatch -------------------------------------------------------------
read -r action session payload <<<"${SSH_ORIGINAL_COMMAND:-}"

# Resolve the action to an action_* function. The charset check keeps the
# constructed name to [a-z0-9_], and the function must actually exist.
[[ "$action" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "denied: bad action" >&2; exit 2; }
fn="action_${action//-/_}"
declare -F "$fn" >/dev/null || { echo "denied: unknown action '$action'" >&2; exit 2; }

[[ "$session" =~ ^cld_master_[A-Za-z0-9_-]+$ ]] || { echo "denied: bad session id" >&2; exit 2; }

# Resolve the target repo from the calling master container's host-set label.
# The container name == session, and the label is set at launch (trusted), so
# the caller cannot point at an arbitrary host path -- only a real master's repo.
REPO=$(docker inspect "$session" --format '{{index .Config.Labels "org.cld.repo-root"}}' 2>/dev/null) || true
[ -n "$REPO" ] && { [ -d "$REPO/.jj" ] || [ -d "$REPO/.git" ]; } \
    || { echo "no master/repo for session $session" >&2; exit 3; }

# Shared context: resolve the session's current change (store-reading only, per
# design decision 5 -- never moves the store's working copy).
REV=$(jj -R "$REPO" log --no-graph -n1 -r "$session" -T commit_id) \
    || { echo "cannot resolve revision for $session" >&2; exit 3; }

# Project subdirectory: `pyproject_dir` in the repo's own cld config (default
# "."), relative to the repo root. Holds both pyproject.toml and .env; missing
# .env is fine -- the runner just runs without it.
PROJECT_SUBDIR=$(cld_conf_get "$REPO/.cld/config.toml" pyproject_dir)
: "${PROJECT_SUBDIR:=.}"
case "$PROJECT_SUBDIR" in
    /*) SECRETS_ENV_FILE="$PROJECT_SUBDIR/.env" ;;
    .)  SECRETS_ENV_FILE="$REPO/.env" ;;
    *)  SECRETS_ENV_FILE="$REPO/$PROJECT_SUBDIR/.env" ;;
esac

# Arbitrary argv, decoded from base64(NUL-joined argv). Never eval'd, so it can
# only ever become arguments to the action's command.
args=()
[ -n "${payload:-}" ] && mapfile -d '' args < <(printf %s "$payload" | base64 -d)

"$fn" "${args[@]}"
