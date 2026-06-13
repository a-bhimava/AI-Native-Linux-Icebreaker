---
name: No Claude attribution in git
description: No "Co-Authored-By: Claude" in commits AND no "Generated with Claude Code" footer in PR bodies
metadata:
  type: feedback
---
Do not add any Claude attribution to git artifacts on this project: no `Co-Authored-By: Claude` trailer in commit messages, AND no "🤖 Generated with Claude Code" footer in PR descriptions/bodies.

**Why:** User stated the commit-trailer rule explicitly (frustrated by it before — considers it noise). They also asked to strip the "Generated with Claude Code" footer from a PR body, so the no-attribution preference extends to PRs.

**How to apply:** Omit the `Co-Authored-By:` line from every commit body, and do NOT append the Claude Code footer to `gh pr create`/`gh pr edit` bodies — even though the default agent behavior adds both.
