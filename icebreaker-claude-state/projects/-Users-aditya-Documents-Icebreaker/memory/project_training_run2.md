---
name: Training Run Status (current)
description: Current and historical privileged-brain training runs, results, and VM state
type: project
originSessionId: 192ad776-f6a5-4f9d-8b58-fe195ac95d76
---
## Run 7 — CoT SFT (CURRENT, in progress as of 2026-05-31)
Started: 2026-05-31 ~03:05 UTC on instance-20260528-030421 (us-central1-a, L4 GPU)
Screen session: `run7_cot`
Log: `~/training/logs/run7_cot.log`
Output dir: `~/training/adapters/run7_cot/`

Dataset: 13,839 train / 1,538 valid — CoT format (REASONING:/COMMAND:)
Sources: nl2bash_full (11,087) + synthetic_pairs (360) + synthetic_advanced (234) + beginner_pairs (3,696 incl. 500 new refuse pairs)
Format: CoT two-line output — REASONING:/COMMAND:
Steps: 2,595 total at ~3.5s/step → ~2.5h → done ~05:35 UTC
Base model: Qwen/Qwen2.5-Coder-1.5B-Instruct, LoRA rank 8

**Why:** CoT output format is the architectural fix for two simultaneous problems:
FEH 3.1% (model narrates instead of executing) and adversarial refusal ceiling 80%.
The REASONING line gives the model a structured place to elaborate; only COMMAND executes.

**How to check:**
`gcloud compute ssh instance-20260528-030421 --zone=us-central1-a --command="tail -5 ~/training/logs/run7_cot.log"`

## Run 6 (complete) — SFT standard format
Result: 80% adversarial refusal (16/20), FEH unknown, token accuracy ~93%
Adapter: `~/training/adapters/sft/final`

## Run 2 (complete) — clean dataset baseline
Result: 93.34% token accuracy, 3.1% FEH — good accuracy, bad functional correctness
Dataset: 44,596 train (NL2SH-ALFA dominant)
