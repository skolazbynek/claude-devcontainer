#!/bin/bash
source /workspace/container-init.sh

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

if [ ! -d "$WORKSPACE_ORIGIN/.jj" ]; then
    echo "Error: No jj repository found at $WORKSPACE_ORIGIN"
    exit 1
fi

echo "Using jj repository at: $WORKSPACE_ORIGIN"

WORKSPACE_REV="${AGENT_REVISION:-@}"

cd "$WORKSPACE_ORIGIN"
jj workspace add --name "$BOOKMARK" -r "$WORKSPACE_REV" "$WORKSPACE_CURRENT"

cd "$WORKSPACE_CURRENT"
jj bookmark create -r @ "$BOOKMARK"

build_claude_config

# Install project dependencies (MCP orchestrator, etc.)
if command -v poetry &>/dev/null; then
    while IFS= read -r pyproject; do
        project_dir=$(dirname "$pyproject")
        echo "Installing poetry dependencies in $project_dir..."
        (cd "$project_dir" && poetry install --no-interaction) || \
            echo "poetry install failed in $project_dir (continuing)"
    done < <(find "$WORKSPACE_CURRENT" -maxdepth 3 -name pyproject.toml \
        -not -path '*/.*' -not -path '*/node_modules/*' -not -path '*/.venv/*' 2>/dev/null)
fi

# Wrap claude to always pass --dangerously-skip-permissions (and --model if set)
CLAUDE_BIN=$(which claude)
CLAUDE_EXTRA_ARGS="--dangerously-skip-permissions"
if [ -n "${AGENT_MODEL:-}" ]; then
    CLAUDE_EXTRA_ARGS="$CLAUDE_EXTRA_ARGS --model $AGENT_MODEL"
fi
printf '#!/bin/bash\nexec %s %s "$@"\n' "$CLAUDE_BIN" "$CLAUDE_EXTRA_ARGS" > /tmp/bin/claude
chmod +x /tmp/bin/claude

/bin/bash

# Cleanup when shell exits
jj workspace forget
