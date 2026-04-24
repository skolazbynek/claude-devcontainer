#!/bin/bash
set -e
source /workspace/container-init.sh
source /workspace/vcs-lib.sh

AGENT_NAME="${SESSION_NAME:?SESSION_NAME must be set}"
INSTRUCTION_FILE="${INSTRUCTION_FILE:-/config/task.md}"

cleanup() {
    local exit_code=$?
    if [ -n "$AGENT_NAME" ] && [ -n "$LOG_FILE" ]; then
        log "Cleanup: forgetting workspace $AGENT_NAME"
        cd "$WORKSPACE_ORIGIN" 2>/dev/null || true
        vcs_forget_workspace "$AGENT_NAME" "$WORKSPACE_CURRENT" 2>&1 | tee -a "$LOG_FILE" || true
    fi
    exit $exit_code
}
trap cleanup EXIT

log() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    [ -n "$LOG_FILE" ] && echo "$msg" >> "$LOG_FILE"
}

log_error() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $*"
    echo "$msg" >&2
    [ -n "$LOG_FILE" ] && echo "$msg" >> "$LOG_FILE"
}

# --- Validate environment ---

if [ ! -f "$INSTRUCTION_FILE" ]; then
    echo "Error: Instruction file not found: $INSTRUCTION_FILE" >&2
    exit 1
fi

# Detect VCS type (jj or git)
detect_vcs || exit 1

# --- Create isolated workspace ---

cd "$WORKSPACE_ORIGIN"
WORKSPACE_REV="${AGENT_REVISION:-}"
# Provide VCS-appropriate default revision if none specified
if [ -z "$WORKSPACE_REV" ]; then
    if [ "$VCS_TYPE" = "jj" ]; then
        WORKSPACE_REV="@"
    else
        WORKSPACE_REV="HEAD"
    fi
fi

vcs_create_workspace "$AGENT_NAME" "$WORKSPACE_CURRENT" "$WORKSPACE_REV" 2>&1

cd "$WORKSPACE_CURRENT"

# For jj, create a new change on top of the revision and a bookmark.
# For git, the worktree already has a branch at the right revision.
if [ "$VCS_TYPE" = "jj" ]; then
    jj new "$WORKSPACE_REV" 2>&1
    jj bookmark create "$AGENT_NAME" 2>&1
fi

OUTPUT_DIR="$WORKSPACE_CURRENT/agent-output-$AGENT_NAME"
LOG_FILE="$OUTPUT_DIR/agent.log"
RESULT_FILE="$OUTPUT_DIR/result.json"
SUMMARY_FILE="$OUTPUT_DIR/summary.json"
mkdir -p "$OUTPUT_DIR"

log "Agent $AGENT_NAME started (VCS: $VCS_TYPE)"

# --- Configure MCP servers ---

build_claude_config

# --- Execute Claude ---

INSTRUCTIONS=$(cat "$INSTRUCTION_FILE")

if [ "$VCS_TYPE" = "jj" ]; then
    VCS_NOTE="Your working directory is isolated in a jujutsu workspace. All changes will be committed as a single change when you're done."
else
    VCS_NOTE="Your working directory is isolated in a git worktree. All changes will be committed when you're done."
fi

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

$VCS_NOTE

TASK INSTRUCTIONS:
$INSTRUCTIONS"

START_TIME=$(date +%s)
log "Executing Claude..."

# Progress monitor - logs every 30s while Claude runs
(while sleep 30; do log "Still running... $(($(date +%s) - START_TIME))s elapsed"; done) &
PROGRESS_PID=$!

AGENT_MODEL="${AGENT_MODEL:-sonnet}"
log "Using model: $AGENT_MODEL"

if claude -p "$SYSTEM_PROMPT" \
    --model "$AGENT_MODEL" \
    --tools "default" \
    --dangerously-skip-permissions \
    --output-format json > "$RESULT_FILE" 2>&1; then
    CLAUDE_EXIT=0
    log "Claude completed successfully"
else
    CLAUDE_EXIT=$?
    log_error "Claude failed with exit code: $CLAUDE_EXIT"
fi

kill $PROGRESS_PID 2>/dev/null || true

DURATION=$(($(date +%s) - START_TIME))
log "Duration: ${DURATION}s"

# --- Commit changes ---

CHANGED_FILES=""
FILE_COUNT=0
COMMIT_HASH="none"
TASK_STATUS="unknown"

if vcs_has_changes; then
    FILE_COUNT=$(vcs_diff_file_count)
    CHANGED_FILES=$(vcs_diff_file_names)
    log "Files modified: $FILE_COUNT ($CHANGED_FILES)"

    # Ask Claude for a descriptive commit message based on what it did
    if [ "$VCS_TYPE" = "jj" ]; then
        DESCRIBE_PROMPT="Look at the current jj diff (run jj diff --stat and jj diff). Write a single short sentence (under 72 chars) describing what was done. Output ONLY the description, nothing else."
    else
        DESCRIBE_PROMPT="Look at the current git diff (run git diff --stat and git diff). Write a single short sentence (under 72 chars) describing what was done. Output ONLY the description, nothing else."
    fi
    COMMIT_MSG=$(claude -p "$DESCRIBE_PROMPT" \
        --model "$AGENT_MODEL" \
        --dangerously-skip-permissions 2>/dev/null | head -1)
    COMMIT_MSG="${COMMIT_MSG:-Agent task: $AGENT_NAME}"
    log "Commit message: $COMMIT_MSG"

    if ! vcs_commit "$COMMIT_MSG" 2>&1 | tee -a "$LOG_FILE"; then
        log_error "Failed to commit changes"
        TASK_STATUS="commit_failed"
        exit 3
    fi

    vcs_update_branch_after_commit "$AGENT_NAME" 2>&1 | tee -a "$LOG_FILE"
    COMMIT_HASH=$(vcs_log_commit_id "$AGENT_NAME" 2>&1 | head -n1)
    log "Committed to branch $AGENT_NAME ($COMMIT_HASH)"
    TASK_STATUS="success"
else
    log "No changes detected"
    TASK_STATUS="no_changes"
fi

# --- Generate summary ---

cat > "$SUMMARY_FILE" <<EOF
{
  "status": "$TASK_STATUS",
  "agent_name": "$AGENT_NAME",
  "branch": "$AGENT_NAME",
  "commit_hash": "$COMMIT_HASH",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "duration_seconds": $DURATION,
  "claude_exit_code": $CLAUDE_EXIT,
  "instruction_file": "$INSTRUCTION_FILE",
  "vcs_type": "$VCS_TYPE",
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

# Squash summary into the agent's commit
if [ "$TASK_STATUS" = "success" ]; then
    vcs_squash_into_parent 2>&1 | tee -a "$LOG_FILE" || \
        log_error "Failed to include summary in commit (non-fatal)"
fi

log "Agent execution complete"
