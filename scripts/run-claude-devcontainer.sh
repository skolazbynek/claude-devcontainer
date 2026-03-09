#!/bin/bash
set -e

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
source "$SCRIPT_DIR/lib/docker-common.sh"

IMAGE_NAME="claude-devcontainer:latest"

require_docker
ensure_image "$IMAGE_NAME" "imgs/claude-devcontainer/Dockerfile.claude-devcontainer" "imgs/claude-devcontainer"
load_dotenv

JJ_ROOT=$(require_jj_root)

# Build docker args
DOCKER_ARGS=("-it")
build_base_args DOCKER_ARGS
build_workspace_args DOCKER_ARGS "$JJ_ROOT"
build_claude_config_args DOCKER_ARGS
build_mysql_args DOCKER_ARGS

# Mount additional host paths (neovim, gitconfig, bashrc)
EXTRA_MOUNT_PATHS=(
    ".gitconfig"
    ".config/nvim"
    ".cache/nvim"
    ".local/share/nvim"
    ".local/state/nvim"
    ".bashrc"
)

log_info "Mounting extra host paths..."
for rel_path in "${EXTRA_MOUNT_PATHS[@]}"; do
    host_path="$HOME/$rel_path"
    if [ -e "$host_path" ]; then
        resolved_path=$(readlink -f "$host_path" 2>/dev/null || echo "$host_path")
        DOCKER_ARGS+=("-v" "$resolved_path:$CONTAINER_HOME/$rel_path")
        log_info "  Mounted: $rel_path"
    else
        log_warn "  Skipped: $rel_path (not found)"
    fi
done

parse_name_arg "$@"
build_session_args DOCKER_ARGS "$(build_session_name "cld" "$CUSTOM_NAME")"

DOCKER_ARGS+=("$IMAGE_NAME" "${REMAINING_ARGS[@]}")

log_info "Starting Claude Code in container..."
log_info "Working directory: $WORKSPACE_BASE/current"
echo ""

docker run "${DOCKER_ARGS[@]}"
