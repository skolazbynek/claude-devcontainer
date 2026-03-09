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

# Parse -n|--name from args. Sets CUSTOM_NAME and REMAINING_ARGS.
parse_name_arg() {
    CUSTOM_NAME=""
    REMAINING_ARGS=()
    while [[ $# -gt 0 ]]; do
        case $1 in
            -n|--name) CUSTOM_NAME="$2"; shift 2 ;;
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

# --- Docker arg builders ---
# All take a nameref to the caller's array as first argument.

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
    _args+=(
        "-v" "/etc/ssl/certs:/etc/ssl/certs:ro"
        "-v" "$mount_dir:$WORKSPACE_BASE/origin"
        "-w" "$WORKSPACE_BASE/current"
        "-e" "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"
    )
}

build_claude_config_args() {
    local -n _args=$1
    _args+=(
        "-v" "$HOME/.claude:$CONTAINER_HOME/.claude:rw"
        "-v" "$HOME/.claude.json:$CONTAINER_HOME/.claude.json:rw"
        "-v" "$HOME/.config:$CONTAINER_HOME/.config"
    )
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
