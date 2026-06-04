#!/usr/bin/env bash
# Starts the full training pipeline (SFT → DPO pairs → DPO) inside a persistent
# screen session. Training continues even if your SSH connection drops.
#
# Usage:
#   bash 02_start_training.sh           # full pipeline
#   bash 02_start_training.sh sft       # SFT only (resume if checkpoint exists)
#   bash 02_start_training.sh dpo       # DPO only (after SFT is done)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Use the Python detected during setup (conda vs system) ───────────────────
if [ -f "$SCRIPT_DIR/python_env" ]; then
  source "$SCRIPT_DIR/python_env"
  PYTHON3="$PYTHON"
else
  # fallback: prefer conda python, then system python3
  PYTHON3="$( [ -x /opt/conda/bin/python ] && echo /opt/conda/bin/python || echo python3 )"
fi

MODE=${1:-full}
SESSION="pb_training"
LOG="$SCRIPT_DIR/training/logs/pipeline.log"

mkdir -p "$SCRIPT_DIR/training/logs"

# ── Check data files ──────────────────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/data/train.jsonl" ] || [ ! -f "$SCRIPT_DIR/data/valid.jsonl" ]; then
  echo "ERROR: data/train.jsonl or data/valid.jsonl not found."
  echo "       Upload them first:  bash mac_commands.sh  (section: Upload data)"
  exit 1
fi

TRAIN_COUNT=$(wc -l < "$SCRIPT_DIR/data/train.jsonl")
VALID_COUNT=$(wc -l < "$SCRIPT_DIR/data/valid.jsonl")
echo "Train examples : $TRAIN_COUNT"
echo "Valid examples : $VALID_COUNT"
echo ""

# ── Kill existing session if any ──────────────────────────────────────────────
screen -S "$SESSION" -X quit 2>/dev/null && echo "Killed existing screen session." || true

# ── Build the command to run inside screen ────────────────────────────────────
case "$MODE" in
  sft)
    CMD="\"$PYTHON3\" \"$SCRIPT_DIR/sft_train.py\" 2>&1 | tee -a $LOG"
    echo "Mode: SFT only"
    ;;
  dpo)
    CMD="\"$PYTHON3\" \"$SCRIPT_DIR/generate_dpo_pairs.py\" && \"$PYTHON3\" \"$SCRIPT_DIR/dpo_train.py\" 2>&1 | tee -a $LOG"
    echo "Mode: DPO only"
    ;;
  full|*)
    CMD="\"$PYTHON3\" \"$SCRIPT_DIR/sft_train.py\" 2>&1 | tee -a $LOG && echo '--- SFT DONE ---' >> $LOG && \"$PYTHON3\" \"$SCRIPT_DIR/generate_dpo_pairs.py\" >> $LOG 2>&1 && \"$PYTHON3\" \"$SCRIPT_DIR/dpo_train.py\" 2>&1 | tee -a $LOG && echo '--- ALL DONE ---' >> $LOG"
    echo "Mode: Full pipeline (SFT → DPO pairs → DPO)"
    ;;
esac

echo ""
echo "Starting training in screen session: $SESSION"
echo "Log file: $LOG"
echo ""

# Start screen session with the command
screen -dmS "$SESSION" bash -c "$CMD"

sleep 2

# Confirm session is running
if screen -list | grep -q "$SESSION"; then
  echo "========================================"
  echo " Training started successfully!"
  echo "========================================"
  echo ""
  echo " To watch live progress:"
  echo "   bash 03_monitor.sh"
  echo ""
  echo " To attach to the training session:"
  echo "   screen -r $SESSION"
  echo "   (Detach with: Ctrl+A then D)"
  echo ""
  echo " If your SSH disconnects: just SSH back in."
  echo " Training keeps running. Resume monitoring with:"
  echo "   bash 03_monitor.sh"
  echo ""
else
  echo "ERROR: Screen session did not start. Check the log:"
  cat "$LOG" 2>/dev/null || echo "(log file empty)"
  exit 1
fi
