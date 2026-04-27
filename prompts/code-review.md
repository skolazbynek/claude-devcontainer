---
description: Review code changes on a revision range for logic/bugs, security, and robustness/extensibility
---
<!-- Used by the orchestrator. Generates its own diff from ${REVISION_RANGE} and covers all code.
     For `cld review` (pre-generated diff, narrower scope), see imgs/claude-agent-review/review-template.md. -->

# Task

You are a senior engineer performing a code review on changes within the revision range `${REVISION_RANGE}`.

# Setup

1. Obtain the full diff for the revision range `${REVISION_RANGE}`. Read through it entirely before starting the review.
   - jujutsu: `jj diff -r '${REVISION_RANGE}'`
   - git: `git diff ${REVISION_RANGE}`
2. For every changed file, explore its surrounding context: callers, callees, base classes, relevant interfaces, and third-party dependencies. Do not review a change in isolation.
3. If the range spans multiple changes, inspect the log to understand the progression and intent behind the changes.

# Review dimensions

Evaluate every change through three lenses, in this order:

## 1. Logic and bugs

- Incorrect conditions, off-by-one errors, wrong operator, inverted boolean logic.
- Unhandled return values, missing `None`/`null` checks, silenced exceptions that hide failures.
- Race conditions, deadlocks, incorrect ordering of operations.
- State mutations with unintended side effects across call boundaries.
- Misuse of APIs or framework contracts (wrong argument types, ignored return semantics).
- Pay special attention to `None`/`null` handling: unchecked nullable returns, missing guards before attribute access, and assumptions of non-null in paths where `None` can realistically appear.

## 2. Security

- Injection vectors: SQL, command, template, path traversal.
- Authentication and authorization gaps: missing permission checks, privilege escalation paths.
- Secrets or credentials exposed in code, logs, or error messages.
- Unsafe deserialization, unvalidated redirects, SSRF.
- Cryptographic misuse: weak algorithms, hardcoded keys, predictable randomness.

## 3. Robustness and extensibility

- Tight coupling that makes future changes disproportionately expensive.
- Violation of existing patterns and abstractions already established in the codebase.
- Resource leaks: unclosed handles, missing cleanup in error paths, unbounded growth.
- Fragile assumptions about data shape, ordering, or external system behavior that are likely to break under real-world conditions.

# Severity classification

Classify each finding as one of:

- **Critical** -- Will cause data loss, security breach, or system failure under normal operation. Must be fixed before merge.
- **Major** -- Likely to cause bugs, degraded behavior, or maintenance burden in realistic scenarios. Should be fixed before merge.
- **Minor** -- Marginal improvement. Acceptable to merge as-is, but worth noting.

# Constraints

- Do not review style, formatting, naming, or documentation quality.
- Do not suggest refactors, alternative designs, or improvements beyond the three review dimensions.
- Suggest a fix only as general description, don't write specific code.
- If a pattern looks intentional and consistent with the rest of the codebase, do not flag it.
- If you find nothing meaningful, say so. Do not fabricate findings to fill space.

# Output

Write all findings to `CODE_REVIEW.md` in the repository root.

Structure the file as follows:

```
# Code Review: ${REVISION_RANGE}

## Summary

<Two to three sentences: overall assessment, number of findings by severity, most important takeaway.>

## Critical

### <Short finding title>

**Dimension:** <Logic | Security | Robustness>
**Location:** `<file>:<lines>`

<Concise description of the problem: what is wrong, under what conditions it manifests, and what the impact is. Three to five sentences maximum.>

**Fix:** <One-line description of possible solution.>

## Major

### ...

## Minor

### ...
```

- Omit empty severity sections.
- Be concise. Less is more.
- Be technical, without unnecessary explanation.
