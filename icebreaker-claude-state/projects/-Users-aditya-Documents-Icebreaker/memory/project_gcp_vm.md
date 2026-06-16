---
name: GCP VM Details
description: GCP compute instance name, zone, directory layout for privileged-brain training
type: project
originSessionId: 192ad776-f6a5-4f9d-8b58-fe195ac95d76
---
VM name: `privileged-brain-vm`, zone: `us-west4-b`, account: `colabuser23@gmail.com`
Status: RUNNING (n1-standard-4, T4 GPU)

Directory layout on VM (flat, NOT ~/icebreaker/):
- `~/sft_train.py` — main training script
- `~/data/train.jsonl` — training data (44,596 examples after fix)
- `~/data/valid.jsonl` — validation data (4,956 examples after fix)
- `~/training/adapters/sft/` — LoRA checkpoint output
- `~/training/logs/` — log files
- `~/01_setup_vm.sh`, `~/02_start_training.sh`, `~/03_monitor.sh` — shell scripts

**Why:** Earlier attempt to scp to ~/icebreaker/privileged-brain/ failed because that path doesn't exist on the VM.

**How to apply:** Always use `~/data/` and `~/training/` paths when uploading/referencing files on this VM.
