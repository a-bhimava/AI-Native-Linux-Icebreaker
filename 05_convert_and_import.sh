#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 05_convert_and_import.sh
# Fuses the LoRA adapter into the base model, converts to GGUF, imports to Ollama.
# Run on your MAC after: bash mac_commands.sh download
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTERS_DIR="$SCRIPT_DIR/training/adapters"
MERGED_DIR="$SCRIPT_DIR/training/merged"
GGUF_DIR="$SCRIPT_DIR/training/gguf"
LLAMA_CPP_DIR="$SCRIPT_DIR/llama.cpp"
MODEL_NAME="privileged-brain"
BASE_MODEL="Qwen/Qwen2.5-Coder-1.5B-Instruct"

echo "========================================"
echo " Privileged Brain — Convert & Import"
echo "========================================"
echo ""

# ── 1. Find adapter (DPO preferred, SFT fallback) ────────────────────────────
if [ -d "$ADAPTERS_DIR/dpo/final" ]; then
  ADAPTER="$ADAPTERS_DIR/dpo/final"
  echo "✓ Using DPO adapter: $ADAPTER"
elif [ -d "$ADAPTERS_DIR/sft/final" ]; then
  ADAPTER="$ADAPTERS_DIR/sft/final"
  echo "⚠ DPO adapter not found — using SFT adapter: $ADAPTER"
else
  echo "ERROR: No adapter found in $ADAPTERS_DIR"
  echo "Run first:  bash mac_commands.sh download"
  exit 1
fi

# ── 2. Check prerequisites ────────────────────────────────────────────────────
if ! command -v ollama &>/dev/null; then
  echo "ERROR: Ollama not installed."
  echo "Install from: https://ollama.com/download"
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found."
  exit 1
fi

# Install required Python packages if needed
echo "Checking Python dependencies..."
python3 -c "import transformers, peft, torch" 2>/dev/null || {
  echo "Installing transformers + peft..."
  pip3 install -q transformers peft torch accelerate
}

# ── 3. Merge LoRA adapter into base model ────────────────────────────────────
mkdir -p "$MERGED_DIR" "$GGUF_DIR"

if [ -f "$MERGED_DIR/config.json" ]; then
  echo "✓ Merged model already exists — skipping merge step."
else
  echo ""
  echo "Merging LoRA adapter into base model (~5–10 min, ~6 GB RAM)..."
  python3 - <<PYEOF
from transformers import AutoModelForCausalLM
from peft import PeftModel
import torch

base_id     = "$BASE_MODEL"
adapter_dir = "$ADAPTER"
out_dir     = "$MERGED_DIR"

print(f"  Loading base model: {base_id}")
model = AutoModelForCausalLM.from_pretrained(
    base_id,
    torch_dtype=torch.float16,
    device_map="cpu",
    trust_remote_code=True,
)

print(f"  Loading adapter: {adapter_dir}")
model = PeftModel.from_pretrained(model, adapter_dir)

print("  Merging weights...")
model = model.merge_and_unload()

print(f"  Saving to: {out_dir}")
model.save_pretrained(out_dir, safe_serialization=True)
print("  Model merge complete!")
PYEOF
fi

# ── Save tokenizer (always, separate from model merge) ───────────────────────
if [ ! -f "$MERGED_DIR/tokenizer.json" ]; then
  echo "Saving tokenizer..."
  python3 -c "
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained('$BASE_MODEL', trust_remote_code=True)
tok.save_pretrained('$MERGED_DIR')
print('  Tokenizer saved via Python.')
" 2>/dev/null
  # If Python failed, copy directly from adapter (works regardless of transformers version)
  if [ ! -f "$MERGED_DIR/tokenizer.json" ]; then
    echo "  Python tokenizer save failed — copying files from adapter..."
    cp "$ADAPTER/tokenizer.json"        "$MERGED_DIR/"
    cp "$ADAPTER/tokenizer_config.json" "$MERGED_DIR/"
    echo "  Tokenizer files copied."
  fi
else
  echo "✓ Tokenizer already present in merged dir."
fi

# ── 4. Get llama.cpp for GGUF conversion ─────────────────────────────────────
if [ ! -d "$LLAMA_CPP_DIR" ]; then
  echo ""
  echo "Cloning llama.cpp (for GGUF conversion)..."
  git clone --depth=1 https://github.com/ggerganov/llama.cpp "$LLAMA_CPP_DIR"
fi

echo "Installing llama.cpp Python requirements..."
pip3 install -q gguf sentencepiece 2>/dev/null || true
pip3 install -q -r "$LLAMA_CPP_DIR/requirements.txt" 2>/dev/null || true

# ── 5. Convert to GGUF ───────────────────────────────────────────────────────
GGUF_FILE="$GGUF_DIR/${MODEL_NAME}.gguf"

if [ -f "$GGUF_FILE" ]; then
  echo "✓ GGUF file already exists — skipping conversion."
else
  echo ""
  echo "Converting to GGUF (f16)..."
  python3 "$LLAMA_CPP_DIR/convert_hf_to_gguf.py" \
    "$MERGED_DIR" \
    --outfile "$GGUF_FILE" \
    --outtype f16
  echo "✓ GGUF saved: $GGUF_FILE"
fi

# ── 6. Write Ollama Modelfile ─────────────────────────────────────────────────
MODELFILE="$GGUF_DIR/Modelfile"
cat > "$MODELFILE" << MEOF
FROM ./${MODEL_NAME}.gguf

PARAMETER temperature 0.1
PARAMETER top_p 0.9
PARAMETER num_ctx 4096
PARAMETER stop "<|im_end|>"

SYSTEM """You are Privileged Brain, a system-administration and DevOps expert.
You give precise, runnable shell commands and explain what they do.
Always prefer brevity — one working command beats a paragraph of prose."""
MEOF

echo "✓ Modelfile written: $MODELFILE"

# ── 7. Import into Ollama ─────────────────────────────────────────────────────
echo ""
echo "Importing into Ollama as '$MODEL_NAME'..."
cd "$GGUF_DIR"
ollama create "$MODEL_NAME" -f Modelfile

echo ""
echo "========================================"
echo " ✓ Done! Model imported as: $MODEL_NAME"
echo ""
echo " Quick test:"
echo "   ollama run $MODEL_NAME 'list all listening TCP ports'"
echo ""
echo " Full evaluation:"
echo "   bash $SCRIPT_DIR/07_evaluate.sh"
echo "========================================"
