#!/usr/bin/env bash
# Shows training status: GPU usage, latest checkpoint, recent log lines.
# Run this any time to check progress. Safe to run repeatedly.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SESSION="pb_training"
LOG="$SCRIPT_DIR/training/logs/pipeline.log"

echo "========================================"
echo " Privileged Brain — Training Monitor"
echo "========================================"
echo ""

# ── Session status ────────────────────────────────────────────────────────────
if screen -list 2>/dev/null | grep -q "$SESSION"; then
  echo "Status : RUNNING  (screen session '$SESSION' is active)"
else
  echo "Status : NOT RUNNING  (screen session '$SESSION' not found)"
  echo "         If training should be running: check log below for errors."
  echo "         To restart: bash 02_start_training.sh"
fi
echo ""

# ── GPU usage ─────────────────────────────────────────────────────────────────
echo "---- GPU --------------------------------------------------------"
nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu \
           --format=csv,noheader,nounits | \
  awk -F',' '{printf "  %-25s  GPU: %s%%  VRAM: %s/%s MB  Temp: %s°C\n", $1, $2, $3, $4, $5}'
echo ""

# ── Latest checkpoints ────────────────────────────────────────────────────────
echo "---- Checkpoints ------------------------------------------------"
SFT_CKPTS=$(ls -d "$SCRIPT_DIR/training/adapters/sft/checkpoint-*" 2>/dev/null | sort -V)
DPO_CKPTS=$(ls -d "$SCRIPT_DIR/training/adapters/dpo/checkpoint-*" 2>/dev/null | sort -V)

if [ -n "$SFT_CKPTS" ]; then
  LATEST_SFT=$(echo "$SFT_CKPTS" | tail -1)
  STEP=$(basename "$LATEST_SFT" | sed 's/checkpoint-//')
  echo "  SFT latest : $LATEST_SFT  (step $STEP / ~5277 total)"
  PCTG=$(awk "BEGIN {printf \"%.1f\", $STEP/5277*100}")
  echo "  SFT progress: $PCTG%"
else
  echo "  SFT checkpoints: none yet"
fi

if [ -n "$DPO_CKPTS" ]; then
  echo "  DPO latest : $(echo "$DPO_CKPTS" | tail -1)"
else
  echo "  DPO checkpoints: none yet"
fi

if [ -d "$SCRIPT_DIR/training/adapters/sft/final" ]; then
  echo "  SFT FINAL  : SAVED  ✓"
fi
if [ -d "$SCRIPT_DIR/training/adapters/dpo/final" ]; then
  echo "  DPO FINAL  : SAVED  ✓"
fi
echo ""

# ── Recent log output ─────────────────────────────────────────────────────────
echo "---- Last 30 log lines ------------------------------------------"
if [ -f "$LOG" ]; then
  tail -30 "$LOG"
else
  echo "  Log file not found: $LOG"
fi
echo ""
echo "---- End --------------------------------------------------------"
echo ""
echo "To follow live:   tail -f $LOG"
echo "To attach screen: screen -r $SESSION"
