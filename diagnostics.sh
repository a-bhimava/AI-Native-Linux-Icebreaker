#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# diagnostics.sh — Privileged Brain Training Diagnostics
# Produces a full report: base model, LoRA config, training run, results.
# Run from your Mac: bash diagnostics.sh
# Run with --vm to also pull live data from the GCP VM.
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── GCP config (read from mac_commands.sh) ────────────────────────────────────
VM_NAME=$(grep '^VM_NAME=' "$SCRIPT_DIR/mac_commands.sh" 2>/dev/null | cut -d'"' -f2)
ZONE=$(grep    '^ZONE='    "$SCRIPT_DIR/mac_commands.sh" 2>/dev/null | cut -d'"' -f2)
PROJECT=$(grep '^PROJECT=' "$SCRIPT_DIR/mac_commands.sh" 2>/dev/null | cut -d'"' -f2)
FETCH_VM=false
[[ "${1:-}" == "--vm" ]] && FETCH_VM=true

# ── Colour helpers ────────────────────────────────────────────────────────────
BOLD=$'\033[1m'; RESET=$'\033[0m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'

header()  { echo; echo "${BOLD}${CYAN}━━━  $*  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"; }
ok()      { echo "  ${GREEN}✓${RESET}  $*"; }
warn()    { echo "  ${YELLOW}⚠${RESET}  $*"; }
missing() { echo "  ${RED}✗${RESET}  $*"; }
kv()      { printf "  %-32s %s\n" "$1" "${BOLD}$2${RESET}"; }

# ── Helpers ───────────────────────────────────────────────────────────────────

human_size() {
  local bytes=$1
  if   (( bytes >= 1073741824 )); then printf "%.1f GB" "$(echo "scale=1; $bytes/1073741824" | bc)"
  elif (( bytes >= 1048576   )); then printf "%.1f MB" "$(echo "scale=1; $bytes/1048576"    | bc)"
  elif (( bytes >= 1024      )); then printf "%.1f KB" "$(echo "scale=1; $bytes/1024"       | bc)"
  else printf "%d B" "$bytes"; fi
}

file_size() {
  local f="$1"
  [ -f "$f" ] && human_size "$(wc -c < "$f")" || echo "—"
}

json_field() {
  # json_field file key   →  extracts "key": "value" or "key": number
  local f="$1" k="$2"
  [ -f "$f" ] && python3 -c "
import json,sys
try:
    d=json.load(open('$f'))
    v=d.get('$k')
    print(v if v is not None else '—')
except: print('—')
" 2>/dev/null || echo "—"
}

json_array() {
  local f="$1" k="$2"
  [ -f "$f" ] && python3 -c "
import json,sys
try:
    d=json.load(open('$f'))
    v=d.get('$k')
    print(', '.join(str(x) for x in v) if isinstance(v,list) else str(v) if v else '—')
except: print('—')
" 2>/dev/null || echo "—"
}

grep_log() {
  # grep_log logfile pattern  →  last matching line, stripped
  local f="$1" pat="$2"
  [ -f "$f" ] && grep -E "$pat" "$f" | tail -1 | sed 's/^[0-9-]* [0-9:,]*  //' || echo "—"
}

extract_metric() {
  # Extract a JSON value from a log line containing {...}
  # extract_metric logfile json_key
  local f="$1" k="$2"
  [ -f "$f" ] && grep -oE "'\''$k'\''[[:space:]]*:[[:space:]]*'\''[^'\'']*'\''" "$f" \
    | tail -1 | sed "s/.*: '\(.*\)'/\1/" || echo "—"
}

# ── Paths ─────────────────────────────────────────────────────────────────────
SFT_ADAPTER="$SCRIPT_DIR/training/adapters/sft/final"
DPO_ADAPTER="$SCRIPT_DIR/training/adapters/dpo/final"
SFT_LOG="$SCRIPT_DIR/training/logs/sft_train.log"
DPO_LOG="$SCRIPT_DIR/training/logs/dpo_train.log"
ADAPTER_CFG="$SFT_ADAPTER/adapter_config.json"
GGUF="$SCRIPT_DIR/training/gguf/privileged-brain.gguf"
MERGED="$SCRIPT_DIR/training/merged"
TRAIN_DATA="$SCRIPT_DIR/train.jsonl"
VALID_DATA="$SCRIPT_DIR/valid.jsonl"

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo "${BOLD}║     Privileged Brain — Training Diagnostics Report      ║${RESET}"
echo "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo "  Generated: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  Script dir: $SCRIPT_DIR"

# ─────────────────────────────────────────────────────────────────────────────
header "1. BASE MODEL"
BASE_MODEL=$(json_field "$ADAPTER_CFG" "base_model_name_or_path")
kv "Model ID:"           "$BASE_MODEL"
kv "Architecture:"       "Qwen2.5-Coder (Transformer, GQA)"
kv "Parameters:"         "1.5 B"
kv "Context length:"     "32,768 tokens"
kv "Training regime:"    "Instruction-tuned (base for SFT)"
kv "Source:"             "HuggingFace — Qwen/Qwen2.5-Coder-1.5B-Instruct"

# ─────────────────────────────────────────────────────────────────────────────
header "2. TRAINING DATA"
if [ -f "$TRAIN_DATA" ]; then
  TRAIN_COUNT=$(wc -l < "$TRAIN_DATA")
  TRAIN_SIZE=$(file_size "$TRAIN_DATA")
  ok "train.jsonl found"
  kv "  Training examples:"  "$TRAIN_COUNT"
  kv "  File size:"          "$TRAIN_SIZE"
else
  missing "train.jsonl not found locally (may be on VM)"
fi
if [ -f "$VALID_DATA" ]; then
  VALID_COUNT=$(wc -l < "$VALID_DATA")
  VALID_SIZE=$(file_size "$VALID_DATA")
  ok "valid.jsonl found"
  kv "  Validation examples:" "$VALID_COUNT"
  kv "  File size:"           "$VALID_SIZE"
else
  missing "valid.jsonl not found locally"
fi
kv "  Data format:"          "Chat (messages: [{role, content}])"
kv "  Train/valid split:"    "90% / 10%"

# ─────────────────────────────────────────────────────────────────────────────
header "3. LoRA / PEFT CONFIGURATION"
if [ -f "$ADAPTER_CFG" ]; then
  ok "adapter_config.json found"
  kv "  PEFT type:"          "$(json_field "$ADAPTER_CFG" "peft_type")"
  kv "  Task type:"          "$(json_field "$ADAPTER_CFG" "task_type")"
  kv "  LoRA rank (r):"      "$(json_field "$ADAPTER_CFG" "r")"
  kv "  LoRA alpha:"         "$(json_field "$ADAPTER_CFG" "lora_alpha")"
  kv "  LoRA dropout:"       "$(json_field "$ADAPTER_CFG" "lora_dropout")"
  kv "  Bias:"               "$(json_field "$ADAPTER_CFG" "bias")"
  kv "  Use DoRA:"           "$(json_field "$ADAPTER_CFG" "use_dora")"
  kv "  Use RSLoRA:"         "$(json_field "$ADAPTER_CFG" "use_rslora")"
  kv "  PEFT version:"       "$(json_field "$ADAPTER_CFG" "peft_version")"
  kv "  Target modules:"     "$(json_array "$ADAPTER_CFG" "target_modules")"
  kv "  Inference mode:"     "$(json_field "$ADAPTER_CFG" "inference_mode")"
else
  missing "adapter_config.json not found"
  warn "Adapter may not have been downloaded yet. Run: bash mac_commands.sh download"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "4. SFT TRAINING RUN"
if [ -f "$SFT_LOG" ]; then
  ok "SFT log found: $SFT_LOG"
  LOG_SIZE=$(file_size "$SFT_LOG")
  LOG_LINES=$(wc -l < "$SFT_LOG")
  kv "  Log size / lines:"   "$LOG_SIZE / $LOG_LINES lines"

  # Extract start/end timestamps
  FIRST_TS=$(head -1 "$SFT_LOG" | grep -oE '^[0-9-]+ [0-9:]+' || echo "—")
  LAST_TS=$(tail -1  "$SFT_LOG" | grep -oE '^[0-9-]+ [0-9:]+' || echo "—")
  kv "  Started:"            "$FIRST_TS"
  kv "  Last entry:"         "$LAST_TS"

  # GPU info
  GPU_LINE=$(grep -E "GPU[[:space:]]*:" "$SFT_LOG" | head -1 | sed 's/.*GPU[[:space:]]*: //')
  VRAM_LINE=$(grep -E "VRAM" "$SFT_LOG" | head -1 | sed 's/.*VRAM: //')
  kv "  GPU:"                "${GPU_LINE:-—}"
  kv "  VRAM:"               "${VRAM_LINE:-—}"

  # Training config from log
  EPOCHS_LINE=$(grep -E "Epochs" "$SFT_LOG" | head -1 | sed 's/.*Epochs[[:space:]]*: //')
  BATCH_LINE=$(grep -E "Eff. batch" "$SFT_LOG" | head -1 | sed 's/.*Eff. batch[[:space:]]*: //')
  SEQ_LINE=$(grep -E "Seq length" "$SFT_LOG" | head -1 | sed 's/.*Seq length[[:space:]]*: //')
  LR_LINE=$(grep -oE "learning_rate['\"]?[[:space:]]*:[[:space:]]*[0-9e.+-]+" "$SFT_LOG" | head -1 | grep -oE "[0-9e.+-]+$")
  kv "  Epochs:"             "${EPOCHS_LINE:-—}"
  kv "  Effective batch:"    "${BATCH_LINE:-—}"
  kv "  Sequence length:"    "${SEQ_LINE:-—}"
  kv "  Learning rate:"      "${LR_LINE:-—}"

  # Training hardware/precision
  FP_LINE=$(grep -E "fp16/bf16" "$SFT_LOG" | head -1 | sed 's/.*fp16\/bf16[[:space:]]*: //')
  kv "  fp16/bf16:"          "${FP_LINE:-—}"

  # Steps / progress
  TOTAL_STEPS=$(grep -oE "[0-9]+/[0-9]+ \[" "$SFT_LOG" | tail -1 | grep -oE "[0-9]+$" | head -1)
  LAST_STEP=$(grep -oE "[0-9]+/[0-9]+ \[" "$SFT_LOG" | tail -1 | grep -oE "^[0-9]+")
  kv "  Total steps:"        "${TOTAL_STEPS:-—}"
  kv "  Last step logged:"   "${LAST_STEP:-—}"

  # Loss curve (extract eval_loss values chronologically)
  echo ""
  echo "  ${BOLD}Eval loss progression:${RESET}"
  grep -oE "'eval_loss': '[0-9.e+-]+'" "$SFT_LOG" 2>/dev/null | \
    awk -F"'" 'NR<=1{print "    step ~200  : "$4} NR==2{print "    step ~600  : "$4} NR==3{print "    step ~1000 : "$4} NR==4{print "    step ~2000 : "$4} END{if(NR>4) print "    final      : "$4}' \
    || echo "    (no eval loss entries in local log)"

  # Training time
  TIME_LINE=$(grep -E "Training complete in|complete in" "$SFT_LOG" | tail -1 | sed 's/.*complete in //')
  if [ -n "$TIME_LINE" ]; then
    ok "Training complete: $TIME_LINE"
  else
    ELAPSED=$(python3 -c "
from datetime import datetime
import re, sys
lines = open('$SFT_LOG').readlines()
ts = []
for l in lines:
    m = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', l)
    if m:
        ts.append(datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
if len(ts) >= 2:
    d = ts[-1] - ts[0]
    h, r = divmod(int(d.total_seconds()), 3600)
    m, s = divmod(r, 60)
    print(f'{h}h {m}m {s}s')
else:
    print('—')
" 2>/dev/null)
    kv "  Elapsed (log span):" "$ELAPSED"
    warn "No 'Training complete' line found — may still be running or was interrupted"
  fi
else
  missing "SFT log not found locally"
  warn "Logs are on the VM. Use: bash mac_commands.sh monitor"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "5. DPO TRAINING RUN"
if [ -f "$DPO_LOG" ]; then
  ok "DPO log found: $DPO_LOG"
  DPO_LINES=$(wc -l < "$DPO_LOG")
  kv "  Log lines:"  "$DPO_LINES"
  DPO_TIME=$(grep -E "DPO complete in" "$DPO_LOG" | tail -1 | sed 's/.*DPO complete in //')
  [ -n "$DPO_TIME" ] && ok "DPO complete: $DPO_TIME" || warn "DPO completion line not found"
  # DPO eval loss
  DPO_LOSS=$(grep -oE "'eval_loss': '[0-9.e+-]+'" "$DPO_LOG" 2>/dev/null | tail -1 | grep -oE "[0-9.e+-]+")
  [ -n "$DPO_LOSS" ] && kv "  Final eval loss:" "$DPO_LOSS" || kv "  Final eval loss:" "—"
else
  missing "DPO log not found locally"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "6. OUTPUT ARTIFACTS"

echo "  ${BOLD}SFT Adapter:${RESET}"
if [ -d "$SFT_ADAPTER" ]; then
  ok "Directory exists: $SFT_ADAPTER"
  for f in adapter_model.safetensors adapter_config.json tokenizer.json; do
    fp="$SFT_ADAPTER/$f"
    if [ -f "$fp" ]; then
      ok "  $f  ($(file_size "$fp"))"
    else
      missing "  $f  MISSING"
    fi
  done
else
  missing "SFT adapter not found — run: bash mac_commands.sh download"
fi

echo ""
echo "  ${BOLD}DPO Adapter:${RESET}"
if [ -d "$DPO_ADAPTER" ]; then
  ok "Directory exists: $DPO_ADAPTER"
  for f in adapter_model.safetensors adapter_config.json; do
    fp="$DPO_ADAPTER/$f"
    [ -f "$fp" ] && ok "  $f  ($(file_size "$fp"))" || missing "  $f  MISSING"
  done
else
  warn "DPO adapter not downloaded (or DPO did not complete)"
fi

echo ""
echo "  ${BOLD}Merged Model:${RESET}"
if [ -f "$MERGED/config.json" ]; then
  MERGED_SIZE=$(du -sh "$MERGED" 2>/dev/null | cut -f1)
  ok "Merged model exists (~$MERGED_SIZE)"
else
  warn "Merged model not present (run 05_convert_and_import.sh)"
fi

echo ""
echo "  ${BOLD}GGUF:${RESET}"
if [ -f "$GGUF" ]; then
  ok "GGUF exists: $GGUF  ($(file_size "$GGUF"))"
else
  warn "GGUF not found (run 05_convert_and_import.sh)"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "7. OLLAMA MODEL STATUS"
if command -v ollama &>/dev/null; then
  ok "Ollama is installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
  if ollama list 2>/dev/null | grep -q "privileged-brain"; then
    MODEL_LINE=$(ollama list 2>/dev/null | grep "privileged-brain")
    ok "Model is registered in Ollama:"
    echo "    $MODEL_LINE"
  else
    warn "privileged-brain not in Ollama — run: cd training/gguf && ollama create privileged-brain -f Modelfile"
  fi
  OLLAMA_RUNNING=false
  if curl -s http://localhost:11434/api/tags &>/dev/null; then
    ok "Ollama server is running"
    OLLAMA_RUNNING=true
  else
    warn "Ollama server not running (open the Ollama app)"
  fi
else
  missing "Ollama not installed"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "8. GCP VM STATUS"
kv "  VM name:"   "$VM_NAME"
kv "  Zone:"      "$ZONE"
kv "  Project:"   "$PROJECT"

if $FETCH_VM; then
  echo ""
  echo "  Connecting to VM (this may take a moment)..."
  VM_STATUS=$(gcloud compute instances describe "$VM_NAME" \
    --zone="$ZONE" --project="$PROJECT" \
    --format="value(status)" 2>/dev/null || echo "UNKNOWN")
  kv "  VM status:" "$VM_STATUS"

  if [ "$VM_STATUS" = "RUNNING" ]; then
    ok "VM is running"
    echo ""
    echo "  ${BOLD}GPU (nvidia-smi):${RESET}"
    gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
      --command="nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu --format=csv,noheader 2>/dev/null || echo 'nvidia-smi unavailable'" 2>/dev/null \
      | sed 's/^/    /'

    echo ""
    echo "  ${BOLD}Training screen session:${RESET}"
    SCREEN_STATUS=$(gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
      --command="screen -list 2>/dev/null | grep pb_training || echo 'No pb_training session'" 2>/dev/null)
    echo "    $SCREEN_STATUS"

    echo ""
    echo "  ${BOLD}Last 10 log lines (VM):${RESET}"
    gcloud compute ssh "$VM_NAME" --zone="$ZONE" --project="$PROJECT" \
      --command="tail -10 ~/training/logs/sft_train.log 2>/dev/null || echo '(log not found)'" 2>/dev/null \
      | sed 's/^/    /'
  else
    warn "VM is not running (status: $VM_STATUS)"
    echo "  To restart: bash mac_commands.sh create"
  fi
else
  echo "  (VM check skipped — run with --vm to include live VM data)"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "9. COMPLETION SUMMARY"

SFT_DONE=false; DPO_DONE=false; GGUF_DONE=false; OLLAMA_DONE=false

[ -f "$SFT_ADAPTER/adapter_model.safetensors" ] && SFT_DONE=true
[ -f "$DPO_ADAPTER/adapter_model.safetensors" ] && DPO_DONE=true
[ -f "$GGUF" ]                                  && GGUF_DONE=true
command -v ollama &>/dev/null && \
  ollama list 2>/dev/null | grep -q "privileged-brain" && OLLAMA_DONE=true

print_status() {
  local label="$1" done="$2" msg_ok="$3" msg_fail="$4"
  if $done; then ok "$(printf '%-28s' "$label") $msg_ok"
  else           warn "$(printf '%-28s' "$label") $msg_fail"
  fi
}

print_status "SFT training:"       $SFT_DONE  "COMPLETE ✓"  "adapter not found locally"
print_status "DPO training:"       $DPO_DONE  "COMPLETE ✓"  "not downloaded (or not run)"
print_status "GGUF conversion:"    $GGUF_DONE "COMPLETE ✓"  "run 05_convert_and_import.sh"
print_status "Ollama import:"      $OLLAMA_DONE "COMPLETE ✓" "run: ollama create privileged-brain -f Modelfile"

echo ""
ALL_DONE=false
($SFT_DONE && $GGUF_DONE && $OLLAMA_DONE) && ALL_DONE=true

if $ALL_DONE; then
  echo "  ${GREEN}${BOLD}★  Model is fully trained, converted, and ready to use.${RESET}"
  echo ""
  echo "  Quick test:"
  echo "    ollama run privileged-brain \"list all listening TCP ports\""
  echo ""
  echo "  Full evaluation:"
  echo "    bash $SCRIPT_DIR/07_evaluate.sh"
else
  echo "  ${YELLOW}${BOLD}⚠  Pipeline not fully complete. See warnings above.${RESET}"
fi

echo ""
echo "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
