#!/bin/bash
set -e

SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
CLD_ROOT="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/lib/docker-common.sh"

IMAGE_NAME="claude-agent:latest"

parse_name_arg "$@"
set -- "${REMAINING_ARGS[@]}"

if [ $# -lt 1 ]; then
    echo "Usage: $0 [-n|--name <name>] [-m|--model <model>] [-r|--revision <revset>] <task-file | -p prompt>" >&2
    exit 1
fi

# SESSION_NAME may be pre-set by a calling script (e.g. review agent)
SESSION_NAME="${SESSION_NAME:-$(build_session_name "agent" "$CUSTOM_NAME")}"

INLINE_PROMPT=""
POSITIONAL_ARGS=()
while [[ $# -gt 0 ]]; do
    case $1 in
        -p) shift; INLINE_PROMPT="$*"; break ;;
        *) POSITIONAL_ARGS+=("$1"); shift ;;
    esac
done

if [ ${#POSITIONAL_ARGS[@]} -gt 0 ] && [ -f "${POSITIONAL_ARGS[0]}" ]; then
    TASK_FILE=$(mktemp --suffix=.md)
    cat "$(realpath "${POSITIONAL_ARGS[0]}")" > "$TASK_FILE"
    if [ -n "$INLINE_PROMPT" ]; then
        printf '\n\n## Additional Instructions\n\n%s\n' "$INLINE_PROMPT" >> "$TASK_FILE"
    fi
elif [ -n "$INLINE_PROMPT" ]; then
    TASK_FILE=$(mktemp --suffix=.md)
    echo "$INLINE_PROMPT" > "$TASK_FILE"
else
    echo "Error: No task file or prompt provided" >&2
    exit 1
fi
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

if [ -n "${AGENT_MODEL:-}" ]; then
    DOCKER_ARGS+=("-e" "AGENT_MODEL=$AGENT_MODEL")
fi

if [ -n "${AGENT_REVISION:-}" ]; then
    DOCKER_ARGS+=("-e" "AGENT_REVISION=$AGENT_REVISION")
fi

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
