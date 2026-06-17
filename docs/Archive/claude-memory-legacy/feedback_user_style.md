---
name: User Collaboration Style
description: Strong preference for thorough research before any data/model/code change; very high cost of mistakes
metadata:
  type: feedback
---
Do thorough research on datasets/APIs/tools BEFORE touching any code. The user lost days to a hasty dataset decision that contaminated training data and produced a useless model (3.1% FEH), and separately rejected a retrain (run8) that regressed safety.

**Why:** Training runs take 75–90 min on GPU + hours of human setup; a bad data/model decision costs days, not minutes. For an OS terminal model, a safety regression is unacceptable.

**How to apply:** For anything touching training data, model choice, or training config: research first, present findings, get explicit confirmation, THEN modify code. Never swap datasets or change configs without approval. Validate any new model ≥ the current one (run7) on every axis before deploying.
