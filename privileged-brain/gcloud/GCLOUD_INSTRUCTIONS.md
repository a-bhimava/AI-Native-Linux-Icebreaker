# Privileged Brain — Google Cloud VM Training Guide

No disconnects. No time limits. Training runs to completion even if you close your laptop.

---

## Cost Estimate (T4 GPU)

| Item | Rate | Est. total |
|---|---|---|
| n1-standard-4 + T4 | ~$0.35/hr | ~$0.55 for SFT + DPO |
| Boot disk 100 GB SSD | ~$0.017/GB/month | ~$1.70/month while stopped |
| Egress (download adapter) | ~$0.08/GB | < $0.01 |

**Your $300 credit covers hundreds of full training runs.**

---

## One-Time Setup: Install gcloud CLI on your Mac

```bash
brew install --cask google-cloud-sdk
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

Your project ID is visible in the top bar of the GCP console.

---

## Step 1 — Edit `mac_commands.sh`

Open `gcloud/mac_commands.sh` and set these three variables at the top:

```bash
VM_NAME="privileged-brain-vm"      # any name you like
ZONE="us-central1-a"               # T4 availability is best in us-central1-a or us-west1-b
PROJECT="your-gcp-project-id"      # from GCP console top bar
```

---

## Step 2 — Create the VM

```bash
cd "/Users/aditya/Documents/Icebreaker/privileged-brain/gcloud"
bash mac_commands.sh create
```

This creates:
- Machine: `n1-standard-4` (4 vCPU, 15 GB RAM)
- GPU: NVIDIA T4 (16 GB VRAM)
- Disk: 100 GB SSD
- Image: Deep Learning on Linux (CUDA + Python pre-installed)

**Wait 2–3 minutes for the VM to fully boot before proceeding.**

> If you get "quota exceeded" for T4 in `us-central1-a`, try `us-west1-b` or `us-east1-c`.
> Change the ZONE variable and re-run.

---

## Step 3 — Upload Data and Scripts

```bash
bash mac_commands.sh upload
```

This sends `train.jsonl`, `valid.jsonl`, and all Python/shell scripts to the VM.

### Uploading a Colab checkpoint (if you have one)

If training got to step 1047 in Colab before disconnecting, upload that checkpoint so the VM resumes from there instead of starting over:

```bash
# Point this at your local checkpoint folder
bash mac_commands.sh upload-checkpoint /path/to/checkpoint-1000
```

---

## Step 4 — SSH into the VM

```bash
bash mac_commands.sh ssh
```

You are now inside the VM. All remaining commands in this section run **on the VM**.

---

## Step 5 — Set Up the VM Environment (run once)

```bash
bash 01_setup_vm.sh
```

This installs all Python packages and creates the directory structure. Takes ~3 minutes.
You should see `CUDA available: True` and the T4 name printed at the end.

**Verify your data uploaded correctly:**
```bash
wc -l data/train.jsonl data/valid.jsonl
# Expected: 28132 train, 3126 valid
```

---

## Step 6 — Start Training

```bash
bash 02_start_training.sh
```

This launches the full pipeline (SFT → DPO pairs → DPO) inside a `screen` session.
**You can now close your SSH connection — training keeps running.**

What happens automatically:
1. SFT trains for 3 epochs (~1.5 hrs on T4), checkpoint every 200 steps
2. DPO pairs are generated from the preference dataset
3. DPO trains for 2 epochs (~10 min), checkpoint every 100 steps
4. Both final adapters saved to `training/adapters/`

---

## Step 7 — Monitor Progress (from your Mac, no SSH needed)

```bash
# From your Mac:
bash mac_commands.sh monitor
```

Or SSH in and run:
```bash
bash 03_monitor.sh
```

**What healthy training looks like:**

| Step | Expected eval loss |
|---|---|
| 200 | ~1.6–1.8 |
| 1000 | ~0.9–1.1 |
| 2000 | ~0.6–0.8 |
| 5277 (end) | ~0.35–0.5 |

If loss is stuck above 1.5 after step 1000, SSH in and restart with a lower learning rate:
```bash
screen -S pb_training -X quit
python3 sft_train.py --lr 1e-4
```

---

## Step 8 — If SSH Disconnects During Training

Training is not affected. SSH reconnect and check:

```bash
bash mac_commands.sh ssh
# then on VM:
bash 03_monitor.sh
```

The screen session `pb_training` is still running. If the screen session is gone (e.g. VM rebooted), restart — it auto-resumes from the latest checkpoint:

```bash
bash 02_start_training.sh
```

---

## Step 9 — Download Adapters to Your Mac

Once `03_monitor.sh` shows `SFT FINAL: SAVED ✓` and `DPO FINAL: SAVED ✓`:

```bash
# On your Mac:
bash mac_commands.sh download
```

This downloads both adapters to:
```
privileged-brain/training/adapters/
├── sft/final/
└── dpo/final/
```

---

## Step 10 — Stop the VM

```bash
bash mac_commands.sh stop
```

**Do this immediately after downloading** — a stopped VM costs ~$0 compute (only tiny storage cost). You can restart it later if needed. To delete it permanently:

```bash
bash mac_commands.sh delete
```

---

## Step 11 — Continue on Your Mac

```bash
cd "/Users/aditya/Documents/Icebreaker/privileged-brain"
bash 05_convert_and_import.sh   # fuse LoRA → GGUF → Ollama
ollama run privileged-brain "list all listening TCP ports"
bash 07_evaluate.sh             # compare baseline vs fine-tuned
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `quota exceeded` on create | Change ZONE to `us-west1-b` or `us-east1-c` |
| `CUDA available: False` after setup | Run `sudo /opt/deeplearning/install-driver.sh` and reboot |
| OOM during training | SSH in, stop training, restart with `--batch-size 2 --grad-accum 8` |
| Loss jumps wildly | Restart with `--lr 1e-4` |
| Screen session missing after VM reboot | `bash 02_start_training.sh` — auto-resumes from checkpoint |
| `processing_class` error | `pip install -q --upgrade trl` then restart |
| `max_length` error | `pip install -q --upgrade trl transformers` then restart |
| Upload fails (file not found) | Run from inside the `gcloud/` folder |
| SSH timeout | VM still boots — wait 2–3 min and retry |
| DPO adapter missing on download | DPO runs after SFT finishes — check if SFT is still running with `03_monitor.sh` |

---

## Quick Reference — All Commands

**On your Mac:**
```bash
bash mac_commands.sh create              # create VM
bash mac_commands.sh ssh                 # SSH in
bash mac_commands.sh upload              # upload data + scripts
bash mac_commands.sh upload-checkpoint /path/to/checkpoint-NNNN
bash mac_commands.sh monitor             # check progress remotely
bash mac_commands.sh download            # get adapters back
bash mac_commands.sh stop                # stop billing
bash mac_commands.sh delete              # delete VM
```

**On the VM (after SSH):**
```bash
bash 01_setup_vm.sh                      # first-time setup
bash 02_start_training.sh                # start full pipeline
bash 02_start_training.sh sft            # SFT only
bash 02_start_training.sh dpo            # DPO only
bash 03_monitor.sh                       # check status
tail -f training/logs/pipeline.log       # live log
screen -r pb_training                    # attach to training session
nvidia-smi                               # GPU usage
```
