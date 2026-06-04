#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# find_gpu.sh — Cyclically tries every GPU type × zone on GCP until one VM
# is successfully created. Stops immediately on first success.
# Usage: bash find_gpu.sh
# ─────────────────────────────────────────────────────────────────────────────

PROJECT="project-12486d7e-4046-45bd-8b4"
VM_NAME="privileged-brain-vm"
IMAGE_FAMILY="common-cu129-ubuntu-2204-nvidia-580"
IMAGE_PROJECT="deeplearning-platform-release"

# Format: "machine-type|gpu-type|zone"
# Ordered: cheapest GPU first, all supported zones per type (from accelerator-types list)
CANDIDATES=(
  # ── T4 ~$0.35/hr ─────────────────────────────────────────────────────────
  "n1-standard-4|nvidia-tesla-t4|us-central1-a"
  "n1-standard-4|nvidia-tesla-t4|us-central1-b"
  "n1-standard-4|nvidia-tesla-t4|us-central1-c"
  "n1-standard-4|nvidia-tesla-t4|us-central1-f"
  "n1-standard-4|nvidia-tesla-t4|us-east1-b"
  "n1-standard-4|nvidia-tesla-t4|us-east1-c"
  "n1-standard-4|nvidia-tesla-t4|us-east1-d"
  "n1-standard-4|nvidia-tesla-t4|us-east4-a"
  "n1-standard-4|nvidia-tesla-t4|us-east4-b"
  "n1-standard-4|nvidia-tesla-t4|us-east4-c"
  "n1-standard-4|nvidia-tesla-t4|us-west1-a"
  "n1-standard-4|nvidia-tesla-t4|us-west1-b"
  "n1-standard-4|nvidia-tesla-t4|us-west2-b"
  "n1-standard-4|nvidia-tesla-t4|us-west2-c"
  "n1-standard-4|nvidia-tesla-t4|us-west3-b"
  "n1-standard-4|nvidia-tesla-t4|us-west4-a"
  "n1-standard-4|nvidia-tesla-t4|us-west4-b"
  "n1-standard-4|nvidia-tesla-t4|europe-west1-b"
  "n1-standard-4|nvidia-tesla-t4|europe-west1-c"
  "n1-standard-4|nvidia-tesla-t4|europe-west1-d"
  "n1-standard-4|nvidia-tesla-t4|europe-west2-a"
  "n1-standard-4|nvidia-tesla-t4|europe-west2-b"
  "n1-standard-4|nvidia-tesla-t4|europe-west3-b"
  "n1-standard-4|nvidia-tesla-t4|europe-west4-a"
  "n1-standard-4|nvidia-tesla-t4|europe-west4-b"
  "n1-standard-4|nvidia-tesla-t4|europe-west4-c"
  "n1-standard-4|nvidia-tesla-t4|europe-central2-b"
  "n1-standard-4|nvidia-tesla-t4|europe-central2-c"
  "n1-standard-4|nvidia-tesla-t4|asia-east1-a"
  "n1-standard-4|nvidia-tesla-t4|asia-east1-c"
  "n1-standard-4|nvidia-tesla-t4|asia-east2-a"
  "n1-standard-4|nvidia-tesla-t4|asia-east2-c"
  "n1-standard-4|nvidia-tesla-t4|asia-northeast1-a"
  "n1-standard-4|nvidia-tesla-t4|asia-northeast1-c"
  "n1-standard-4|nvidia-tesla-t4|asia-northeast3-b"
  "n1-standard-4|nvidia-tesla-t4|asia-northeast3-c"
  "n1-standard-4|nvidia-tesla-t4|asia-south1-a"
  "n1-standard-4|nvidia-tesla-t4|asia-south1-b"
  "n1-standard-4|nvidia-tesla-t4|asia-southeast1-a"
  "n1-standard-4|nvidia-tesla-t4|asia-southeast1-b"
  "n1-standard-4|nvidia-tesla-t4|asia-southeast1-c"
  "n1-standard-4|nvidia-tesla-t4|asia-southeast2-a"
  "n1-standard-4|nvidia-tesla-t4|asia-southeast2-b"
  "n1-standard-4|nvidia-tesla-t4|australia-southeast1-a"
  "n1-standard-4|nvidia-tesla-t4|australia-southeast1-c"
  "n1-standard-4|nvidia-tesla-t4|me-west1-b"
  "n1-standard-4|nvidia-tesla-t4|me-west1-c"
  "n1-standard-4|nvidia-tesla-t4|northamerica-northeast1-c"
  "n1-standard-4|nvidia-tesla-t4|southamerica-east1-a"
  "n1-standard-4|nvidia-tesla-t4|southamerica-east1-b"
  "n1-standard-4|nvidia-tesla-t4|southamerica-east1-c"
  # ── P4 ~$0.60/hr ─────────────────────────────────────────────────────────
  "n1-standard-4|nvidia-tesla-p4|us-central1-a"
  "n1-standard-4|nvidia-tesla-p4|us-central1-c"
  "n1-standard-4|nvidia-tesla-p4|us-east4-a"
  "n1-standard-4|nvidia-tesla-p4|us-east4-b"
  "n1-standard-4|nvidia-tesla-p4|us-east4-c"
  "n1-standard-4|nvidia-tesla-p4|us-west2-b"
  "n1-standard-4|nvidia-tesla-p4|us-west2-c"
  "n1-standard-4|nvidia-tesla-p4|europe-west4-b"
  "n1-standard-4|nvidia-tesla-p4|europe-west4-c"
  "n1-standard-4|nvidia-tesla-p4|asia-southeast1-a"
  "n1-standard-4|nvidia-tesla-p4|asia-southeast1-b"
  "n1-standard-4|nvidia-tesla-p4|asia-southeast1-c"
  "n1-standard-4|nvidia-tesla-p4|australia-southeast1-a"
  "n1-standard-4|nvidia-tesla-p4|australia-southeast1-b"
  "n1-standard-4|nvidia-tesla-p4|northamerica-northeast1-a"
  "n1-standard-4|nvidia-tesla-p4|northamerica-northeast1-b"
  "n1-standard-4|nvidia-tesla-p4|northamerica-northeast1-c"
  # ── L4 ~$0.70/hr ─────────────────────────────────────────────────────────
  "g2-standard-4|nvidia-l4|us-central1-a"
  "g2-standard-4|nvidia-l4|us-central1-b"
  "g2-standard-4|nvidia-l4|us-central1-c"
  "g2-standard-4|nvidia-l4|us-east1-b"
  "g2-standard-4|nvidia-l4|us-east1-c"
  "g2-standard-4|nvidia-l4|us-east1-d"
  "g2-standard-4|nvidia-l4|us-east4-a"
  "g2-standard-4|nvidia-l4|us-east4-c"
  "g2-standard-4|nvidia-l4|us-west1-a"
  "g2-standard-4|nvidia-l4|us-west1-b"
  "g2-standard-4|nvidia-l4|us-west1-c"
  "g2-standard-4|nvidia-l4|us-west4-a"
  "g2-standard-4|nvidia-l4|us-west4-c"
  "g2-standard-4|nvidia-l4|europe-west1-b"
  "g2-standard-4|nvidia-l4|europe-west1-c"
  "g2-standard-4|nvidia-l4|europe-west2-a"
  "g2-standard-4|nvidia-l4|europe-west2-b"
  "g2-standard-4|nvidia-l4|europe-west3-a"
  "g2-standard-4|nvidia-l4|europe-west3-b"
  "g2-standard-4|nvidia-l4|europe-west4-a"
  "g2-standard-4|nvidia-l4|europe-west4-b"
  "g2-standard-4|nvidia-l4|europe-west4-c"
  "g2-standard-4|nvidia-l4|europe-west6-b"
  "g2-standard-4|nvidia-l4|europe-west6-c"
  "g2-standard-4|nvidia-l4|asia-east1-a"
  "g2-standard-4|nvidia-l4|asia-east1-b"
  "g2-standard-4|nvidia-l4|asia-east1-c"
  "g2-standard-4|nvidia-l4|asia-northeast1-a"
  "g2-standard-4|nvidia-l4|asia-northeast1-b"
  "g2-standard-4|nvidia-l4|asia-northeast1-c"
  "g2-standard-4|nvidia-l4|asia-northeast3-a"
  "g2-standard-4|nvidia-l4|asia-northeast3-b"
  "g2-standard-4|nvidia-l4|asia-south1-a"
  "g2-standard-4|nvidia-l4|asia-south1-b"
  "g2-standard-4|nvidia-l4|asia-south1-c"
  "g2-standard-4|nvidia-l4|asia-southeast1-a"
  "g2-standard-4|nvidia-l4|asia-southeast1-b"
  "g2-standard-4|nvidia-l4|asia-southeast1-c"
  "g2-standard-4|nvidia-l4|me-central2-a"
  "g2-standard-4|nvidia-l4|me-central2-c"
  "g2-standard-4|nvidia-l4|northamerica-northeast1-b"
  "g2-standard-4|nvidia-l4|northamerica-northeast1-c"
  "g2-standard-4|nvidia-l4|northamerica-northeast2-a"
  "g2-standard-4|nvidia-l4|northamerica-northeast2-b"
  # ── P100 ~$1.46/hr ───────────────────────────────────────────────────────
  "n1-standard-4|nvidia-tesla-p100|us-central1-c"
  "n1-standard-4|nvidia-tesla-p100|us-central1-f"
  "n1-standard-4|nvidia-tesla-p100|us-east1-b"
  "n1-standard-4|nvidia-tesla-p100|us-east1-c"
  "n1-standard-4|nvidia-tesla-p100|us-west1-a"
  "n1-standard-4|nvidia-tesla-p100|us-west1-b"
  "n1-standard-4|nvidia-tesla-p100|europe-west1-b"
  "n1-standard-4|nvidia-tesla-p100|europe-west1-d"
  "n1-standard-4|nvidia-tesla-p100|europe-west4-a"
  "n1-standard-4|nvidia-tesla-p100|asia-east1-a"
  "n1-standard-4|nvidia-tesla-p100|asia-east1-c"
  "n1-standard-4|nvidia-tesla-p100|australia-southeast1-b"
  # ── V100 ~$2.48/hr ───────────────────────────────────────────────────────
  "n1-standard-8|nvidia-tesla-v100|us-central1-a"
  "n1-standard-8|nvidia-tesla-v100|us-central1-b"
  "n1-standard-8|nvidia-tesla-v100|us-central1-c"
  "n1-standard-8|nvidia-tesla-v100|us-central1-f"
  "n1-standard-8|nvidia-tesla-v100|us-east1-b"
  "n1-standard-8|nvidia-tesla-v100|us-east1-c"
  "n1-standard-8|nvidia-tesla-v100|us-west1-a"
  "n1-standard-8|nvidia-tesla-v100|us-west1-b"
  "n1-standard-8|nvidia-tesla-v100|europe-west4-a"
  "n1-standard-8|nvidia-tesla-v100|europe-west4-b"
  "n1-standard-8|nvidia-tesla-v100|europe-west4-c"
  "n1-standard-8|nvidia-tesla-v100|asia-east1-c"
)

TOTAL=${#CANDIDATES[@]}
START_TIME=$(date +%s)
ROUND=0

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  GPU Hunter — $TOTAL combinations × cycling until success"
echo "  Ctrl+C to stop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

while true; do
  ROUND=$((ROUND + 1))
  echo "── Round $ROUND  ($(( ($(date +%s) - START_TIME) / 60 ))m elapsed) ──────────────────────"

  for entry in "${CANDIDATES[@]}"; do
    IFS='|' read -r machine_type gpu_type zone <<< "$entry"
    ELAPSED=$(( $(date +%s) - START_TIME ))
    printf "  %-12s %-30s ... " "$gpu_type" "$zone"

    ERR=$(gcloud compute instances create "$VM_NAME" \
      --project="$PROJECT" \
      --zone="$zone" \
      --machine-type="$machine_type" \
      --accelerator="type=$gpu_type,count=1" \
      --maintenance-policy=TERMINATE \
      --image-family="$IMAGE_FAMILY" \
      --image-project="$IMAGE_PROJECT" \
      --boot-disk-size=100GB \
      --boot-disk-type=pd-ssd \
      --metadata="install-nvidia-driver=True" 2>&1); RC=$?

    if [ $RC -eq 0 ]; then
      echo "✓ GOT IT!"
      echo ""
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo "  ✓  VM CREATED SUCCESSFULLY"
      echo "  GPU  : $gpu_type"
      echo "  Zone : $zone"
      echo "  Time : ${ELAPSED}s ($(( ELAPSED / 60 ))m $(( ELAPSED % 60 ))s)"
      echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
      echo ""
      echo "Updating mac_commands.sh with zone=$zone ..."
      SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
      sed -i '' "s/^ZONE=.*/ZONE=\"$zone\"/" "$SCRIPT_DIR/mac_commands.sh"
      echo ""
      echo "Next steps:"
      echo "  bash mac_commands.sh upload"
      echo "  bash mac_commands.sh ssh"
      exit 0
    fi

    REASON=$(echo "$ERR" | grep -oE 'QUOTA_EXCEEDED|ZONE_RESOURCE_POOL_EXHAUSTED|stockout|resourcesNotAvailable' | head -1)
    echo "${REASON:-unavailable}"
  done

  echo ""
  echo "  All $TOTAL combinations tried. Waiting 60s before round $((ROUND+1))..."
  sleep 60
done
