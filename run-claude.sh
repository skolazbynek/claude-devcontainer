#!/bin/bash
set -e

# Configuration
IMAGE_NAME="claude-code-safe"
CONTAINER_USER="claude"
WORKSPACE_BASE="/workspace"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    log_error "Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if image exists, if not build it
if [[ "$(docker images -q $IMAGE_NAME 2> /dev/null)" == "" ]]; then
    log_info "Image '$IMAGE_NAME' not found. Building..."
    docker build -t $IMAGE_NAME .
    log_info "Image built successfully."
fi

# Load environment variables from .env if it exists (for optional CLAUDE_REPOS config)
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs 2>/dev/null)
fi

# Build docker run command with base options
DOCKER_ARGS=(
    "-it"
    "--rm"
    "--cap-drop=ALL"	# security
    "--security-opt=no-new-privileges"	# security
    "--cpus=2.0"
    "--memory=4g"
    "--user" "$(id -u):$(id -g)"
    "-e" "HOME=/home/$CONTAINER_USER"	# env $HOME
)

# Hardcoded list of paths to mount from host home directory
# Each path will be mounted from ~/.{path} on host to ~/.{path} on container
# Symlinks will be resolved before mounting
MOUNT_PATHS=(
    ".claude"
    ".claude.json"
    ".gitconfig"
    # specify .config/nvim to resolve symlink
    ".config/nvim"
    ".cache/nvim"
    ".config"
    ".local/share/nvim"
    ".local/state/nvim"
    ".bashrc"
)

# Iterate over mount paths and add them to docker args
log_info "Mounting configured paths from host to container..."
for rel_path in "${MOUNT_PATHS[@]}"; do
    host_path="$HOME/$rel_path"
    container_path="/home/$CONTAINER_USER/$rel_path"

    # Check if path exists
    if [ -e "$host_path" ]; then
        # Resolve symlinks to get actual path
        resolved_path=$(readlink -f "$host_path" 2>/dev/null || realpath "$host_path" 2>/dev/null || echo "$host_path")

        DOCKER_ARGS+=("-v" "$resolved_path:$container_path")
        log_info "  Mounted: $rel_path -> $resolved_path"
    else
        log_warn "  Skipped: $rel_path (not found)"
    fi
done

# Mount current directory as workspace
CURRENT_DIR=$(pwd)
DOCKER_ARGS+=(
    "-v" "/etc/ssl/certs:/etc/ssl/certs"
    "-v" "$CURRENT_DIR:$WORKSPACE_BASE/origin"
    "-w" "$WORKSPACE_BASE/current"
)

# Add image name
DOCKER_ARGS+=("$IMAGE_NAME")

# Pass through any additional arguments to Claude
if [ $# -gt 0 ]; then
    DOCKER_ARGS+=("$@")
fi

# Run the container
log_info "Starting Claude Code in container..."
log_info "Working directory: $WORKSPACE_BASE/current"
echo ""

docker run "${DOCKER_ARGS[@]}"
