---
name: Dataset Fix — Root Cause of 3.1% FEH
description: What went wrong in training run 1 and what was fixed for run 2
type: project
originSessionId: 192ad776-f6a5-4f9d-8b58-fe195ac95d76
---
**Root cause:** `download_datasets.py` downloaded `evol_codealpaca`, `codealpha_20k`, `hf_codealpha` — general coding Q&A datasets. These used `input`/`output` field names identical to NL→bash format, but 97% of 28,132 examples were Python tutorials, finance Q&A, Stack Overflow answers. Model learned to write essays, not bash commands.

**Fix applied (2026-05-27):**
- `scripts/download_datasets.py` — complete rewrite, now downloads only:
  - westenfelder/NL2SH-ALFA (40,414 train pairs, NAACL 2025, MIT)
  - mecha-org/linux-command-dataset (8,517 pairs, Apache 2.0)
  - neulab/tldr — skipped (legacy script format, not loadable)
- `scripts/process_datasets.py` — hardened `is_valid()` with prose detection, length limits
- `eval/nl2sh_alfa_test.jsonl` — 300 held-out eval pairs (not for training)
- Old checkpoints deleted from VM before run 2

**How to apply:** If a future training run scores poorly, check data composition first — `wc -l data/processed/train.jsonl` and spot-check 5 examples to verify they are short bash commands, not prose.
