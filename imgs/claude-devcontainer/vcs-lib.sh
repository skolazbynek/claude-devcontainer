# Shell-level VCS abstraction. Source this file to get vcs_* functions.
#
# Detects whether the repository is jujutsu or git and provides a unified set
# of functions that both entrypoints (agent and devcontainer) use for workspace
# isolation, branching, committing, and cleanup.
#
# Usage: source /workspace/vcs-lib.sh
# Requires: WORKSPACE_ORIGIN to be set.

# --- Detection ---------------------------------------------------------------

VCS_TYPE=""

detect_vcs() {
    # Determine VCS type based on repo markers and available tools.
    # Prefers jj when both the .jj directory and the jj binary exist.
    if [ -d "$WORKSPACE_ORIGIN/.jj" ] && command -v jj &>/dev/null; then
        VCS_TYPE="jj"
    elif [ -d "$WORKSPACE_ORIGIN/.git" ] && command -v git &>/dev/null; then
        VCS_TYPE="git"
    elif [ -e "$WORKSPACE_ORIGIN/.git" ] && command -v git &>/dev/null; then
        # .git can be a file (worktree pointer)
        VCS_TYPE="git"
    else
        echo "Error: No supported VCS repository found at $WORKSPACE_ORIGIN" >&2
        echo "Expected .jj/ (jujutsu) or .git/ (git) directory" >&2
        return 1
    fi
    echo "Detected VCS: $VCS_TYPE"
}

# --- Workspace isolation -----------------------------------------------------

vcs_create_workspace() {
    # Create an isolated workspace/worktree for an agent or devcontainer.
    # Args: $1=name  $2=target_path  $3=revision (optional)
    local name="$1" path="$2" revision="${3:-}"

    if [ "$VCS_TYPE" = "jj" ]; then
        local cmd="jj workspace add --name $name"
        [ -n "$revision" ] && cmd="$cmd -r $revision"
        cmd="$cmd $path"
        eval "$cmd" 2>&1
    else
        local cmd="git worktree add -b $name $path"
        [ -n "$revision" ] && cmd="$cmd $revision"
        eval "$cmd" 2>&1
    fi
}

vcs_forget_workspace() {
    # Remove/forget a workspace/worktree.
    # Args: $1=name  $2=path (required for git, optional for jj)
    local name="$1" path="${2:-}"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj workspace forget "$name" 2>&1
    else
        if [ -n "$path" ]; then
            git worktree remove --force "$path" 2>&1 || true
        fi
        git worktree prune 2>&1 || true
        # Clean up the tracking branch
        git branch -D "$name" 2>/dev/null || true
    fi
}

# --- Branch / bookmark management --------------------------------------------

vcs_create_branch() {
    # Create a named branch/bookmark at a revision.
    # Args: $1=name  $2=revision (optional, default: current)
    local name="$1" revision="${2:-}"

    if [ "$VCS_TYPE" = "jj" ]; then
        local cmd="jj bookmark create $name"
        [ -n "$revision" ] && cmd="$cmd -r $revision"
        eval "$cmd" 2>&1
    else
        # In git worktree context, branch is already created by worktree add.
        # This is for additional branches if needed.
        if [ -n "$revision" ]; then
            git branch "$name" "$revision" 2>&1
        else
            git branch "$name" 2>&1
        fi
    fi
}

vcs_set_branch() {
    # Force-update a branch/bookmark to point at a revision.
    # Args: $1=name  $2=revision
    local name="$1" revision="$2"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj bookmark set "$name" -r "$revision" 2>&1
    else
        git branch -f "$name" "$revision" 2>&1
    fi
}

# --- Change creation ---------------------------------------------------------

vcs_new_change() {
    # Create a new change on top of a revision.
    # Args: $1=revision
    local revision="${1:-}"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj new "$revision" 2>&1
    else
        # In git, the worktree is already at the right revision.
        # Only checkout if explicitly requested.
        [ -n "$revision" ] && git checkout "$revision" 2>&1
    fi
}

# --- Committing --------------------------------------------------------------

vcs_commit() {
    # Commit all changes with a message.
    # Args: $1=message
    local message="$1"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj commit -m "$message" 2>&1
    else
        git add -A 2>&1
        git commit -m "$message" 2>&1
    fi
}

# --- Describe / amend message ------------------------------------------------

vcs_log_commit_id() {
    # Resolve a revision to a concrete commit hash.
    # Args: $1=revision
    local revision="$1"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj log -r "$revision" --no-graph -T 'commit_id' 2>&1 | head -n1
    else
        git rev-parse "$revision" 2>&1
    fi
}

# --- Diff / change detection -------------------------------------------------

vcs_has_changes() {
    # Return 0 (true) if there are uncommitted changes, 1 otherwise.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat 2>&1 | grep -q .
    else
        [ -n "$(git status --porcelain 2>/dev/null)" ]
    fi
}

vcs_diff_stat() {
    # Print a stat summary of changes.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat --no-pager 2>/dev/null
    else
        git diff HEAD --stat 2>/dev/null
    fi
}

vcs_diff_file_count() {
    # Print the number of changed files.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat --no-pager 2>/dev/null | grep '|' | wc -l
    else
        git diff HEAD --stat 2>/dev/null | grep '|' | wc -l
    fi
}

vcs_diff_file_names() {
    # Print comma-separated list of changed file names.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj diff --stat --no-pager 2>/dev/null | grep '|' | awk '{print $1}' | tr '\n' ', ' | sed 's/,$//'
    else
        git diff HEAD --stat 2>/dev/null | grep '|' | awk '{print $1}' | tr '\n' ', ' | sed 's/,$//'
    fi
}

# --- Squash ------------------------------------------------------------------

vcs_squash_into_parent() {
    # Squash the current change into its parent.
    if [ "$VCS_TYPE" = "jj" ]; then
        jj squash --from @ --into @- 2>&1
    else
        git add -A 2>&1
        git reset --soft HEAD~1 2>&1
        git commit --amend --no-edit 2>&1
    fi
}

# --- After-commit branch update ---------------------------------------------

vcs_update_branch_after_commit() {
    # After committing, update the branch to point at the new commit.
    # In jj, @ moves forward after commit so the bookmark needs to be set to @-.
    # In git, the branch already tracks HEAD, so this is a no-op.
    # Args: $1=branch_name
    local branch_name="$1"

    if [ "$VCS_TYPE" = "jj" ]; then
        jj bookmark set "$branch_name" -r @- 2>&1
    fi
    # git: branch already points at HEAD after commit -- no action needed
}
