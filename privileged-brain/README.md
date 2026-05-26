# Privileged Brain — Fine-Tuning & Inference Pipeline

Qwen 2.5 Coder 1.5B fine-tuned for NL→Bash translation. Runs on Apple M4 (16GB) via PyTorch MPS. Inference via Ollama or llama.cpp with speculative decoding.

## Run order

```bash
bash 01_setup.sh               # Install Ollama, llama.cpp, Python deps (~10 min)
bash 02_get_data.sh            # Download NL2Bash + bash-commands-dataset
bash 03_generate_synthetic.sh  # Optional — needs ANTHROPIC_API_KEY
bash 04_train.sh               # SFT + DPO (~4-7 hours on M4)
bash 05_convert_and_import.sh  # Merge LoRA → GGUF → Ollama import
bash 06_start_inference.sh     # Start inference server
bash 07_evaluate.sh            # FEH evaluation vs baseline
```

## Time estimates (Apple M4, 16GB)

| Step | Time |
|---|---|
| Setup + data download | 15-30 min |
| SFT training (3 epochs, ~15k examples) | 3-6 hours |
| DPO training (2 epochs, ~1k pairs) | 45-90 min |
| GGUF conversion + quantization | 10-15 min |
| **Total** | **5-9 hours** |

Run `04_train.sh` overnight.

## Inference endpoints

| Mode | Command | Port | Notes |
|---|---|---|---|
| Ollama | `bash 06_start_inference.sh 1` | 11434 | Easy, no speculative decoding |
| llama.cpp | `bash 06_start_inference.sh 2` | 8080 | Speculative decoding, grammar |

## Test a query

```bash
# Ollama
ollama run privileged-brain "block all traffic on port 3306"

# API
curl http://127.0.0.1:11434/api/chat -d '{
  "model": "privileged-brain",
  "messages": [{"role": "user", "content": "show disk usage"}],
  "stream": false
}'
```

## Key files

| File | Purpose |
|---|---|
| `scripts/sft_train.py` | SFT fine-tuning (TRL + LoRA + MPS) |
| `scripts/dpo_train.py` | DPO preference training |
| `scripts/fuse_lora.py` | Merge adapter into base model |
| `scripts/eval_feh.py` | Functional Equivalence Heuristic |
| `inference/grammar/mcp_tool_call.gbnf` | Grammar for constrained decoding |
| `conversion/Modelfile` | Ollama model definition (generated) |
