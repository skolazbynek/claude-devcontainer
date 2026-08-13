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

# The broker client is `cld broker <action>` (cld/broker.py), not a wrapper script:
# one implementation of the ssh call, shared by the CLI and by everything that
# reaches the host through it. CLD_BROKER_ENDPOINT is [user@]host:port (default
# user: zet). See docs/design-cld-broker.md.
if [ -n "${CLD_BROKER_ENDPOINT:-}" ] && [ -f /run/secrets/broker-key ]; then
    echo "[INFO] cld broker ready: run tests via 'cld broker run-tests <pytest args>'"
fi

# Symlink workspace-root files (e.g., .env) from origin into workspace.
# WORKSPACE_FILES is colon-separated, e.g., ".env:.envrc"
link_workspace_files() {
    local files="${WORKSPACE_FILES:-}"
    [ -z "$files" ] && return 0

    local IFS=':'
    for file in $files; do
        if [ -f "$WORKSPACE_ORIGIN/$file" ]; then
            ln -sf "$WORKSPACE_ORIGIN/$file" "$WORKSPACE_CURRENT/$file"
            echo "[INFO] Linked $file from origin"
        else
            echo "[WARN] $file not found in origin, skipping"
        fi
    done
}

# Create this container's own mailbox dirs under the shared mailbox mount.
# No-op if the mailbox tree isn't mounted (plain devcontainer / one-shot agent sessions).
ensure_own_mailbox() {
    local mailbox_base="/var/cld/mailboxes"
    [ -d "$mailbox_base" ] || return 0
    local name="${SESSION_NAME:?SESSION_NAME must be set}"
    if ! mkdir -p "$mailbox_base/$name/tmp" "$mailbox_base/$name/inbox" "$mailbox_base/$name/archive" 2>/tmp/mailbox-mkdir-err; then
        cat /tmp/mailbox-mkdir-err >&2
        echo "[ERROR] Could not write to $mailbox_base (running as uid=$(id -u) gid=$(id -g))." >&2
        echo "        The host directory bind-mounted here is missing, stale, or owned by someone" >&2
        echo "        else. From the HOST (not this container), find its source path and fix it:" >&2
        echo "          docker inspect $name --format '{{range .Mounts}}{{if eq .Destination \"$mailbox_base\"}}{{.Source}}{{end}}{{end}}'" >&2
        echo "          chown -R $(id -u):$(id -g) <path printed above>" >&2
        return 1
    fi
}

# Seed ~/.ssh/known_hosts from the repo's SSH remote.
# Containers get the ssh-agent forwarded but ship no known_hosts, so a first
# `jj git push` / `git push` fails with "Host key verification failed" -- a trust
# gap, not a reachability one. Best-effort: no remote, an https remote, or no
# network all just skip (the push, if any, will report the real problem).
seed_known_hosts() {
    local url host
    url=$(jj git remote list 2>/dev/null | awk '$1 == "origin" {print $2; exit}')
    [ -n "$url" ] || url=$(git -C "$WORKSPACE_ORIGIN" remote get-url origin 2>/dev/null)
    case "$url" in
        *@*:*)      host="${url#*@}"; host="${host%%:*}" ;;
        ssh://*)    host="${url#ssh://}"; host="${host#*@}"; host="${host%%[:/]*}" ;;
        *)          return 0 ;;
    esac
    [ -n "$host" ] || return 0
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    # -f pins the file: ssh-keygen otherwise resolves ~ from the passwd database
    # rather than $HOME, so the already-seeded check would read the wrong file.
    if [ -f ~/.ssh/known_hosts ] && ssh-keygen -F "$host" -f ~/.ssh/known_hosts >/dev/null 2>&1; then
        return 0
    fi
    if ssh-keyscan -t rsa,ecdsa,ed25519 "$host" 2>/dev/null >> ~/.ssh/known_hosts; then
        chmod 600 ~/.ssh/known_hosts
        echo "[cld] seeded ~/.ssh/known_hosts for $host"
    else
        echo "[WARN] could not ssh-keyscan $host -- a push to it may fail host-key verification" >&2
    fi
}

# Copy staged host config tree into $HOME.
# Every RO $HOME mount is staged under /tmp/host-config/<rel> by the launcher;
# we overlay that tree onto $HOME so the container has writable copies and
# Docker never creates $HOME subdirs as root.
copy_host_configs() {
    local src_root="/tmp/host-config"
    [ -d "$src_root" ] || return 0
    cp -aT "$src_root" "$HOME"
    # Only chmod the paths we just staged, not the whole $HOME (~635M of baked caches).
    (cd "$src_root" && find . -mindepth 1 -print0) | \
        (cd "$HOME" && xargs -0 -r chmod u+w) 2>/dev/null || true
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
    if jq -e '.mcpServers["graphql-tester"]' "$HOME/.claude.json" &>/dev/null; then
        rewrites=$(echo "$rewrites" | jq '.["graphql-tester"] = {
            "type": "stdio",
            "command": "python3",
            "args": ["/opt/cld/cld/mcp/graphql.py"]
        }')
    fi
    if jq -e '.mcpServers.messenger' "$HOME/.claude.json" &>/dev/null; then
        rewrites=$(echo "$rewrites" | jq '.messenger = {
            "type": "stdio",
            "command": "python3",
            "args": ["/opt/cld/cld/mcp/messenger.py"]
        }')
    fi
    if [ "$rewrites" != '{}' ]; then
        jq --argjson r "$rewrites" '.mcpServers += $r' \
            "$HOME/.claude.json" > /tmp/claude-json-tmp && \
            mv /tmp/claude-json-tmp "$HOME/.claude.json"
        echo "MCP servers rewritten for container: $(echo "$rewrites" | jq -r 'keys | join(", ")')"
    fi
}
