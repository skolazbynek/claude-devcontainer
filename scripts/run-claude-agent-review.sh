#!/bin/bash
set -e

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
CLD_ROOT="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/lib/docker-common.sh"

parse_name_arg "$@"
set -- "${REMAINING_ARGS[@]}"

if [ $# -ne 2 ]; then
    echo "Usage: $0 [-n|--name <name>] <feature-branch> <trunk-branch>" >&2
    exit 1
fi

FEATURE_BRANCH="$1"
TRUNK_BRANCH="$2"

export SESSION_NAME=$(build_session_name "review" "$CUSTOM_NAME")
TEMPLATE_FILE="$CLD_ROOT/imgs/claude-agent-review/review-template.md"

JJ_ROOT=$(require_jj_root)

cd "$JJ_ROOT"

# Generate diff
DIFF_FILE="review-diff-$SESSION_NAME.patch"
echo "Generating diff: fork_point($FEATURE_BRANCH | $TRUNK_BRANCH) -> $FEATURE_BRANCH"
if ! jj diff --from "fork_point($FEATURE_BRANCH | $TRUNK_BRANCH)" --to "$FEATURE_BRANCH" --git > "$DIFF_FILE" 2>&1; then
    echo "Error: Failed to generate diff" >&2
    rm -f "$DIFF_FILE"
    exit 1
fi

if [ ! -s "$DIFF_FILE" ]; then
    echo "Error: Generated diff is empty" >&2
    exit 1
fi

echo "Diff saved to: $DIFF_FILE"

# Create task file from template
if [ ! -f "$TEMPLATE_FILE" ]; then
    echo "Error: Template not found: $TEMPLATE_FILE" >&2
    exit 1
fi

TASK_FILE="/tmp/review-task-$SESSION_NAME.md"

# Export variables for envsubst
export TRUNK_BRANCH FEATURE_BRANCH
export DIFF_FILE_PATH="/workspace/origin/$DIFF_FILE"
envsubst '$TRUNK_BRANCH $FEATURE_BRANCH $DIFF_FILE_PATH' < "$TEMPLATE_FILE" > "$TASK_FILE"

echo "Task file created: $TASK_FILE"
echo ""

# Call upstream agent (SESSION_NAME is exported, run-claude-agent.sh will pick it up)
UPSTREAM_AGENT="$SCRIPT_DIR/run-claude-agent.sh"
if [ ! -x "$UPSTREAM_AGENT" ]; then
    echo "Error: Upstream run-claude-agent.sh not found or not executable: $UPSTREAM_AGENT" >&2
    exit 1
fi

exec "$UPSTREAM_AGENT" "$TASK_FILE"
