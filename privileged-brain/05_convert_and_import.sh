#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo " Privileged Brain — GGUF Conversion"
echo " & Ollama Import"
echo "========================================"

# --- Step 1: Fuse LoRA adapters into base model ---
echo "[1/5] Fusing LoRA adapter into base model..."
python3 scripts/fuse_lora.py --adapter dpo --output-dir conversion/fused_model
echo ""

# --- Step 2: Get llama.cpp conversion script ---
echo "[2/5] Locating llama.cpp convert script..."

CONVERT_SCRIPT=""

# Try brew-installed llama.cpp first
BREW_PREFIX=$(brew --prefix llama.cpp 2>/dev/null || echo "")
if [ -n "$BREW_PREFIX" ] && [ -f "$BREW_PREFIX/convert_hf_to_gguf.py" ]; then
  CONVERT_SCRIPT="$BREW_PREFIX/convert_hf_to_gguf.py"
  echo "  Found at $CONVERT_SCRIPT"
fi

# Fallback: clone llama.cpp repo (just the convert script)
if [ -z "$CONVERT_SCRIPT" ]; then
  echo "  Not found in brew prefix. Cloning llama.cpp for conversion scripts..."
  if [ ! -d "conversion/llama.cpp" ]; then
    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git conversion/llama.cpp
    pip3 install --quiet -r conversion/llama.cpp/requirements/requirements-convert_hf_to_gguf.txt
  fi
  CONVERT_SCRIPT="conversion/llama.cpp/convert_hf_to_gguf.py"
  echo "  Using $CONVERT_SCRIPT"
fi

# --- Step 3: Convert HuggingFace model to GGUF (float16) ---
echo ""
echo "[3/5] Converting fused model to GGUF format..."
python3 "$CONVERT_SCRIPT" \
  conversion/fused_model \
  --outfile conversion/models/privileged-brain-f16.gguf \
  --outtype f16

echo "  GGUF created: conversion/models/privileged-brain-f16.gguf"
echo ""

# --- Step 4: Quantize to Q4_K_M ---
echo "[4/5] Quantizing to Q4_K_M (~1.1GB)..."
llama-quantize \
  conversion/models/privileged-brain-f16.gguf \
  conversion/models/privileged-brain-q4_k_m.gguf \
  Q4_K_M

SIZE=$(du -sh conversion/models/privileged-brain-q4_k_m.gguf | cut -f1)
echo "  Quantized model: conversion/models/privileged-brain-q4_k_m.gguf ($SIZE)"

# INV-7: Record SHA-256 of quantized model
CKSUM_FILE="$(cd "$SCRIPT_DIR/.." && pwd)/models/checksums.sha256"
if [ -d "$(dirname "$CKSUM_FILE")" ]; then
  echo "Recording model checksum (INV-7)..."
  sha256sum conversion/models/privileged-brain-q4_k_m.gguf >> "$CKSUM_FILE" 2>/dev/null || true
  echo "  Checksum appended to $CKSUM_FILE"
fi
echo ""

# --- Step 5: Import into Ollama ---
echo "[5/5] Importing into Ollama as 'privileged-brain'..."

# Write the Modelfile with absolute path to the GGUF
GGUF_ABS="$(pwd)/conversion/models/privileged-brain-q4_k_m.gguf"
cat > conversion/Modelfile <<MODELFILE_EOF
FROM $GGUF_ABS

SYSTEM """You control a Linux system through MCP tool calls.
You receive a JSON object with intent_id, allowed_tool, and tool_schema.
Execute EXACTLY ONE tool call. Output ONLY the JSON call.

Output format: {"tool":"<allowed_tool>","params":<params matching tool_schema>}

Rules:
- Use only the allowed_tool specified in the input
- params must match tool_schema exactly
- No explanation. No reasoning. One JSON object only.
- If input is malformed: output {"tool":"system.status","params":{}}"""

PARAMETER temperature 0.1
PARAMETER top_p 0.9
PARAMETER num_predict 256
PARAMETER num_ctx 4096
PARAMETER stop "<|im_end|>"
MODELFILE_EOF

ollama create privileged-brain -f conversion/Modelfile

echo ""
echo "========================================"
echo " Import complete!"
echo "========================================"
echo ""
echo "Quick test:"
echo "  ollama run privileged-brain 'list all running services'"
echo ""
echo "Next: bash 06_start_inference.sh"
