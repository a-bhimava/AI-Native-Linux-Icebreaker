#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# All gcloud commands you run on your MAC (not on the VM).
# Edit VM_NAME, ZONE, and PROJECT to match your GCP setup.
# ─────────────────────────────────────────────────────────────────────────────

# ── EDIT THESE ────────────────────────────────────────────────────────────────
VM_NAME="privileged-brain-vm"      # whatever you named your VM in GCP console
ZONE="us-west4-b"                  # updated automatically by 'create' if zone is exhausted
PROJECT="project-12486d7e-4046-45bd-8b4"      # GCP project ID
GCLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ──────────────────────────────────────────────────────────────────────────────

# GPU candidates — on-demand only, ordered cheapest first.
# Format: "machine-type|accelerator|zone1|zone2|..."
# GPU candidates — ALL official zones per GPU type, cheapest first.
# Zone lists generated from: gcloud compute accelerator-types list
GPU_CANDIDATES=(
  # ── T4 16 GB  ~$0.35/hr ───────────────────────────────────────────────────
  "n1-standard-4|nvidia-tesla-t4|us-central1-a|us-central1-b|us-central1-c|us-central1-f|us-east1-b|us-east1-c|us-east1-d|us-east4-a|us-east4-b|us-east4-c|us-west1-a|us-west1-b|us-west2-b|us-west2-c|us-west3-b|us-west4-a|us-west4-b|europe-west1-b|europe-west1-c|europe-west1-d|europe-west2-a|europe-west2-b|europe-west3-b|europe-west4-a|europe-west4-b|europe-west4-c|europe-central2-b|europe-central2-c|asia-east1-a|asia-east1-c|asia-east2-a|asia-east2-c|asia-northeast1-a|asia-northeast1-c|asia-northeast3-b|asia-northeast3-c|asia-south1-a|asia-south1-b|asia-southeast1-a|asia-southeast1-b|asia-southeast1-c|asia-southeast2-a|asia-southeast2-b|australia-southeast1-a|australia-southeast1-c|me-west1-b|me-west1-c|northamerica-northeast1-c|southamerica-east1-a|southamerica-east1-b|southamerica-east1-c"
  # ── P4  8 GB  ~$0.60/hr ───────────────────────────────────────────────────
  "n1-standard-4|nvidia-tesla-p4|us-central1-a|us-central1-c|us-east4-a|us-east4-b|us-east4-c|us-west2-b|us-west2-c|europe-west4-b|europe-west4-c|asia-southeast1-a|asia-southeast1-b|asia-southeast1-c|australia-southeast1-a|australia-southeast1-b|northamerica-northeast1-a|northamerica-northeast1-b|northamerica-northeast1-c"
  # ── L4 24 GB  ~$0.70/hr ───────────────────────────────────────────────────
  "g2-standard-4|nvidia-l4|us-central1-a|us-central1-b|us-central1-c|us-east1-b|us-east1-c|us-east1-d|us-east4-a|us-east4-c|us-west1-a|us-west1-b|us-west1-c|us-west4-a|us-west4-c|europe-west1-b|europe-west1-c|europe-west2-a|europe-west2-b|europe-west3-a|europe-west3-b|europe-west4-a|europe-west4-b|europe-west4-c|europe-west6-b|europe-west6-c|asia-east1-a|asia-east1-b|asia-east1-c|asia-northeast1-a|asia-northeast1-b|asia-northeast1-c|asia-northeast3-a|asia-northeast3-b|asia-south1-a|asia-south1-b|asia-south1-c|asia-southeast1-a|asia-southeast1-b|asia-southeast1-c|me-central2-a|me-central2-c|northamerica-northeast1-b|northamerica-northeast1-c|northamerica-northeast2-a|northamerica-northeast2-b"
  # ── P100 16 GB  ~$1.46/hr ─────────────────────────────────────────────────
  "n1-standard-4|nvidia-tesla-p100|us-central1-c|us-central1-f|us-east1-b|us-east1-c|us-west1-a|us-west1-b|europe-west1-b|europe-west1-d|europe-west4-a|asia-east1-a|asia-east1-c|australia-southeast1-b"
  # ── V100 16 GB  ~$2.48/hr ─────────────────────────────────────────────────
  "n1-standard-8|nvidia-tesla-v100|us-central1-a|us-central1-b|us-central1-c|us-central1-f|us-east1-b|us-east1-c|us-west1-a|us-west1-b|europe-west4-a|europe-west4-b|europe-west4-c|asia-east1-c"
)

case "${1:-help}" in

# ── 1. CREATE VM ──────────────────────────────────────────────────────────────
create)
  CREATED_ZONE=""
  CREATED_GPU=""

  LAST_GPU=""
  for candidate in "${GPU_CANDIDATES[@]}"; do
    IFS='|' read -r machine_type gpu_type zones_str <<< "$candidate"
    IFS='|' read -ra zones <<< "$zones_str"

    if [ "$gpu_type" != "$LAST_GPU" ]; then
      echo "── $gpu_type ($machine_type) ──"
      LAST_GPU="$gpu_type"
    fi

    for z in "${zones[@]}"; do
      echo -n "  $z ... "
      ERR=$(gcloud compute instances create "$VM_NAME" \
          --project="$PROJECT" \
          --zone="$z" \
          --machine-type="$machine_type" \
          --accelerator="type=$gpu_type,count=1" \
          --maintenance-policy=TERMINATE \
          --image-family=common-cu129-ubuntu-2204-nvidia-580 \
          --image-project=deeplearning-platform-release \
          --boot-disk-size=100GB \
          --boot-disk-type=pd-ssd \
          --metadata="install-nvidia-driver=True" 2>&1); RC=$?
      if [ $RC -eq 0 ]; then
        CREATED_ZONE="$z"
        CREATED_GPU="$gpu_type"
        break 2
      else
        REASON=$(echo "$ERR" | grep -oE 'QUOTA_EXCEEDED|ZONE_RESOURCE_POOL_EXHAUSTED|resourcesNotAvailable|stockout' | head -1)
        echo "${REASON:-unavailable}"
      fi
    done
  done

  if [ -z "$CREATED_ZONE" ]; then
    echo ""
    echo "ERROR: Could not get a GPU in any zone."
    echo "GCP is under heavy load. Wait 10–15 min and retry."
    exit 1
  fi

  # Persist the winning zone back into this script
  sed -i '' "s/^ZONE=.*/ZONE=\"$CREATED_ZONE\"/" "$GCLOUD_DIR/mac_commands.sh"
  echo ""
  echo "✓ VM created!  GPU=$CREATED_GPU  zone=$CREATED_ZONE  (on-demand)"
  echo "  ZONE saved to mac_commands.sh automatically."
  echo ""
  echo "Wait ~2 min for it to boot, then:"
  echo "  bash mac_commands.sh upload"
  echo "  bash mac_commands.sh ssh"
  ;;

# ── 2. SSH ────────────────────────────────────────────────────────────────────
ssh)
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT"
  ;;

# ── 3. UPLOAD DATA AND SCRIPTS ────────────────────────────────────────────────
upload)
  echo "Uploading training data and scripts to VM..."

  # Use ~ so files land in whoever gcloud SSH connects as (colabuser23, aditya, etc.)
  # Create data dir on VM first
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
    --command="mkdir -p ~/data" 2>/dev/null

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
  LOCAL_DEST="$GCLOUD_DIR/training/adapters"
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
  echo "Next: bash 05_convert_and_import.sh"
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
