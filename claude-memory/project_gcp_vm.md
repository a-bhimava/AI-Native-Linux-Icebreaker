---
name: GCP VM Details
description: GPU quota=1; icebreaker-phase2-vm (T4) STOPPED with run7 deployed; pb-train-l4 deleted; old VMs retired
metadata:
  type: project
---
GCP project `project-12486d7e-4046-45bd-8b4`, account `colabuser23@gmail.com`. **Global GPU quota = 1 — only ONE GPU VM can run at a time** (had to stop the T4 to create the L4). L4 zones stock out — sweep zones (`us-central1-c` worked when us-west4 didn't); `g2-standard-4` has better availability.

**VMs:**
- `icebreaker-phase2-vm` — T4, `us-west4-b`. **STOPPED** with run7 deployed. Restart + `scripts/start_pb.sh` to serve the PB. SSH: `gcloud compute ssh icebreaker-phase2-vm --zone=us-west4-b --project=project-12486d7e-4046-45bd-8b4`.
- `pb-train-l4` — L4, us-central1-c. **DELETED** (run8 experiment torn down).
- `instance-20260528-030421`, `privileged-brain-vm` — retired.

**Config:** `dual-brain/scripts/deploy.env` (gitignored) holds VM_NAME/ZONE/PROJECT + the **GEMINI_API_KEY** — secrets live in env, never in TOML (`config.py` rejects them). Re-create from `deploy.env.example` if missing. VMs bill while RUNNING — stop when idle. See [[phase4-pb-final]].
