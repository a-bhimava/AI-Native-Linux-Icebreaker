# Privileged Brain — Google Colab Fine-Tuning Guide

Use this guide to run SFT + DPO training on a free or Pro Colab GPU, then bring the adapter back to your Mac for GGUF conversion and Ollama import.

---

## Speed Comparison

| Hardware | SFT (3 epochs, 28k examples) | DPO (2 epochs, 25 pairs) |
|---|---|---|
| M4 Mac (local) | ~3–6 hrs | ~30 min |
| T4 — Colab free | ~1.5 hrs | ~10 min |
| V100 — Colab Pro | ~50 min | ~5 min |
| A100 40GB — Colab Pro | ~25 min | ~3 min |
| A100 80GB — Colab Pro+ | ~15 min | ~2 min |

---

## Prerequisites

- Google account (free tier works with T4)
- Google Colab Pro recommended if you want A100 or guaranteed runtime
- ~500 MB free space on Google Drive
- You have already run `bash 02_get_data.sh` and `bash 03_generate_synthetic.sh` locally to generate `data/processed/train.jsonl` and `data/processed/valid.jsonl`

---

## Step 1 — Upload Training Data to Google Drive

On your Mac, zip the processed data folder:

```bash
cd "/Users/aditya/Documents/Project Icebreaker/privileged-brain"
zip -r processed_data.zip data/processed/train.jsonl data/processed/valid.jsonl
```

Then upload `processed_data.zip` to Google Drive and extract it so the files are at:

```
My Drive/
└── privileged-brain/
    └── data/
        └── processed/
            ├── train.jsonl   ← required
            └── valid.jsonl   ← required
```

You can create folders directly in the Drive web UI and upload the two `.jsonl` files individually — no need to zip if you prefer.

---

## Step 2 — Upload the Notebook to Colab

**Option A — Upload directly:**
1. Go to [colab.research.google.com](https://colab.research.google.com)
2. File → Upload notebook
3. Select `privileged-brain/colab/privileged_brain_finetune.ipynb`

**Option B — Open from Drive:**
1. Copy `privileged_brain_finetune.ipynb` to your Google Drive
2. Double-click it in Drive — it opens in Colab automatically

---

## Step 3 — Set the Runtime to GPU

1. In Colab: **Runtime → Change runtime type**
2. Set **Hardware accelerator** to **GPU**
3. GPU type:
   - Free tier → T4 (automatically assigned)
   - Pro → choose **A100** for fastest results
4. Click **Save**

> If you get "GPU not available", try again later — free T4s are sometimes fully allocated.

---

## Step 4 — Edit the Path Config Cell (Cell 4)

Open Cell 4 in the notebook. If you placed files at the paths in Step 1, no changes are needed:

```python
DRIVE_DATA_DIR   = "/content/drive/MyDrive/privileged-brain/data/processed"
DRIVE_OUTPUT_DIR = "/content/drive/MyDrive/privileged-brain/training"
```

If you used different folder names on Drive, update these two variables.

---

## Step 5 — Run All Cells in Order

Click **Runtime → Run all**, or run cells one by one with Shift+Enter.

**What to expect at each stage:**

| Cell | What happens | Time |
|---|---|---|
| 1 — Install | pip installs, no output | ~2 min |
| 2 — Mount Drive | Google auth popup | <1 min |
| 3 — GPU detect | Prints GPU name + auto-selected flags | instant |
| 4 — Paths | Prints data/output paths | instant |
| 5 — Verify data | Prints example counts or error | instant |
| 6 — Load model | Downloads Qwen 2.5 Coder 1.5B (~3.1 GB) | ~3 min |
| 7 — LoRA | Prints trainable parameter count | instant |
| 8 — Load dataset | Maps chat template over examples | ~30 sec |
| 9 — SFT train | Progress bar; loss ~2.0 → ~0.4 over 3 epochs | 15–90 min |
| 10 — Save SFT | Lists saved adapter files | instant |
| 11–13 — DPO | Optional; skip if not needed | 2–10 min |

**Healthy loss curve:**
- Step 50: ~1.8–2.2
- End of epoch 1: ~0.7–0.9
- End of epoch 3: ~0.35–0.5

If loss is stuck above 1.5 after 500 steps, the GPU may be running in CPU fallback — check that the GPU runtime is active.

---

## Step 6 — Handling Disconnects

The notebook saves checkpoints to Drive every 200 steps. If Colab disconnects:

1. Reconnect and re-run cells 1–8 (cell 5 verify data can be skipped — data is on Drive)
2. Cell 9 (SFT training) automatically detects the latest checkpoint and resumes from it
3. You will not lose progress

> **Tip:** Keep the Colab tab in the foreground. Colab disconnects idle sessions after ~90 min of inactivity. If you need to step away, run a lightweight keep-alive by adding a cell with `import time; time.sleep(3600)` in a separate thread — or just use Colab Pro which has longer idle timeouts.

---

## Step 7 — Download the Adapter

After training completes, your adapter is already on Drive at:

```
My Drive/privileged-brain/training/adapters/
├── sft/
│   └── final/          ← SFT adapter (use this if you skipped DPO)
│       ├── adapter_config.json
│       ├── adapter_model.safetensors
│       └── tokenizer files
└── dpo/
    └── final/          ← DPO adapter (use this if you ran DPO)
```

Download the `adapters/` folder from Drive to your Mac. Right-click the folder in Drive → Download. It downloads as a zip.

Extract it and place the contents so the structure is:

```
privileged-brain/training/adapters/
├── sft/final/
└── dpo/final/
```

---

## Step 8 — Continue Locally on Your Mac

```bash
cd "/Users/aditya/Documents/Project Icebreaker/privileged-brain"

# Fuse LoRA → HF model → GGUF → quantize → import into Ollama
bash 05_convert_and_import.sh

# Test the model
ollama run privileged-brain "list all listening TCP ports"

# Start llama.cpp server with speculative decoding (optional, for benchmarking)
bash 06_start_inference.sh

# Evaluate: compare baseline vs fine-tuned FEH score
bash 07_evaluate.sh
```

`05_convert_and_import.sh` uses the DPO adapter by default. If you only ran SFT, edit the script to point to `training/adapters/sft/final/` instead.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `CUDA out of memory` | Reduce `BATCH_SIZE` in Cell 4 by half; re-run from Cell 9 |
| `bfloat16 not supported` | Change `USE_BF16=False, USE_FP16=True` in Cell 3 |
| Cell 5 fails — file not found | Check Drive path matches `DRIVE_DATA_DIR` in Cell 4 |
| Loss jumps erratically | Gradient overflow — lower `learning_rate` to `1e-4` in Cell 9 |
| Colab keeps disconnecting | Use Colab Pro or keep the tab active; checkpoints protect you |
| `tokenizer` keyword error | You have an old TRL version — Cell 1 installs latest; restart runtime |
| Drive quota exceeded | Delete old checkpoints from Drive; keep only `final/` folders |
| `processing_class` error | Old TRL version — restart runtime after Cell 1 install |
