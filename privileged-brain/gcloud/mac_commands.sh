#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# All gcloud commands you run on your MAC (not on the VM).
# Edit VM_NAME, ZONE, and PROJECT to match your GCP setup.
# ─────────────────────────────────────────────────────────────────────────────

# ── EDIT THESE ────────────────────────────────────────────────────────────────
VM_NAME="privileged-brain-vm"      # whatever you named your VM in GCP console
ZONE="us-central1-a"               # zone you selected when creating the VM
PROJECT="your-gcp-project-id"      # GCP project ID (visible in console top bar)
GCLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ──────────────────────────────────────────────────────────────────────────────

case "${1:-help}" in

# ── 1. CREATE VM ──────────────────────────────────────────────────────────────
create)
  echo "Creating VM: $VM_NAME in $ZONE..."
  gcloud compute instances create "$VM_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --machine-type=n1-standard-4 \
    --accelerator=type=nvidia-tesla-t4,count=1 \
    --maintenance-policy=TERMINATE \
    --image-family=common-cu121-debian-11-py310 \
    --image-project=deeplearning-platform-release \
    --boot-disk-size=100GB \
    --boot-disk-type=pd-ssd \
    --metadata="install-nvidia-driver=True"
  echo ""
  echo "VM created. Wait ~2 min for it to boot, then SSH in:"
  echo "  bash mac_commands.sh ssh"
  ;;

# ── 2. SSH ────────────────────────────────────────────────────────────────────
ssh)
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT"
  ;;

# ── 3. UPLOAD DATA AND SCRIPTS ────────────────────────────────────────────────
upload)
  echo "Uploading training data and scripts to VM..."

  # Upload data files
  gcloud compute scp \
    "$GCLOUD_DIR/train.jsonl" \
    "$GCLOUD_DIR/valid.jsonl" \
    "$VM_NAME":~/data/ \
    --zone="$ZONE" --project="$PROJECT"

  # Upload scripts
  gcloud compute scp \
    "$GCLOUD_DIR/sft_train.py" \
    "$GCLOUD_DIR/dpo_train.py" \
    "$GCLOUD_DIR/generate_dpo_pairs.py" \
    "$GCLOUD_DIR/01_setup_vm.sh" \
    "$GCLOUD_DIR/02_start_training.sh" \
    "$GCLOUD_DIR/03_monitor.sh" \
    "$VM_NAME":~/ \
    --zone="$ZONE" --project="$PROJECT"

  echo ""
  echo "Upload complete. SSH in and run:"
  echo "  bash 01_setup_vm.sh"
  echo "  bash 02_start_training.sh"
  ;;

# ── 4. UPLOAD CHECKPOINT (resume from Colab checkpoint) ──────────────────────
upload-checkpoint)
  CHECKPOINT_PATH="${2:-}"
  if [ -z "$CHECKPOINT_PATH" ]; then
    echo "Usage: bash mac_commands.sh upload-checkpoint /path/to/checkpoint-1000"
    exit 1
  fi
  echo "Uploading checkpoint: $CHECKPOINT_PATH"
  gcloud compute scp --recurse \
    "$CHECKPOINT_PATH" \
    "$VM_NAME":~/training/adapters/sft/ \
    --zone="$ZONE" --project="$PROJECT"
  echo "Checkpoint uploaded. Training will auto-resume from it."
  ;;

# ── 5. MONITOR (check log without SSH-ing in) ─────────────────────────────────
monitor)
  echo "Fetching training log from VM..."
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
    --command="bash 03_monitor.sh"
  ;;

# ── 6. DOWNLOAD ADAPTERS ──────────────────────────────────────────────────────
download)
  LOCAL_DEST="$(dirname "$GCLOUD_DIR")/training/adapters"
  mkdir -p "$LOCAL_DEST"

  echo "Downloading SFT adapter..."
  gcloud compute scp --recurse \
    "$VM_NAME":~/training/adapters/sft/final \
    "$LOCAL_DEST/sft/" \
    --zone="$ZONE" --project="$PROJECT"

  echo "Downloading DPO adapter (if available)..."
  gcloud compute scp --recurse \
    "$VM_NAME":~/training/adapters/dpo/final \
    "$LOCAL_DEST/dpo/" \
    --zone="$ZONE" --project="$PROJECT" 2>/dev/null || echo "  DPO adapter not ready yet — skipping."

  echo ""
  echo "Adapters downloaded to: $LOCAL_DEST"
  echo "Next: cd .. && bash 05_convert_and_import.sh"
  ;;

# ── 7. STOP VM (stop billing when done) ───────────────────────────────────────
stop)
  echo "Stopping VM (you will not be billed for compute while stopped)..."
  gcloud compute instances stop "$VM_NAME" --zone="$ZONE" --project="$PROJECT"
  echo "VM stopped. Storage costs ~\$0.02/GB/month while stopped."
  echo "To delete permanently: bash mac_commands.sh delete"
  ;;

# ── 8. DELETE VM ──────────────────────────────────────────────────────────────
delete)
  echo "WARNING: This permanently deletes the VM and all data on it."
  echo "Make sure you have downloaded your adapters first!"
  read -r -p "Type 'yes' to confirm: " CONFIRM
  if [ "$CONFIRM" = "yes" ]; then
    gcloud compute instances delete "$VM_NAME" --zone="$ZONE" --project="$PROJECT" --quiet
    echo "VM deleted."
  else
    echo "Cancelled."
  fi
  ;;

# ── HELP ──────────────────────────────────────────────────────────────────────
help|*)
  echo "Usage: bash mac_commands.sh <command>"
  echo ""
  echo "Commands (run on your Mac):"
  echo "  create              Create the GCP VM with T4 GPU"
  echo "  ssh                 SSH into the VM"
  echo "  upload              Upload data + scripts to VM"
  echo "  upload-checkpoint   Upload a Colab checkpoint to resume from"
  echo "  monitor             Check training progress remotely"
  echo "  download            Download trained adapters back to Mac"
  echo "  stop                Stop VM (pause billing)"
  echo "  delete              Delete VM permanently"
  echo ""
  echo "Edit VM_NAME, ZONE, PROJECT at the top of this file first."
  ;;
esac
