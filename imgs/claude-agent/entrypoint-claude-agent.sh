#!/bin/bash
set -e

INSTRUCTION_FILE="${INSTRUCTION_FILE:-/config/task.md}"
WORKSPACE_ORIGIN="/workspace/origin"
WORKSPACE_CURRENT="/workspace/current"

# Cleanup function - always forget workspace
cleanup() {
    local exit_code=$?
    if [ -n "$AGENT_NAME" ] && [ -n "$LOG_FILE" ]; then
        log "Cleanup: Forgetting workspace $AGENT_NAME"
        cd "$WORKSPACE_ORIGIN" 2>/dev/null || true
        if jj workspace forget "$AGENT_NAME" 2>&1 | tee -a "$LOG_FILE"; then
            log "Workspace forgotten successfully"
        else
            log_error "Failed to forget workspace (non-fatal)"
        fi
    fi
    exit $exit_code
}

trap cleanup EXIT

# Logging functions (output dir created after workspace setup)
log() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    if [ -n "$LOG_FILE" ]; then
        echo "$msg" >> "$LOG_FILE"
    fi
}

log_error() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $*"
    echo "$msg" >&2
    if [ -n "$LOG_FILE" ]; then
        echo "$msg" >> "$LOG_FILE"
    fi
}

# Step 1: Validate environment
echo "[$(date +'%Y-%m-%d %H:%M:%S')] Step 1: Validating environment..."
if [ ! -f "$INSTRUCTION_FILE" ]; then
    echo "Error: Instruction file not found: $INSTRUCTION_FILE" >&2
    exit 1
fi

if [ ! -d "$WORKSPACE_ORIGIN/.jj" ]; then
    echo "Error: No jj repository found at $WORKSPACE_ORIGIN" >&2
    exit 1
fi

# Step 2: Create jj workspace
echo "[$(date +'%Y-%m-%d %H:%M:%S')] Step 2: Creating isolated workspace..."
if ! cd "$WORKSPACE_ORIGIN" 2>&1; then
    echo "ERROR: Failed to change to workspace origin directory" >&2
    exit 2
fi

if ! jj workspace add --name "$AGENT_NAME" -r @ "$WORKSPACE_CURRENT" 2>&1; then
    echo "ERROR: Failed to create jj workspace" >&2
    exit 2
fi

if ! cd "$WORKSPACE_CURRENT" 2>&1; then
    echo "ERROR: Failed to change to workspace current directory" >&2
    exit 2
fi

if ! jj bookmark create "$AGENT_NAME" 2>&1; then
    echo "ERROR: Failed to create bookmark" >&2
    exit 2
fi

# Setup output directory and logging
OUTPUT_DIR="$WORKSPACE_CURRENT/agent-output-$AGENT_NAME"
LOG_FILE="$OUTPUT_DIR/agent.log"
RESULT_FILE="$OUTPUT_DIR/result.json"
SUMMARY_FILE="$OUTPUT_DIR/summary.json"

if ! mkdir -p "$OUTPUT_DIR" 2>&1; then
    echo "ERROR: Failed to create output directory" >&2
    exit 2
fi

log "Agent $AGENT_NAME started"
log "Output directory: $OUTPUT_DIR"

# Step 3: Configure MCP servers
log "Step 3: Configuring MCP servers..."
if [ -f "$HOME/.claude.json" ] && command -v jq &>/dev/null; then
    if GLOBAL_MCP=$(jq -c '.mcpServers // {}' "$HOME/.claude.json" 2>&1 | tee -a "$LOG_FILE"); then
        if [ "$GLOBAL_MCP" != "{}" ]; then
            log "Found global MCP servers, merging configuration..."
            if jq --argjson servers "$GLOBAL_MCP" \
               '.projects["/workspace/current"].mcpServers = $servers' \
               "$HOME/.claude.json" > /tmp/claude.json.tmp 2>&1 | tee -a "$LOG_FILE"; then
                if cat /tmp/claude.json.tmp > "$HOME/.claude.json" 2>&1 | tee -a "$LOG_FILE"; then
                    rm -f /tmp/claude.json.tmp
                    log "MCP servers configured successfully"
                else
                    log_error "Failed to write merged MCP config (non-fatal, continuing...)"
                    rm -f /tmp/claude.json.tmp
                fi
            else
                log_error "Failed to merge MCP config (non-fatal, continuing...)"
                rm -f /tmp/claude.json.tmp
            fi
        else
            log "No global MCP servers found"
        fi
    else
        log_error "Failed to read MCP config (non-fatal, continuing...)"
    fi
else
    log "Skipping MCP configuration (jq not available or no config file)"
fi

# Step 4: Execute Claude agent
log "Step 4: Executing Claude agent..."
log "Reading instructions from $INSTRUCTION_FILE"
INSTRUCTIONS=$(cat "$INSTRUCTION_FILE")

# Create enhanced system prompt for iteration and retry
SYSTEM_PROMPT="You are an autonomous agent working on a task in complete isolation.

CRITICAL INSTRUCTIONS:
1. You must complete the task specified in the instructions below
2. If you encounter errors, try alternative approaches
3. Do not give up after first failure - iterate and retry with different methods
4. Try multiple solutions until you succeed or exhaust all reasonable options
5. Document your attempts and reasoning in comments or commit messages
6. If you cannot complete the task after multiple attempts, create a file 'AGENT-FAILURE.md' explaining:
   - What you tried
   - Why each approach failed
   - What would be needed to complete the task

Your working directory is isolated in a jujutsu workspace. All changes will be committed as a single change when you're done.

TASK INSTRUCTIONS:
$INSTRUCTIONS"

START_TIME=$(date +%s)
log "Executing Claude with autonomous retry instructions..."

# Background progress monitor - logs every 30s while Claude runs
(
    while sleep 30; do
        ELAPSED=$(($(date +%s) - START_TIME))
        log "Still running... ${ELAPSED}s elapsed"
    done
) &
PROGRESS_PID=$!

if claude -p "$SYSTEM_PROMPT" \
    --tools "default" \
    --dangerously-skip-permissions \
    --output-format json > "$RESULT_FILE" 2>&1; then
    CLAUDE_EXIT=0
    log "Claude completed with exit code: 0"
else
    CLAUDE_EXIT=$?
    log_error "Claude failed with exit code: $CLAUDE_EXIT"
    log_error "Check $RESULT_FILE for details"
fi

# Stop progress monitor
kill $PROGRESS_PID 2>/dev/null || true

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
log "Execution duration: ${DURATION}s"

# Step 5: Process results and verify completion
log "Step 5: Processing results..."

CHANGED_FILES=""
FILE_COUNT=0
COMMIT_HASH="none"
TASK_STATUS="unknown"

# Step 6: Commit changes if any
log "Step 6: Committing changes..."
if jj diff --stat 2>&1 | tee -a "$LOG_FILE" | grep -q .; then
    log "Changes detected, analyzing..."

    # Count files and list them
    FILE_COUNT=$(jj diff --stat --no-pager 2>/dev/null | grep '|' | wc -l || echo 0)
    CHANGED_FILES=$(jj diff --stat --no-pager 2>/dev/null | grep '|' | awk '{print $1}' | tr '\n' ', ' | sed 's/,$//' || echo "")

    log "Files modified: $FILE_COUNT"
    log "Changed files: $CHANGED_FILES"

    # Create commit
    if ! jj commit -m "Agent task: $AGENT_NAME" 2>&1 | tee -a "$LOG_FILE"; then
        log_error "Failed to commit changes"
        TASK_STATUS="commit_failed"
        exit 3
    fi

    # Update bookmark to point to the committed changes
    if ! jj bookmark set "$AGENT_NAME" -r @- 2>&1 | tee -a "$LOG_FILE"; then
        log_error "Failed to set bookmark to committed changes"
        TASK_STATUS="commit_failed"
        exit 3
    fi

    # Get commit hash
    COMMIT_HASH=$(jj log -r "$AGENT_NAME" --no-graph -T 'commit_id' 2>&1 | head -n1)
    log "Changes committed to bookmark $AGENT_NAME"
    log "Commit hash: $COMMIT_HASH"
    TASK_STATUS="success"
else
    log "No changes detected"
    CHANGED_FILES="none"
    FILE_COUNT=0
    TASK_STATUS="no_changes"
fi

# Step 7: Generate summary and include in commit
log "Step 7: Generating execution summary..."

cat > "$SUMMARY_FILE" <<EOF
{
  "status": "$TASK_STATUS",
  "agent_name": "$AGENT_NAME",
  "bookmark": "$AGENT_NAME",
  "commit_hash": "$COMMIT_HASH",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "duration_seconds": $DURATION,
  "claude_exit_code": $CLAUDE_EXIT,
  "instruction_file": "$INSTRUCTION_FILE",
  "changes": {
    "files_modified": $FILE_COUNT,
    "changed_files": "$CHANGED_FILES"
  },
  "output": {
    "log_file": "$LOG_FILE",
    "result_file": "$RESULT_FILE",
    "summary_file": "$SUMMARY_FILE"
  }
}
EOF

log "Summary written to $SUMMARY_FILE"

# If we committed changes, squash the summary into that commit
if [ "$TASK_STATUS" = "success" ]; then
    log "Including summary in commit..."
    if jj squash --from @ --into @- 2>&1 | tee -a "$LOG_FILE"; then
        log "Summary included in commit successfully"
    else
        log_error "Failed to include summary in commit (non-fatal)"
    fi
fi

log "Agent execution complete"

# Cleanup happens automatically via trap EXIT
