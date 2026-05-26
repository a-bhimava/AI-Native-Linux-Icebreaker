#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo " Privileged Brain — Dataset Download"
echo "========================================"

# --- Step 1: Download raw datasets ---
echo "[1/3] Downloading NL2Bash and bash-commands-dataset..."
python3 scripts/download_datasets.py

# --- Step 2: Process into ChatML format ---
echo ""
echo "[2/3] Processing into ChatML format for TRL..."
python3 scripts/process_datasets.py

# --- Step 3: Stats summary ---
echo ""
echo "[3/3] Dataset summary:"
if [ -f data/processed/train.jsonl ]; then
  TRAIN_COUNT=$(wc -l < data/processed/train.jsonl)
  VALID_COUNT=$(wc -l < data/processed/valid.jsonl)
  echo "  train.jsonl : $TRAIN_COUNT examples"
  echo "  valid.jsonl : $VALID_COUNT examples"
fi

echo ""
echo "Data ready."
echo ""
echo "Optional: bash 03_generate_synthetic.sh   (requires ANTHROPIC_API_KEY)"
echo "Or skip straight to training: bash 04_train.sh"
