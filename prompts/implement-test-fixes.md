# Overview

Your task is to review code fix suggestions and implement those which you decide to.

# Input
A file `TEST_RESULT_ANALYSIS.md` is your input file. It's main part contains multiple short summaries of test failures and their root causes. Some of them contain code fix suggestions along with location.

# Workflow
Go through each item in your input, and for each one follow these steps:
    - Review the item summary and work through rules defined below to decide whether to implement the fix suggestion or not.
    - If the fix should not or cannot be implemented, briefly (in one or two sentences) describe why and move on to the next item.
    - If you decide to implement the fix, create a implementation plan. Make sure to follow the implementation rules described below.
    - After implementing a fix, try running the test for it.
        - If it succeeds, summarize your work on this item briefly and move on
        - If it fails, review the failure but don't try to fix it. Describe briefly what you did and what was the failure caused by and move on.

## DECISION MAKING
Below are rules for you to use when deciding whether to implement a fix suggestion or not. Go through each one of them in descending order and if the item passes ALL of them, then it's okay to implement.
    - Fix suggestions must exist. Some items will be only descriptive of a test failure without a suggestion, these don't have anything to implement.
    - The fix can touch only code in 'api' package, tests or mocks.
    - The fix cannot be related to external service
    - The fix must be isolated to a single design domain (graphql, data managers, permissions etc...). Explore the code related to the fix, and if you are not sure, do not implement.

## IMPLEMENTATION RULES
You are writing a senior level code. Before implementation, always explore the related code and find the cleanest way to implement the suggested fix.
    - Reuse existing code as much as possible. Edit existing code, unless creating new one is required for clarity or function.
    - Avoid any magic or implicit behaviour, keep it simple and descriptive.
    - Prefer simple functional behaviour to complex structures or logic.

# Output
After you finish, summarize all your work into structured `TEST_FIX_IMPLEMENTATION.md` file. Each worked item should be described briefly.
