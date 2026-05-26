#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── RAM CONTROL ────────────────────────────────────────────────────────────────
# Run with LOW_MEM=1 to enable memory-saving mode (~5-7 GB instead of ~9-11 GB):
#   LOW_MEM=1 bash 04_train.sh
#
# Normal mode (default): batch=2, seq=512            → ~9-11 GB, faster
# Low-memory mode:       batch=1, seq=256, grad_ckpt → ~5-7 GB, ~20% slower/step
# ──────────────────────────────────────────────────────────────────────────────
LOW_MEM=${LOW_MEM:-0}

# Resume SFT from latest checkpoint if it exists (safe after a crash)
RESUME=${RESUME:-0}

LOW_MEM_FLAG=""
RESUME_FLAG=""
[ "$LOW_MEM" = "1" ] && LOW_MEM_FLAG="--low-memory"
[ "$RESUME"  = "1" ] && RESUME_FLAG="--resume"

echo "========================================"
echo " Privileged Brain — Fine-Tuning"
echo " Apple M4 MPS Backend"
echo "========================================"
echo ""
echo " Low-memory mode : $( [ "$LOW_MEM" = "1" ] && echo "ON  (~5-7 GB)" || echo "OFF (~9-11 GB)")"
echo " Resume SFT      : $( [ "$RESUME"  = "1" ] && echo "YES" || echo "NO")"
echo ""
echo " TIP: Run with LOW_MEM=1 if you see OOM crashes or heavy swap."
echo " TIP: Run with RESUME=1 to continue a crashed SFT run."
echo " TIP: This script is automatically wrapped with caffeinate"
echo "      so the Mac will not sleep during training."
echo ""

# --- Verify data exists ---
if [ ! -f data/processed/train.jsonl ]; then
  echo "ERROR: data/processed/train.jsonl not found."
  echo "       Run bash 02_get_data.sh first."
  exit 1
fi

TRAIN_COUNT=$(wc -l < data/processed/train.jsonl)
VALID_COUNT=$(wc -l < data/processed/valid.jsonl)
echo "Training examples   : $TRAIN_COUNT"
echo "Validation examples : $VALID_COUNT"
echo ""

# ── Phase 1: SFT ──────────────────────────────────────────────────────────────
echo "========================================"
echo " Phase 1: SFT Training"
if [ "$LOW_MEM" = "1" ]; then
  echo " ~5-7 hrs on M4 (low-memory mode)"
else
  echo " ~3-6 hrs on M4 (normal mode)"
fi
echo "========================================"
echo ""

# caffeinate flags: -d=no display sleep, -i=no idle sleep, -m=no disk sleep, -s=no system sleep
caffeinate -dims python3 scripts/sft_train.py \
  --epochs 3 \
  --batch-size 2 \
  --grad-accum 8 \
  --lr 2e-4 \
  --lora-rank 8 \
  $LOW_MEM_FLAG \
  $RESUME_FLAG

echo ""
echo "SFT complete. Adapter saved: training/adapters/sft/final/"
echo ""

# ── Phase 2: DPO preference pairs ─────────────────────────────────────────────
echo "========================================"
echo " Phase 2: DPO Preference Pairs"
echo "========================================"
echo ""

python3 scripts/generate_dpo_pairs.py

DPO_COUNT=$(wc -l < data/processed/dpo_pairs.jsonl)
echo "DPO pairs: $DPO_COUNT"
echo ""

# ── Phase 3: DPO Training ──────────────────────────────────────────────────────
echo "========================================"
echo " Phase 3: DPO Training"
echo " ~1 hr on M4"
echo "========================================"
echo ""

caffeinate -dims python3 scripts/dpo_train.py

echo ""
echo "DPO complete. Adapter saved: training/adapters/dpo/final/"
echo ""
echo "========================================"
echo " Training complete!"
echo "========================================"
echo ""
echo "Next: bash 05_convert_and_import.sh"
