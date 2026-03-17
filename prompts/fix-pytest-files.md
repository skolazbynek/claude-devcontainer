---
description: Analyze failing pytest logs in ./test_failures/, find root causes, and suggest fixes
---

# Overview

You are a senior developer. Your task is to find the root cause of failing tests.

# Input
In `./test_failures/` folder each file contains logs from a single failing pytest test.

# Workflow
Go through the files from input one by one and try to find the root cause of its failure by:
- Analyzing the logs from the input files
- Checking out the test itself that failed
- Exploring the codebase relevant to the failing test, based on the test logs
- Checking code history with jujutsu

# Constraints
- Calls to Diskuze API within test are not mocked by design. Skip such failures, note them in your output summary briefly.
- Any timeouts are temporary failures, there's no other root cause.

# Expected result
For each test file, find and present the root cause summarized in two or three sentences.
- If the root cause is within the code (e.g. not third-party timeouts), try suggesting a simple fix, if there is one. If the issue is too complex or you are not sure, do not try to suggest a fix.
- If the root cause is related to a recently introduced change, mention this and add brief context (one, two sentences).

Compile all findings into a single structured markdown file `TEST_RESULTS_ANALYSIS.md` 
