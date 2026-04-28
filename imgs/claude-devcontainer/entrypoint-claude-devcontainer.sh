#!/bin/bash
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

# Copy host nvim config/data into container HOME so nvim can write freely
# without persisting back to the host. Source dirs are RO-mounted by the
# devcontainer launcher under /tmp/nvim-host/.
NVIM_HOST_DIR="/tmp/nvim-host"
if [ -d "$NVIM_HOST_DIR" ]; then
    mkdir -p "$HOME/.config" "$HOME/.local/share" "$HOME/.local/state" "$HOME/.cache"
    [ -d "$NVIM_HOST_DIR/config" ] && cp -aT "$NVIM_HOST_DIR/config" "$HOME/.config/nvim"
    [ -d "$NVIM_HOST_DIR/share" ]  && cp -aT "$NVIM_HOST_DIR/share"  "$HOME/.local/share/nvim"
    [ -d "$NVIM_HOST_DIR/state" ]  && cp -aT "$NVIM_HOST_DIR/state"  "$HOME/.local/state/nvim"
    [ -d "$NVIM_HOST_DIR/cache" ]  && cp -aT "$NVIM_HOST_DIR/cache"  "$HOME/.cache/nvim"
fi

BOOKMARK="${SESSION_NAME:?SESSION_NAME must be set}"

# Detect VCS type (jj or git)
detect_vcs || exit 1

echo "Using $VCS_TYPE repository at: $WORKSPACE_ORIGIN"

WORKSPACE_REV="${AGENT_REVISION:-}"
if [ -z "$WORKSPACE_REV" ]; then
    if [ "$VCS_TYPE" = "jj" ]; then
        WORKSPACE_REV="@"
    else
        WORKSPACE_REV="HEAD"
    fi
fi

cd "$WORKSPACE_ORIGIN"
vcs_create_workspace "$BOOKMARK" "$WORKSPACE_CURRENT" "$WORKSPACE_REV"

cd "$WORKSPACE_CURRENT"

# For jj, create a bookmark at the current change.
# For git, the branch is already created by worktree add.
if [ "$VCS_TYPE" = "jj" ]; then
    jj bookmark create -r @ "$BOOKMARK"
fi

build_claude_config

# Install project dependencies (MCP orchestrator, etc.)
if command -v poetry &>/dev/null; then
    while IFS= read -r pyproject; do
        project_dir=$(dirname "$pyproject")
        echo "[INFO] Installing poetry packages in $project_dir"
        (cd "$project_dir" && poetry install --no-interaction -q >/dev/null 2>&1) || \
            echo "[WARN] poetry install failed in $project_dir (continuing)"
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
vcs_forget_workspace "$BOOKMARK" "$WORKSPACE_CURRENT"
