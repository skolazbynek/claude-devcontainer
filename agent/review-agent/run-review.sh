#!/bin/bash
set -e

# Check arguments
if [ $# -ne 2 ]; then
    echo "Usage: $0 <feature-branch> <trunk-branch>" >&2
    exit 1
fi

FEATURE_BRANCH="$1"
TRUNK_BRANCH="$2"
AGENT_NAME="review_$RANDOM"
CURRENT_DIR="$(pwd)"

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
echo "Generating diff: $TRUNK_BRANCH -> $FEATURE_BRANCH"
jj diff --from "$TRUNK_BRANCH" --to "fork_point($TRUNK_BRANCH | $FEATURE_BRANCH)" --git > "$DIFF_FILE"

if [ ! -s "$DIFF_FILE" ]; then
    echo "Error: Generated diff is empty" >&2
    exit 1
fi

echo "Diff saved to: $DIFF_FILE"

# Create task file from template
TEMPLATE_FILE="$HOME/.config/claude/templates/merge-review.md"
if [ ! -f "$TEMPLATE_FILE" ]; then
    echo "Error: Template not found: $TEMPLATE_FILE" >&2
    exit 1
fi

TASK_FILE="/tmp/review-task-$AGENT_NAME.md"

# Read template, skip line 2 (the diff generation instruction), replace variables
{
    head -n 1 "$TEMPLATE_FILE"
    tail -n +3 "$TEMPLATE_FILE" | sed "s/\${TRUNK_BRANCH}/$TRUNK_BRANCH/g" | sed "s/\${FEATURE_BRANCH}/$FEATURE_BRANCH/g"
    echo ""
    echo "# Input"
    echo ""
    echo "The diff has been generated and saved to \`/workspace/origin/$DIFF_FILE\`. Read this file to perform the review."
    echo ""
    echo "# Output Location"
    echo ""
    echo "Write your review findings to \`review-output.md\` in the repository root directory."
} > "$TASK_FILE"

echo "Task file created: $TASK_FILE"
echo ""

# Call upstream agent
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
exec "$SCRIPT_DIR/../run-agent.sh" "$TASK_FILE"
