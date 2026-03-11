#!/bin/bash
set -e

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
CLD_ROOT="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/lib/docker-common.sh"

IMAGE_NAME="claude-devcontainer:latest"

require_docker
ensure_image "$IMAGE_NAME" "$CLD_ROOT/imgs/claude-devcontainer/Dockerfile.claude-devcontainer" "$CLD_ROOT"
load_dotenv

JJ_ROOT=$(require_jj_root)

DOCKER_ARGS=("-it")
build_base_args DOCKER_ARGS
build_workspace_args DOCKER_ARGS "$JJ_ROOT"
build_claude_config_args DOCKER_ARGS
build_mysql_args DOCKER_ARGS
build_docker_socket_args DOCKER_ARGS "$JJ_ROOT"

# Direct ro mounts: config files that don't need writes
DIRECT_RO=(".gitconfig" ".config/nvim" ".bashrc")
# Staged mounts: copied into $HOME at startup so container can write without affecting host
STAGED=(".cache/nvim" ".local/share/nvim" ".local/state/nvim")

log_info "Mounting extra host paths..."
for rel_path in "${DIRECT_RO[@]}"; do
    mount_home_path DOCKER_ARGS "$rel_path" "$CONTAINER_HOME/$rel_path:ro" && \
        log_info "  $rel_path (ro)" || log_warn "  $rel_path (not found)"
done
for rel_path in "${STAGED[@]}"; do
    mount_home_path DOCKER_ARGS "$rel_path" "/tmp/host-files/$rel_path:ro" && \
        log_info "  $rel_path (staged)" || log_warn "  $rel_path (not found)"
done

parse_name_arg "$@"
build_session_args DOCKER_ARGS "$(build_session_name "cld" "$CUSTOM_NAME")"

DOCKER_ARGS+=("$IMAGE_NAME" "${REMAINING_ARGS[@]}")

log_info "Starting Claude Code in container..."
log_info "Working directory: $WORKSPACE_BASE/current"
echo ""

docker run "${DOCKER_ARGS[@]}"
