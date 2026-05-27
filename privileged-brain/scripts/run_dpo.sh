#!/usr/bin/env bash
# Run DPO fine-tuning on the GCP VM after SFT completes.
# Upload updated generate_dpo_pairs.py first, then SSH and run this.
#
# From your local machine:
#   gcloud compute scp privileged-brain/scripts/generate_dpo_pairs.py \
#       privileged-brain-vm:~/generate_dpo_pairs.py --zone=us-west4-b
#   gcloud compute ssh privileged-brain-vm --zone=us-west4-b \
#       --command="bash ~/run_dpo.sh"
#
# Or run interactively on the VM inside a screen session.
set -euo pipefail

ZONE="us-west4-b"
VM="privileged-brain-vm"
LOG="$HOME/training/logs/dpo_run1.log"

echo "================================================"
echo " Privileged Brain — DPO Fine-Tuning"
echo "================================================"

# Verify SFT adapter exists before running DPO
if [ ! -d "$HOME/training/adapters/sft/final" ]; then
  echo "ERROR: SFT final adapter not found at ~/training/adapters/sft/final"
  echo "       Wait for the SFT training run to complete first."
  exit 1
fi
echo "SFT adapter found — OK"

# Generate DPO preference pairs
echo ""
echo "[1/2] Generating DPO preference pairs..."
cd "$HOME"
python3 generate_dpo_pairs.py
echo "      Pairs written to ~/data/dpo_pairs.jsonl"

# Verify pairs were generated
PAIR_COUNT=$(wc -l < "$HOME/data/dpo_pairs.jsonl")
echo "      Total pairs: $PAIR_COUNT"
if [ "$PAIR_COUNT" -lt 50 ]; then
  echo "ERROR: Expected at least 50 DPO pairs, got $PAIR_COUNT"
  exit 1
fi

# Run DPO training
echo ""
echo "[2/2] Starting DPO fine-tuning (~30-60 minutes on T4)..."
echo "      Logging to $LOG"
python3 dpo_train.py 2>&1 | tee "$LOG"

echo ""
echo "================================================"
echo " DPO complete — adapter saved to:"
echo "   ~/training/adapters/dpo/final"
echo ""
echo " Next: run bash 05_convert_and_import.sh (locally)"
echo "       to fuse, quantize, and import to Ollama."
echo "================================================"
