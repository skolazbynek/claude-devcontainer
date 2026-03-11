#!/bin/bash
source /workspace/container-init.sh

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

if [ ! -d "$WORKSPACE_ORIGIN/.jj" ]; then
    echo "Error: No jj repository found at $WORKSPACE_ORIGIN"
    exit 1
fi

echo "Using jj repository at: $WORKSPACE_ORIGIN"

cd "$WORKSPACE_ORIGIN"
jj workspace add --name "$BOOKMARK" -r @ "$WORKSPACE_CURRENT"

cd "$WORKSPACE_CURRENT"
jj bookmark create -r @ "$BOOKMARK"

build_claude_config
copy_staged_files

# Install project dependencies (MCP orchestrator, etc.)
if [ -f "$WORKSPACE_CURRENT/pyproject.toml" ] && command -v poetry &>/dev/null; then
    echo "Installing project dependencies..."
    poetry install --no-interaction --quiet -C "$WORKSPACE_CURRENT" 2>/dev/null || true
fi

# Wrap claude to always pass --dangerously-skip-permissions
CLAUDE_BIN=$(which claude)
printf '#!/bin/bash\nexec %s --dangerously-skip-permissions "$@"\n' "$CLAUDE_BIN" > /tmp/bin/claude
chmod +x /tmp/bin/claude

/bin/bash

# Cleanup when shell exits
jj workspace forget
