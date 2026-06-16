---
name: No Claude co-author in commits
description: User does not want "Co-Authored-By: Claude" trailers in git commits
type: feedback
originSessionId: 192ad776-f6a5-4f9d-8b58-fe195ac95d76
---
Do not add `Co-Authored-By: Claude` (or any Claude trailer) to git commit messages on this project.

**Why:** User stated this explicitly when re-pushing a commit; they were also frustrated by it in an earlier session. They consider the trailer noise.

**How to apply:** Omit the `Co-Authored-By:` line from every commit's HEREDOC body on this repo. The default agent behavior adds it; suppress it here.
