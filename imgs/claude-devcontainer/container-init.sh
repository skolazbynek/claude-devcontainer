# Shared container initialization. Source this from entrypoints.

export WORKSPACE_ORIGIN="/workspace/origin"
export WORKSPACE_CURRENT="/workspace/current"

mkdir -p /tmp/bin
export PATH="/tmp/bin:$PATH"

# Wrap mysql to use mounted credentials file automatically
if [ -n "${MYSQL_DEFAULTS_FILE:-}" ] && [ -f "$MYSQL_DEFAULTS_FILE" ]; then
    MYSQL_BIN=$(which mysql)
    printf '#!/bin/bash\nexec %s --defaults-extra-file=%s "$@"\n' "$MYSQL_BIN" "$MYSQL_DEFAULTS_FILE" > /tmp/bin/mysql
    chmod +x /tmp/bin/mysql
fi

# Copy staged host config tree into $HOME.
# Every RO $HOME mount is staged under /tmp/host-config/<rel> by the launcher;
# we overlay that tree onto $HOME so the container has writable copies and
# Docker never creates $HOME subdirs as root.
copy_host_configs() {
    local src_root="/tmp/host-config"
    [ -d "$src_root" ] || return 0
    cp -aT "$src_root" "$HOME"
    chmod -R u+w "$HOME" 2>/dev/null || true
}

# Build container-local claude.json from read-only host config.
# Merges global and host-project MCP servers into user scope (top-level mcpServers)
# so they're available regardless of which project directory claude runs in.
build_claude_config() {
    local host_config="/tmp/host-config/.claude.json"
    [ -f "$host_config" ] || return 0

    if ! command -v jq &>/dev/null; then
        cp "$host_config" "$HOME/.claude.json"
        return 0
    fi

    local global_mcp host_mcp
    global_mcp=$(jq -c '.mcpServers // {}' "$host_config" 2>/dev/null || echo '{}')
    host_mcp='{}'
    if [ -n "${HOST_PROJECT_DIR:-}" ]; then
        host_mcp=$(jq -c --arg p "$HOST_PROJECT_DIR" '.projects[$p].mcpServers // {}' "$host_config" 2>/dev/null || echo '{}')
    fi

    if jq --argjson g "$global_mcp" --argjson h "$host_mcp" \
       '.mcpServers = ($g + $h)' \
       "$host_config" > "$HOME/.claude.json" 2>/dev/null; then
        echo "MCP servers configured (user scope)"
    else
        cp "$host_config" "$HOME/.claude.json"
    fi

    # Rewrite baked-in MCP servers to use container paths
    local rewrites='{}'
    if jq -e '.mcpServers.orchestrator' "$HOME/.claude.json" &>/dev/null; then
        rewrites=$(echo "$rewrites" | jq '.orchestrator = {
            "type": "stdio",
            "command": "python3",
            "args": ["/opt/cld/cld/mcp/orchestrator.py"]
        }')
    fi
    if jq -e '.mcpServers["graphql-tester"]' "$HOME/.claude.json" &>/dev/null; then
        rewrites=$(echo "$rewrites" | jq '.["graphql-tester"] = {
            "type": "stdio",
            "command": "python3",
            "args": ["/opt/cld/cld/mcp/graphql.py"]
        }')
    fi
    if [ "$rewrites" != '{}' ]; then
        jq --argjson r "$rewrites" '.mcpServers += $r' \
            "$HOME/.claude.json" > /tmp/claude-json-tmp && \
            mv /tmp/claude-json-tmp "$HOME/.claude.json"
        echo "MCP servers rewritten for container: $(echo "$rewrites" | jq -r 'keys | join(", ")')"
    fi
}
