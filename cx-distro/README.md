# cx-distro — Icebreaker AI-Native OS ISO Builder

Produces a bootable Ubuntu live ISO with both AI brains, mcpd, the Controller,
model weights, and system configuration pre-installed.

## Prerequisites

- Docker (for reproducible builds)
- GGUF model files in `../models/` with verified `checksums.sha256`

## Quick start

```bash
# Build the Docker build container
docker build -t icebreaker-build -f Dockerfile.build ..

# Build the ISO (runs all 6 stages)
docker run --privileged -v "$(pwd)/..:/build" icebreaker-build
```

## Fast iteration

```bash
# Skip binary builds, rebuild only chroot + ISO (stages 4-5)
docker run --privileged -v "$(pwd)/..:/build" icebreaker-build --skip-to=4

# Build without model files (fast, ~500 MB ISO for testing boot flow)
docker run --privileged -v "$(pwd)/..:/build" icebreaker-build --no-models
```

## Verify build output

```bash
# Static checks (runs on macOS — no build needed)
bash tests/test_build_output.sh --static-only

# Full checks (inside Docker after build)
bash tests/test_build_output.sh
```

## Pin files

| File | Purpose |
|------|---------|
| `LLAMA_CPP_COMMIT` | llama.cpp git commit hash (rebuild llama-server on change) |
| `UBUNTU_BASE` | Ubuntu codename for live-build (e.g. `noble` = 24.04 LTS) |

## Build stages

| Stage | What | Skip with |
|-------|------|-----------|
| 0 | Preflight (checksums, repo state) | `--skip-to=1` |
| 1 | Build mcpd (Rust) | `--skip-to=2` |
| 2 | Build llama-server (C++) | `--skip-to=3` |
| 3 | Build Python venv | `--skip-to=4` |
| 4 | Assemble chroot tree | `--skip-to=5` |
| 5 | Build ISO (live-build) | — |
