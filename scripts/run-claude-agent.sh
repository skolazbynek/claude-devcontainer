#!/bin/bash
set -e

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
CLD_ROOT="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/lib/docker-common.sh"

IMAGE_NAME="claude-agent:latest"

parse_name_arg "$@"
set -- "${REMAINING_ARGS[@]}"

if [ $# -lt 1 ]; then
    echo "Usage: $0 [-n|--name <name>] <task-file>" >&2
    exit 1
fi

# SESSION_NAME may be pre-set by a calling script (e.g. review agent)
SESSION_NAME="${SESSION_NAME:-$(build_session_name "agent" "$CUSTOM_NAME")}"

TASK_FILE="$1"

if [ ! -f "$TASK_FILE" ]; then
    echo "Error: Task file not found: $TASK_FILE" >&2
    exit 1
fi

TASK_FILE=$(realpath "$TASK_FILE")
JJ_ROOT=$(require_jj_root)

require_docker
ensure_image "$IMAGE_NAME" "$CLD_ROOT/imgs/claude-agent/Dockerfile.claude-agent" "$CLD_ROOT/imgs/claude-agent"
load_dotenv

# Build docker args
DOCKER_ARGS=(
    "--detach"
    "--name" "$SESSION_NAME"
)
build_base_args DOCKER_ARGS
build_workspace_args DOCKER_ARGS "$JJ_ROOT"
build_claude_config_args DOCKER_ARGS
build_docker_socket_args DOCKER_ARGS "$JJ_ROOT"
build_mysql_args DOCKER_ARGS

build_session_args DOCKER_ARGS "$SESSION_NAME"

HOST_TASK_FILE=$(to_host_path "$TASK_FILE")
DOCKER_ARGS+=(
    "-e" "INSTRUCTION_FILE=/config/task.md"
    "-v" "$HOST_TASK_FILE:/config/task.md:ro"
)

DOCKER_ARGS+=("$IMAGE_NAME")

echo "Starting agent in background..."
echo "Agent name: $SESSION_NAME"
echo "Task file: $TASK_FILE"
echo "Repository: $JJ_ROOT"
echo ""

CONTAINER_ID=$(docker run "${DOCKER_ARGS[@]}")

if [ -z "$CONTAINER_ID" ]; then
    echo "Error: Failed to start container" >&2
    exit 1
fi

echo "Container ID: $CONTAINER_ID"
echo ""
echo "========================================"
echo "Agent started successfully"
echo "========================================"
echo ""
echo "Check if running:"
echo "  docker ps --filter id=$CONTAINER_ID"
echo ""
echo "Follow progress (logs):"
echo "  tail -f $JJ_ROOT/agent-output-$SESSION_NAME/agent.log"
echo ""
echo "Wait for completion:"
echo "  docker wait $CONTAINER_ID"
echo ""
echo "After completion, view results:"
echo "  jj log -r $SESSION_NAME"
echo "  jj diff -r $SESSION_NAME"
echo "  cat $JJ_ROOT/agent-output-$SESSION_NAME/summary.json"
echo ""
echo "Merge changes:"
echo "  jj squash --from $SESSION_NAME"
echo ""
