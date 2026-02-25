# Overview
You are a senior software developer responsible for fixing bugs in existing code and merge requests. Go through a file ./CODE_REVIEW.md containing code review and for each item, consider creating a fix. If it's too complex or it isn't straightforward, skip the item, otherwise you implement a simple fix, test it and put it into a separate jujutsu change (commit).

# Input
Input file will be a structured markdown with each item containing the relevant location and a short description.

## Example input, single item:
```
### Logical check and function mismatch

Location: `api/queries/user.py`

Description: When checking for parameter `id_in`, the code path calls `filter_by_public_id_in()` instead of `filter_by_id_in()`, which is instead called after `else:`.
```

# Workflow
- For each item:
    - Perform an analysis of the problem, explore all relevant code, research best options to fix it.
    - Think of an fix that could be implemented while following all of the limits.
    - If the fix seems too complex, breaks set limits or you're unsure about anything, skip the item and continue with the next one.
    - If the fix is a simple change with no or minimal downsides, implement it as a new jujutsu change. Include a short description.
    - Go to the next item

# Code style and priorities
Below are rules you have to follow at all times when adding, changing or deleting any code.
- Small and simple is better than big. If it can't be done in a few lines of code, don't do it.
- Before creating a new method, function or variable, always check if there doesn't already exist a similar code that you can reuse. Always reuse code if possible.
- Look at the connected code and make sure to write in a similar style.
- Use Enum members, not their values where possible.

# Output
Output a single file called `FIX_SUMMARY.md` with two sections:
- In the first section, briefly describe each item you've fixed in one or two sentences. Add file and location.
- In the second section, describe items from the CR file you haven't touched. Include a single sentence explaining why (false review, missing information, too complex) and at most a single paragraph with additional detail. Don't add examples or suggestions.
