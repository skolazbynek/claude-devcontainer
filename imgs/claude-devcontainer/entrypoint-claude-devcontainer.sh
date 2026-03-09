#!/bin/bash
source /workspace/container-init.sh

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

# /workspace/origin is guaranteed to be the jj root by run-claude.sh
ORIGIN_DIR="/workspace/origin"

if [ ! -d "$ORIGIN_DIR/.jj" ]; then
    echo "Error: No jj repository found at $ORIGIN_DIR"
    exit 1
fi

echo "Using jj repository at: $ORIGIN_DIR"

cd "$ORIGIN_DIR"
jj workspace add --name $BOOKMARK -r @ /workspace/current

cd /workspace/current
jj bookmark create -r @ $BOOKMARK

# Wrap claude to always pass --dangerously-skip-permissions
CLAUDE_BIN=$(which claude)
mkdir -p /tmp/bin
printf '#!/bin/bash\nexec %s --dangerously-skip-permissions "$@"\n' "$CLAUDE_BIN" > /tmp/bin/claude
chmod +x /tmp/bin/claude
export PATH="/tmp/bin:$PATH"

/bin/bash

# Cleanup when shell exits
jj workspace forget
