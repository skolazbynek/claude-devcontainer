#!/bin/bash
set -e

# Check arguments
if [ $# -ne 2 ]; then
    echo "Usage: $0 <feature-branch> <trunk-branch>" >&2
    exit 1
fi

FEATURE_BRANCH="$1"
TRUNK_BRANCH="$2"
export AGENT_NAME="review_$RANDOM"
CURRENT_DIR="$(pwd)"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
TEMPLATE_FILE="/home/zet/.config/claude/templates/review-template.md"

# Find jj repository root
find_jj_root() {
    local dir="$1"
    while [ "$dir" != "/" ]; do
        if [ -d "$dir/.jj" ]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

JJ_ROOT=$(find_jj_root "$CURRENT_DIR")

if [ -z "$JJ_ROOT" ]; then
    echo "Error: No jj repository found" >&2
    exit 1
fi

cd "$JJ_ROOT"

# Generate diff
DIFF_FILE="review-diff-$AGENT_NAME.patch"
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

TASK_FILE="/tmp/review-task-$AGENT_NAME.md"

# Export variables for envsubst
export TRUNK_BRANCH FEATURE_BRANCH
export DIFF_FILE_PATH="/workspace/origin/$DIFF_FILE"
envsubst '$TRUNK_BRANCH $FEATURE_BRANCH $DIFF_FILE_PATH' < "$TEMPLATE_FILE" > "$TASK_FILE"

echo "Task file created: $TASK_FILE"
echo ""

# Call upstream agent
UPSTREAM_AGENT="$SCRIPT_DIR/../run-agent.sh"
if [ ! -x "$UPSTREAM_AGENT" ]; then
    echo "Error: Upstream run-agent.sh not found or not executable: $UPSTREAM_AGENT" >&2
    exit 1
fi

exec "$UPSTREAM_AGENT" "$TASK_FILE"
