#!/bin/bash
set -e

IMAGE_NAME="claude-agent:latest"
CONTAINER_USER="claude"
AGENT_NAME="agent_$RANDOM"

# Check arguments
if [ $# -lt 1 ]; then
    echo "Usage: $0 <task-file>" >&2
    exit 1
fi

TASK_FILE="$1"
CURRENT_DIR="$(pwd)"

# Validate task file exists
if [ ! -f "$TASK_FILE" ]; then
    echo "Error: Task file not found: $TASK_FILE" >&2
    exit 1
fi

# Convert to absolute path
TASK_FILE=$(realpath "$TASK_FILE")

# Find jj repository root
find_jj_root() {
    local dir="$1"
    while [ "$dir" != "/" ]; do
        if [ -d "$dir/.jj" ]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

JJ_ROOT=$(find_jj_root "$CURRENT_DIR")

if [ -z "$JJ_ROOT" ]; then
    echo "Error: No jj repository found" >&2
    exit 1
fi

# Build Docker image if needed
if [ -z "$(docker images -q $IMAGE_NAME 2>/dev/null)" ]; then
    echo "Building Docker image..."
    docker build -f Dockerfile.agent -t $IMAGE_NAME .
    echo ""
fi

# Run agent in background
echo "Starting agent in background..."
echo "Agent name: $AGENT_NAME"
echo "Task file: $TASK_FILE"
echo "Repository: $JJ_ROOT"
echo ""

CONTAINER_ID=$(docker run \
    --rm \
    --detach \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --cpus=2.0 \
    --memory=4g \
    --user "$(id -u):$(id -g)" \
    --name "$AGENT_NAME" \
    -e "HOME=/home/$CONTAINER_USER" \
    -e "INSTRUCTION_FILE=/config/task.md" \
    -e "AGENT_NAME=$AGENT_NAME" \
    -e "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt" \
    -v "$JJ_ROOT:/workspace/origin:rw" \
    -v "$TASK_FILE:/config/task.md:ro" \
    -v "$HOME/.claude:/home/$CONTAINER_USER/.claude:rw" \
    -v "$HOME/.claude.json:/home/$CONTAINER_USER/.claude.json:rw" \
    -v "$HOME/.config:/home/$CONTAINER_USER/.config" \
    -v "/etc/ssl/certs:/etc/ssl/certs:ro" \
    "$IMAGE_NAME")

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
echo "  tail -f $JJ_ROOT/agent-output-$AGENT_NAME/agent.log"
echo ""
echo "Wait for completion:"
echo "  docker wait $CONTAINER_ID"
echo ""
echo "After completion, view results:"
echo "  jj log -r $AGENT_NAME"
echo "  jj diff -r $AGENT_NAME"
echo "  cat $JJ_ROOT/agent-output-$AGENT_NAME/summary.json"
echo ""
echo "Merge changes:"
echo "  jj squash --from $AGENT_NAME"
echo ""
