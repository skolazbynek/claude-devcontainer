# Shared Docker container setup for Claude launcher scripts.
# Source this file; do not execute directly.

[[ -n "${_DOCKER_COMMON_LOADED:-}" ]] && return
_DOCKER_COMMON_LOADED=1

CONTAINER_USER="claude"
CONTAINER_HOME="/home/$CONTAINER_USER"
WORKSPACE_BASE="/workspace"

# --- Logging ---

_RED='\033[0;31m'
_GREEN='\033[0;32m'
_YELLOW='\033[1;33m'
_NC='\033[0m'

log_info()  { echo -e "${_GREEN}[INFO]${_NC} $1"; }
log_warn()  { echo -e "${_YELLOW}[WARN]${_NC} $1"; }
log_error() { echo -e "${_RED}[ERROR]${_NC} $1"; }

# --- Utilities ---

require_jj_root() {
    local dir
    dir="$(pwd)"
    while [ "$dir" != "/" ]; do
        if [ -d "$dir/.jj" ]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    log_error "No jj repository found"
    exit 1
}

# Build a session name from prefix and optional custom suffix.
# Falls back to $RANDOM if no suffix given.
build_session_name() {
    local prefix="$1"
    echo "${prefix}_${2:-$RANDOM}"
}

# Parse common args from CLI. Sets CUSTOM_NAME, AGENT_MODEL, AGENT_REVISION, and REMAINING_ARGS.
parse_name_arg() {
    CUSTOM_NAME=""
    AGENT_MODEL=""
    AGENT_REVISION=""
    REMAINING_ARGS=()
    while [[ $# -gt 0 ]]; do
        case $1 in
            -n|--name) CUSTOM_NAME="$2"; shift 2 ;;
            -m|--model) AGENT_MODEL="$2"; shift 2 ;;
            -r|--revision) AGENT_REVISION="$2"; shift 2 ;;
            *) REMAINING_ARGS+=("$1"); shift ;;
        esac
    done
}

require_docker() {
    if ! command -v docker &>/dev/null; then
        log_error "Docker is not installed."
        exit 1
    fi
}

ensure_image() {
    local image="$1"
    local dockerfile="$2"
    local context="$3"
    if [[ "$(docker images -q "$image" 2>/dev/null)" == "" ]]; then
        log_info "Image '$image' not found. Building..."
        docker build -f "$dockerfile" -t "$image" "$context"
        log_info "Image built successfully."
    fi
}

load_dotenv() {
    if [ -f .env ]; then
        export $(grep -v '^#' .env | xargs 2>/dev/null)
    fi
}

# --- Path translation ---
# When running inside a container (HOST_PROJECT_DIR / HOST_HOME set),
# docker volume mounts must reference host paths since the daemon runs on the host.

to_host_path() {
    local path="$1"
    if [ -n "${HOST_PROJECT_DIR:-}" ]; then
        path="${path/#\/workspace\/current/$HOST_PROJECT_DIR}"
        path="${path/#\/workspace\/origin/$HOST_PROJECT_DIR}"
    fi
    if [ -n "${HOST_HOME:-}" ]; then
        path="${path/#$CONTAINER_HOME/$HOST_HOME}"
    fi
    echo "$path"
}

# --- Docker arg builders ---

# Mount a path relative to $HOME into the container. Returns 1 if source doesn't exist.
mount_home_path() {
    local -n _args=$1
    local rel_path="$2"
    local target="$3"
    local host_path="$HOME/$rel_path"
    [ -e "$host_path" ] || return 1
    local resolved
    resolved=$(readlink -f "$host_path" 2>/dev/null || echo "$host_path")
    _args+=("-v" "$resolved:$target")
}

build_base_args() {
    local -n _args=$1
    _args+=(
        "--rm"
        "--cap-drop=ALL"
        "--security-opt=no-new-privileges"
        "--cpus=2.0"
        "--memory=4g"
        "--user" "$(id -u):$(id -g)"
        "-e" "HOME=$CONTAINER_HOME"
    )
}

build_workspace_args() {
    local -n _args=$1
    local mount_dir="$2"
    local host_mount_dir
    host_mount_dir=$(to_host_path "$mount_dir")
    _args+=(
        "-v" "/etc/ssl/certs:/etc/ssl/certs:ro"
        "-v" "$host_mount_dir:$WORKSPACE_BASE/origin"
        "-w" "$WORKSPACE_BASE/current"
        "-e" "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"
    )
}

build_claude_config_args() {
    local -n _args=$1
    local host_home
    host_home=$(to_host_path "$HOME")
    _args+=(
        "-v" "$host_home/.claude:$CONTAINER_HOME/.claude:rw"
        "-v" "$host_home/.claude.json:/tmp/host-claude.json:ro"
        "-v" "$host_home/.config:$CONTAINER_HOME/.config:ro"
    )
}

build_docker_socket_args() {
    local -n _args=$1
    local jj_root="$2"
    local docker_sock="/var/run/docker.sock"
    if [ -S "$docker_sock" ]; then
        local docker_gid
        docker_gid=$(stat -c '%g' "$docker_sock")
        _args+=(
            "-v" "$docker_sock:$docker_sock"
            "--group-add" "$docker_gid"
            "-e" "HOST_PROJECT_DIR=$jj_root"
            "-e" "HOST_HOME=$HOME"
        )
        log_info "Docker socket mounted (orchestrator support)"
    else
        log_warn "Docker socket not found, orchestrator agent lifecycle tools unavailable"
    fi
}

build_session_args() {
    local -n _args=$1
    local session_name="$2"
    _args+=("-e" "SESSION_NAME=$session_name")
    log_info "Session name: $session_name"
}

build_mysql_args() {
    local -n _args=$1
    if [ -n "${MYSQL_CONFIG:-}" ]; then
        if [ -f "$MYSQL_CONFIG" ]; then
            local resolved
            resolved=$(realpath "$MYSQL_CONFIG")
            _args+=("-v" "$resolved:/run/secrets/mysql.cnf:ro")
            _args+=("-e" "MYSQL_DEFAULTS_FILE=/run/secrets/mysql.cnf")
            log_info "MySQL config mounted from: $resolved"
        else
            log_warn "MYSQL_CONFIG set but file not found: $MYSQL_CONFIG"
        fi
    fi
}
