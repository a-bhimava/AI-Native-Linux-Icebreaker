# VM Backup — instance-20260528-030421

Full backup of the Phase 2 / Run 7 dev VM, taken **2026-06-09** immediately before
shutting the VM down. Everything irreplaceable is here; everything rebuildable is
documented in `manifest/` so it can be recreated on a new VM.

**Source VM:** `instance-20260528-030421` · `us-central1-a` · project `fiery-artifact-344713`
**Machine:** g2-standard-8, 1× NVIDIA L4 (23GB), 100GB NVMe, Debian 12 (bookworm)
(full config: `manifest/gcloud-instance-describe.yaml`)

## Contents

| File | Size | What it is | Restores to |
|---|---|---|---|
| `models/run7_cot_q4km.gguf` | 940M | **The PB model** — Run 7 CoT SFT, Q4_K_M quant | `~/models/` |
| `models/run7_cot_fp16.gguf` | 2.9G | Run 7 fp16 GGUF (re-quantize source) | `~/models/` |
| `models/qwen2.5-1.5b-instruct-q4_k_m.gguf` | 940M | QB model (also re-downloadable from HF) | `~/models/` |
| `training.tar.gz` | 1.2G | All LoRA adapters (sft, run7_cot, run7_cot_bak, dpo, run7_cot_dpo, run7_cot_dpo_v2) + training logs | `~/training/` |
| `conversion.tar` | 2.9G | `run7_cot_fused/` — fused HF model dir (source for GGUF conversion) | `~/conversion/` |
| `home-files.tar.gz` | 3.0M | `scripts/` (training/eval py), `data/` (processed + synthetic), `eval/`, `dual-brain/`, dotfiles (.bashrc/.profile/.config), all `*.log`, `*.sh`, requirements.txt, historical milestone tarballs | `~/` |
| `src-mcpd.tar.gz` | 67K | `~/src/mcpd` Rust source (target/ excluded — rebuild with cargo) | `~/src/` |
| `icebreaker.tar.gz` | 116K | `~/icebreaker` tree, source only (target/ excluded; was not a git repo) | `~/` |
| `manifest/` | — | System-state snapshots (see below) | reference only |
| `checksums.sha256` | — | SHA-256 of every artifact, computed on the VM | — |

## The final fine-tuned Privileged Brain model

**The production PB model is `models/run7_cot_q4km.gguf` (940M) in this folder.**
It is the Run 7 chain-of-thought SFT fine-tune (Qwen2 1.5B architecture, LoRA
fused, quantized to Q4_K_M) — the model the dual-brain Controller dispatches to.

```
SHA-256: 4c3c4628f193e32c6e7f0fed61ff6007fcc8f3d4ff518c60b214cf28c7fcad7c
```

Three forms of the same model live in this backup, from most to least derived:

| Form | File | Use |
|---|---|---|
| Q4_K_M GGUF | `models/run7_cot_q4km.gguf` | **Deploy this.** Runs in llama-server. |
| fp16 GGUF | `models/run7_cot_fp16.gguf` | Re-quantize to other formats (`llama-quantize`) |
| Fused HF dir | `conversion.tar` → `run7_cot_fused/` | Further training, HF-ecosystem use, GGUF re-conversion |
| LoRA adapters | `training.tar.gz` → `adapters/run7_cot/` | Re-fuse onto the base model from scratch |

### How to store it

- Keep this folder intact — verify with `shasum -a 256 -c checksums.sha256` after
  any copy or move. GGUF files are already compressed; don't gzip them.
- Per INV-7, before the model is used by mcpd it must be registered in
  `models/checksums.sha256` at the repo root:
  `shasum -a 256 run7_cot_q4km.gguf >> <repo>/models/checksums.sha256`
- Recommended: keep a second copy off this laptop (external drive or a GCS
  bucket: `gsutil cp models/run7_cot_q4km.gguf gs://YOUR_BUCKET/models/`).
  The Q4 file + the adapters are the minimum irreplaceable set.

### How to use it

On any machine with llama.cpp (build recipe in `manifest/llamacpp-build.txt`):

```bash
# Serve as the PB on port 8082 (QB uses 8081):
llama-server --model models/run7_cot_q4km.gguf \
  --port 8082 --n-gpu-layers 99 --ctx-size 2048
```

On the new VM, `~/dual-brain/scripts/start_pb.sh` wraps exactly this. The
Controller's `controller.toml` points the PB backend at the server URL.

To evaluate it after restore: `scripts/eval_feh_cot.py` and
`scripts/eval_adversarial_gguf.py` (in `home-files.tar.gz` → `scripts/`) are the
eval harnesses used for Run 7.

## What was deliberately NOT backed up (rebuildable)

| Skipped | Size | How to recreate on new VM |
|---|---|---|
| `~/.cache` | 6.0G | pip/HF caches — regenerate automatically |
| `~/.local/lib` | 5.3G | `pip install --user -r manifest/pip-user-freeze.txt` |
| `~/dual-brain-venv` | 243M | `python3 -m venv ~/dual-brain-venv && pip install -r manifest/pip-dualbrain-venv-freeze.txt` |
| `~/llama.cpp` | 615M | See `manifest/llamacpp-build.txt` — clone, checkout commit `399739d`, `cmake -B build -DGGML_CUDA=ON && cmake --build build --target llama-server llama-quantize -j$(nproc)` |
| `~/.rustup`, `~/.cargo` | 1.4G | `rustup` install (version in `manifest/rust-version.txt`) |
| mcpd `target/` dirs | 1.7G | `cargo build --release` in `~/src/mcpd` |

## Verify integrity

```bash
cd backups/vm-backup
shasum -a 256 -c checksums.sha256   # all lines must say OK
```
(Verified OK at backup time, 2026-06-09.)

## Restore to a new VM

```bash
# 1. Create VM (match manifest/gcloud-instance-describe.yaml):
gcloud compute instances create NEW_VM \
  --zone=us-central1-a --project=fiery-artifact-344713 \
  --machine-type=g2-standard-8 \
  --accelerator=type=nvidia-l4,count=1 \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=100GB --maintenance-policy=TERMINATE

# 2. Upload artifacts:
gcloud compute scp --zone=ZONE training.tar.gz conversion.tar home-files.tar.gz \
  src-mcpd.tar.gz icebreaker.tar.gz NEW_VM:~
gcloud compute scp --zone=ZONE models/*.gguf NEW_VM:~/models/

# 3. On the VM, extract in $HOME:
tar -xzf home-files.tar.gz && tar -xzf training.tar.gz && \
tar -xf conversion.tar && tar -xzf src-mcpd.tar.gz && tar -xzf icebreaker.tar.gz

# 4. Recreate environments (see "NOT backed up" table above):
#    - NVIDIA driver + CUDA (versions: manifest/nvidia-smi.txt, manifest/nvcc-version.txt)
#    - apt packages:  manifest/apt-manual.txt
#    - venv + pip:    manifest/pip-dualbrain-venv-freeze.txt, manifest/pip-user-freeze.txt
#    - llama.cpp:     manifest/llamacpp-build.txt (commit + CUDA build flags)

# 5. Start inference (exact command in manifest/llamacpp-build.txt):
~/llama.cpp/build/bin/llama-server \
  --model ~/models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
  --port 8081 --n-gpu-layers 99 --ctx-size 2048

# 6. Deploy current dual-brain code and run tests:
VM_NAME=NEW_VM bash dual-brain/scripts/deploy_to_vm.sh
```

## Manifest files

- `gcloud-instance-describe.yaml` — machine type, GPU, disk, network
- `nvidia-smi.txt`, `nvcc-version.txt` — driver 550.x / CUDA toolchain versions
- `apt-manual.txt`, `dpkg-all.txt` — installed packages
- `pip-dualbrain-venv-freeze.txt`, `pip-user-freeze.txt` — Python deps
- `llamacpp-build.txt` — llama.cpp commit, CUDA cmake flags, live server command line
- `rust-version.txt`, `python-version.txt`, `uname.txt`, `os-release.txt`
- `home-listing.txt`, `home-du.txt` — full `ls -laR` / `du` of home at backup time
- `ps-aux.txt`, `systemd-running.txt`, `crontab.txt` — runtime state at backup time
