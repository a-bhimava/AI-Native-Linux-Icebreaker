# Icebreaker ISO Build Requirements

**Purpose.** Every fact required for a successful ISO build, in ONE file, so we don't rediscover them by hitting runtime failures. Cross-referenced by `cx-distro/preflight.sh` (which asserts every line here at build time) and by `cx-distro/rebuild/rebuild-v67.sh` (which drives the build once preflight is green).

**Owner.** Every ISO build MUST pass `preflight.sh` before invoking any part of the build pipeline. If a build fails on something not documented here, add it here first, then update preflight, then re-run.

---

## 1. Host / VM preconditions

| Requirement | Value | Verify command |
|---|---|---|
| GCP project ID | `project-ef281c18-2a28-4139-a89` (team.projectsyard@gmail.com) | `gcloud config get-value project` |
| GCP zone | `us-central1-a` | (arg to every gcloud call) |
| VM name | `icebreaker-build-vm` | `gcloud compute instances describe <VM> --format='value(status)'` returns `RUNNING` |
| VM min free disk | ≥ 20 GB on `$HOME` filesystem | `ssh <VM> df -BG $HOME \| awk 'NR==2 {print $4}'` ≥ 20 |
| Docker on VM | v20.10+ (legacy builder OK) | `ssh <VM> docker --version` — supports `--no-cache` flag but NOT `--progress=plain` on 29.1.3 |
| tmux on VM | any version | `ssh <VM> which tmux` |
| operator has `gcloud`, `git`, `tar`, `zstd` | | `which gcloud git tar zstd` on Mac |

## 2. Icebreaker source tree state

| Requirement | Value |
|---|---|
| Tree layout on VM | `$HOME/Icebreaker` — git clone (branch pull on each build) |
| Tree preparation | `ssh <VM> 'cd Icebreaker && git fetch origin && git checkout <branch> && git pull --ff-only'` |
| Model file location | `$HOME/models/run7_cot_q4km.gguf` on the VM (940 MB, kept out of git) |
| Model checksum | Must match `models/checksums.sha256` in the tree |
| Model symlink | `models/run7_cot_q4km.gguf → $HOME/models/run7_cot_q4km.gguf` (created after clone) |
| `cx-distro/.build/` | Owned by root from prior sudo builds; `sudo rm -rf` to clean if needed |

## 3. Docker image apt packages (`cx-distro/Dockerfile.build`)

All packages MUST appear in the `apt-get install` block. Adding a package after the fact requires `docker build --no-cache` because Docker won't invalidate the RUN layer just because the arg list changed.

**Required packages (verified 2026-07-13 after 4 build attempts):**

```
# ISO build tooling
debootstrap squashfs-tools xorriso isolinux syslinux-common grub-efi-amd64-bin
mtools dosfstools

# C/C++ compilation for mcpd + llama.cpp
build-essential cmake git ca-certificates curl pkg-config

# Rust + cross toolchain
gcc-aarch64-linux-gnu       # required for arm64 mcpd link (fixed 2026-07-13, was rust-lld linker error)
g++-aarch64-linux-gnu       # required for arm64 llama.cpp cross-compile

# Python for controller venv
python3 python3-venv python3-pip
python3-dev                 # required for pycairo build (pkg-config finds python.pc) — fixed 2026-07-13

# GTK / GLib for dual-brain[gui] deps
libseccomp-dev
libgirepository1.0-dev      # legacy libgirepository — kept for compat
libgirepository-2.0-dev     # required for PyGObject 3.56.3 on Ubuntu 24.04 — fixed 2026-07-13
libcairo2-dev
```

**Docker ENV vars required:**

```
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
```
Without this, `cargo build --release --target=aarch64-unknown-linux-gnu` fails at link stage with `rust-lld: error: too many errors emitted`. Fixed 2026-07-13.

## 4. Rust toolchain (inside Docker image)

Both targets MUST be pre-installed via `rustup target add`:
- `x86_64-unknown-linux-gnu`
- `aarch64-unknown-linux-gnu`

## 5. cmake / llama.cpp build flags

`build.sh` Stage 2 MUST loop over `{amd64, arm64}` and produce per-arch binaries at:
- `cx-distro/.build/llama.cpp/build-portable/bin/llama-server` (amd64)
- `cx-distro/.build/llama.cpp/build-arm64/bin/llama-server` (arm64)

These paths are hardcoded in `v6.manifest:37` — do NOT change without updating the manifest.

Required cmake flags:

```
-DCMAKE_BUILD_TYPE=Release
-DLLAMA_BUILD_TESTS=OFF
-DLLAMA_BUILD_EXAMPLES=OFF
-DLLAMA_BUILD_SERVER=ON
-DBUILD_SHARED_LIBS=OFF     # F-21 rule: default is thin binary + .so; static is what ships
```

Plus per-arch flags from `config/archs/{arch}.conf` (`LLAMA_CMAKE_FLAGS` var).

For arm64 cross-compile ADD:

```
-DCMAKE_SYSTEM_NAME=Linux
-DCMAKE_SYSTEM_PROCESSOR=aarch64
-DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc
-DCMAKE_CXX_COMPILER=aarch64-linux-gnu-g++
```

Verification post-build:
- `file(1)` on each binary — must match target arch (`x86-64` for amd64, `ARM aarch64` for arm64).
- `stat -c %s` on each binary must return > 1,000,000 (1 MB minimum — a smaller binary means BUILD_SHARED_LIBS wasn't applied and F-21 will bite at runtime).

## 6. Label naming convention

`build-iso.sh:155` regex covers ARCH-suffixed labels. Current:

```
^v6\.[6-9]|^v[7-9]
```

Extend the regex when new labels arrive. **Any label matching the regex OR any non-amd64 arch gets `${LABEL}-${ARCH}.iso` naming.** Anything else gets unsuffixed `${LABEL}.iso` (V0-V6.51 legacy — do not use for new builds).

Root `Makefile:52` `qemu-%` target uses `incremental/.build/out/${LABEL}-${arch}.iso` (**NO** `icebreaker-` prefix — that convention was retired 2026-07-09).

## 7. mcpd seccomp allowlist minima

Every syscall the broadened `mcpd-harvest.sh` exercises MUST be in the allowlist. Current known-required set added by Scope G + I:

**Path permission checks (F-55 + F-58, both arches):**
- `SYS_faccessat` — arm64 canonical (arm64 has no `access(2)`)
- `SYS_faccessat2` — modern glibc's first-choice on Linux 5.8+
- `SYS_access` under `#[cfg(target_arch = "x86_64")]` — F-58, legacy syscall #21 that dpkg-query etc. still call on amd64

**File I/O, memory, threading, sockets, exec** — see `src/mcpd/src/sandbox/seccomp.rs::allowed_syscalls()` (330+ lines). Do NOT modify without running `mcpd-harvest.sh` before AND after.

## 8. F-51 marker set (`v2.manifest`)

Each marker is `"F-xx:filename:substring"`. `filename` is resolved relative to `${chroot}${sp}/` where `sp` is the venv site-packages path.

**Known incorrect markers (discovered 2026-07-13 during v6.7 build):**
- `F-53-shot:bridge.py:_last_screenshot_error` — resolves to `controller/bridge.py` (wrong). Should be `rpa_bridge/bridge.py` since that's where the file lives per `dual-brain/` tree. **MUST FIX** in `v2.manifest` before next build.

Any new marker added MUST be verified with `[ -f "${chroot}${sp}/${filename}" ]` before the marker check loop runs.

## 9. Model file (INV-7)

The 940 MB `run7_cot_q4km.gguf` model:
- Lives at `/home/aditya/models/run7_cot_q4km.gguf` on the VM (NOT in the repo tree).
- Symlinked into `Icebreaker/models/run7_cot_q4km.gguf` after tarball extract.
- SHA-256 must match `models/checksums.sha256`.
- Embedded into ISO by `v6.manifest:60` (not by `build.sh` Stage 4 — that path only fires under `--no-models=0` for standalone Docker ISOs).
- If running `build.sh --no-models`, standalone Docker ISO ships without the model but `make iso-*` (which uses `v6.manifest`) still embeds it. This is the correct workflow.

## 10. Build path decision

- **DO** use: `sudo bash cx-distro/rebuild/rebuild-v67.sh` — one canonical entry point.
- **DO NOT** use: `cx-distro/rebuild/rebuild.sh` (legacy amd64-only, calls `build_v2.sh`, misses Scope G refactor).
- **DO NOT** use: `docker run ... --skip-to=N` for anything other than 0. SKIP_TO is a lower bound, not a range — `--skip-to=1` runs Stages 1-5, not "just Stage 1".

## 11. Free disk floor at each phase

- Before Stage 5 (debootstrap + squashfs + xorriso): ≥ 15 GB free per arch.
- Before `make iso-arm64` (qemu-user-static debootstrap): ≥ 25 GB free (arm64 chroot is bigger due to per-package emulation overhead).

Total for dual-arch build: **≥ 40 GB free** on the VM `/home/aditya` filesystem.

## 12. Container cleanup

Every `docker run --rm` should self-clean, but stale containers from crashed runs accumulate. Before starting a big build:

```
sudo docker container prune -f
sudo docker image prune -f  # keeps active :v67-* images, removes dangling
```

Do NOT prune volumes or the entire system — that removes the base `ubuntu:noble` layer + toolchain caches, costing 5+ min on next build.

---

## Changelog

- **2026-07-13** — Initial version. Consolidates lessons from 17 distinct failures across 5 build attempts on 2026-07-12/13.
