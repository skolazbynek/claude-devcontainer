---
description: Resolve all jujutsu conflicts in the current working change (@). Jujutsu-specific -- not applicable to git repositories.
---

# Task

You are a senior engineer. Resolve all conflicts in the current jujutsu working change.

# Background

Jujutsu records conflicts as first-class commit state. Conflicted files contain inline conflict markers; removing them with correct content is all that is required — jj detects resolution automatically.

**Conflict marker format (default diff style):**

```
<<<<<<< Conflict 1 of N
%%%%%%% Changes from base to side #1
-removed line
+added line
+++++++ Contents of side #2
content from other side
>>>>>>> Conflict 1 of N ends
```

`%%%%%%%` precedes a diff (base → one side). `+++++++` precedes a snapshot (the other side). Resolve by replacing the entire block — markers included — with the correct final content.

# Workflow

1. Run `jj resolve --list` to enumerate all conflicted files.
2. For each file:
   a. Read the file and locate every conflict block.
   b. Read `jj log -r @` and `jj diff -r @` for context on what this change is doing.
   c. Examine surrounding code, callers, and intent to determine the correct resolution.
   d. Edit the file: replace each conflict block with the resolved content.
3. After all files are edited, run `jj resolve --list` again to confirm zero conflicts remain.
4. Run `jj diff` to verify the final state looks correct.

# Rules

- Never leave a conflict marker in the file — partial resolution is not acceptable.
- Do not resolve by blindly picking one side; understand the intent of both sides first.
- Do not modify code outside conflict regions unless strictly required for correctness.
- Do not add comments, docstrings, or formatting changes beyond what is needed to resolve.
- If a conflict cannot be resolved with confidence, stop and report the file and the ambiguity instead of guessing.

# Output

After all conflicts are resolved, write a brief summary to `CONFLICTS_RESOLVED.md`:
- List of files resolved, one per line.
- For each file, one sentence describing what the conflict was and how it was resolved.
- If any conflict was skipped due to ambiguity, list it separately with the reason.
