---
name: User Collaboration Style
description: User's strong preference for thorough research before any code changes; very high cost of mistakes
type: feedback
originSessionId: 192ad776-f6a5-4f9d-8b58-fe195ac95d76
---
Do thorough internet research on datasets/APIs/tools BEFORE touching any code. User lost 2 days to a hasty dataset decision that contaminated training data and produced a useless model (3.1% FEH).

**Why:** The user said explicitly: "now dont be hasty - you have been hasty and costed my 2 days during a very stressful phase. think of what datasets we will use. research that on the internet first." Training runs take 75-90 minutes on GCP + hours of human setup time — a mistake in data selection costs days, not minutes.

**How to apply:** For any decision that affects training data, model architecture, or dataset selection: search the internet first, present findings, get confirmation, THEN modify code. Never swap datasets or change training configs without explicit user approval.
