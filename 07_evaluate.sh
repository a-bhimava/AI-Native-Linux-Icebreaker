#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 07_evaluate.sh
# Side-by-side comparison: baseline Qwen2.5-Coder vs your fine-tuned model.
# Run on your Mac after 05_convert_and_import.sh
# ─────────────────────────────────────────────────────────────────────────────

FINETUNED="privileged-brain"
BASELINE="qwen2.5-coder:1.5b-instruct"   # pulled from Ollama hub

echo "========================================"
echo " Privileged Brain — Evaluation"
echo "========================================"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────
if ! command -v ollama &>/dev/null; then
  echo "ERROR: Ollama not found. Install from https://ollama.com/download"
  exit 1
fi

# Pull baseline if not already present
if ! ollama list | grep -q "qwen2.5-coder:1.5b"; then
  echo "Pulling baseline model ($BASELINE)..."
  ollama pull "$BASELINE"
fi

# Check fine-tuned model exists
if ! ollama list | grep -q "$FINETUNED"; then
  echo "ERROR: Fine-tuned model '$FINETUNED' not found."
  echo "Run: bash 05_convert_and_import.sh"
  exit 1
fi

# ── Test prompts ──────────────────────────────────────────────────────────────
PROMPTS=(
  "List all listening TCP ports on this machine."
  "Show disk usage of each directory under /home, sorted by size."
  "Find all files modified in the last 24 hours under /var/log."
  "Kill the process using port 8080."
  "Show the top 5 processes by memory usage."
  "Check if a remote host at 192.168.1.1 port 443 is reachable."
  "Tail the last 50 lines of syslog and filter for errors."
  "Create a cron job that runs /home/user/backup.sh every day at 2 AM."
)

PASS=0
TOTAL=${#PROMPTS[@]}

echo "Running $TOTAL test prompts..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

for i in "${!PROMPTS[@]}"; do
  PROMPT="${PROMPTS[$i]}"
  NUM=$((i + 1))

  echo "── Test $NUM/$TOTAL ─────────────────────────────────────────────"
  echo "Prompt: $PROMPT"
  echo ""

  echo "[ BASELINE: $BASELINE ]"
  BASE_OUT=$(ollama run "$BASELINE" "$PROMPT" 2>/dev/null)
  echo "$BASE_OUT"
  echo ""

  echo "[ FINE-TUNED: $FINETUNED ]"
  FT_OUT=$(ollama run "$FINETUNED" "$PROMPT" 2>/dev/null)
  echo "$FT_OUT"
  echo ""

  # Simple heuristic: fine-tuned response contains a shell command (has backtick or $)
  if echo "$FT_OUT" | grep -qE '`[^`]+`|\$\s|\bsudo\b|\bss\b|\bnetstat\b|\bfind\b|\bkill\b|\btail\b|\bcron\b'; then
    echo "  ✓ Fine-tuned gave a command-like answer"
    PASS=$((PASS + 1))
  else
    echo "  ⚠ Fine-tuned answer may not contain a direct command"
  fi

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
done

echo ""
echo "========================================"
echo " Evaluation complete"
echo " Fine-tuned heuristic score: $PASS / $TOTAL"
echo ""
if [ "$PASS" -ge 6 ]; then
  echo " ✓ Model looks good — consistently produces commands."
elif [ "$PASS" -ge 3 ]; then
  echo " ~ Partial improvement — consider more training data or epochs."
else
  echo " ✗ Low score — check that the model was imported correctly."
fi
echo "========================================"
