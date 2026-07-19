#!/usr/bin/env bash
# cx-distro/preflight.sh — Icebreaker ISO build preflight
#
# Runs BEFORE `docker build` / `make iso-*`. Verifies every requirement
# in cx-distro/BUILD_REQUIREMENTS.md. Exits 0 if all green; 1 if any red.
#
# The rule: if this script is green, the build is highly likely to succeed
# on the first try. If it's red, fix the red item (or update this script
# + BUILD_REQUIREMENTS.md together) BEFORE running the build.
#
# Modes:
#   ./preflight.sh              — run all checks, print red/green per line
#   ./preflight.sh --quiet      — only print reds; exit code carries the answer
#   ./preflight.sh --host       — skip VM-side checks (for local-only preflight)
#
# Exit codes:
#   0 — all checks green
#   1 — one or more checks red (build MUST NOT proceed)

set -uo pipefail

# ── argparse ─────────────────────────────────────────────────────────────
QUIET=0
SKIP_VM=0
for arg in "$@"; do
    case "$arg" in
        --quiet) QUIET=1 ;;
        --host)  SKIP_VM=1 ;;
        --help|-h)
            grep '^#' "$0" | head -25
            exit 0
            ;;
    esac
done

# ── colors + accounting ──────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; RST='\033[0m'
FAILS=0
CHECKS=0
FAIL_LIST=()

pass() {
    CHECKS=$((CHECKS + 1))
    [ "$QUIET" -eq 0 ] && printf "${GRN}[ OK ]${RST} %s\n" "$1"
}
fail() {
    CHECKS=$((CHECKS + 1))
    FAILS=$((FAILS + 1))
    FAIL_LIST+=("$1")
    printf "${RED}[FAIL]${RST} %s\n" "$1" >&2
    [ -n "${2:-}" ] && printf "        ${YEL}fix:${RST} %s\n" "$2" >&2
}

# ── env ──────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/cx-distro/Dockerfile.build"
BUILD_SH="${REPO_ROOT}/cx-distro/build.sh"
BUILD_ISO_SH="${REPO_ROOT}/incremental/build/build-iso.sh"
MAKEFILE="${REPO_ROOT}/Makefile"
SECCOMP_RS="${REPO_ROOT}/src/mcpd/src/sandbox/seccomp.rs"
V2_MANIFEST="${REPO_ROOT}/incremental/versions/v2.manifest"
V6_MANIFEST="${REPO_ROOT}/incremental/versions/v6.manifest"
CHECKSUMS="${REPO_ROOT}/models/checksums.sha256"

VM_NAME="${VM_NAME:-icebreaker-phase2-vm}"
VM_ZONE="${VM_ZONE:-us-west4-b}"
VM_PROJECT="${VM_PROJECT:-project-12486d7e-4046-45bd-8b4}"

echo "═══ Icebreaker ISO build preflight ═══"
echo ""

# ── 1. Operator machine checks ──────────────────────────────────────────
echo "── 1. Operator machine ──"
for tool in gcloud git tar zstd; do
    if command -v "$tool" >/dev/null 2>&1; then
        pass "$tool available"
    else
        fail "$tool not found on operator machine" "brew install $tool  (or apt install $tool on Linux)"
    fi
done

# ── 2. Source tree state ────────────────────────────────────────────────
echo ""
echo "── 2. Source tree ──"

if [ -f "$DOCKERFILE" ]; then
    pass "cx-distro/Dockerfile.build present"
else
    fail "cx-distro/Dockerfile.build missing" "check working directory"
    exit 1
fi

# Required apt packages in Dockerfile
_pkgs=(
    debootstrap squashfs-tools xorriso isolinux syslinux-common grub-efi-amd64-bin
    mtools dosfstools
    build-essential cmake git ca-certificates curl pkg-config
    gcc-aarch64-linux-gnu 'g++-aarch64-linux-gnu'
    python3 python3-venv python3-pip python3-dev
    libseccomp-dev libgirepository1.0-dev libgirepository-2.0-dev libcairo2-dev
)
for pkg in "${_pkgs[@]}"; do
    # grep -F: fixed-string search — no regex interpretation of `++`
    if grep -qF "$pkg" "$DOCKERFILE"; then
        pass "Dockerfile has $pkg"
    else
        fail "Dockerfile missing $pkg" "add to apt-get install block in Dockerfile.build"
    fi
done

# Required env vars in Dockerfile
if grep -q "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER" "$DOCKERFILE"; then
    pass "Dockerfile exports CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER"
else
    fail "Dockerfile missing CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER env" \
         "add ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc"
fi

# Rust cross target
if grep -q "aarch64-unknown-linux-gnu" "$DOCKERFILE"; then
    pass "Dockerfile installs aarch64-unknown-linux-gnu rustup target"
else
    fail "Dockerfile does not add aarch64-unknown-linux-gnu rustup target" \
         "add to 'rustup target add' line"
fi

# ── 3. build.sh Stage 1 + Stage 2 shape ─────────────────────────────────
echo ""
echo "── 3. build.sh ──"

if grep -q 'for target_arch in amd64 arm64' "$BUILD_SH"; then
    pass "build.sh has per-arch loop"
else
    fail "build.sh missing per-arch loop" "Scope G refactor: for target_arch in amd64 arm64"
fi

if grep -q 'BUILD_SHARED_LIBS=OFF' "$BUILD_SH"; then
    pass "build.sh has BUILD_SHARED_LIBS=OFF (F-21)"
else
    fail "build.sh missing BUILD_SHARED_LIBS=OFF" \
         "F-21: llama-server MUST be static; add to LLAMA_CMAKE_FLAGS_COMMON"
fi

if grep -q 'CMAKE_C_COMPILER=aarch64-linux-gnu-gcc' "$BUILD_SH"; then
    pass "build.sh has arm64 llama-server cross-compile flags"
else
    fail "build.sh missing arm64 CMAKE_C_COMPILER=aarch64-linux-gnu-gcc" \
         "add to LLAMA_CMAKE_FLAGS_ARCH for arm64"
fi

# ── 4. Label naming (build-iso.sh + Makefile) ───────────────────────────
echo ""
echo "── 4. Label naming ──"

if grep -qE '\^v6\\\.\[6-9\]' "$BUILD_ISO_SH"; then
    pass "build-iso.sh regex covers v6.6-v6.9"
else
    fail "build-iso.sh label regex may not cover v6.7+ non-amd64 naming" \
         "check line ~155 for regex: ^v6\\.[6-9]|^v[7-9]"
fi

if grep -qE 'icebreaker-\$\(LABEL\)' "$MAKEFILE"; then
    fail "Makefile qemu-% target still has obsolete 'icebreaker-' prefix" \
         "remove prefix from Makefile:52 — filename is \${LABEL}-\${arch}.iso"
else
    pass "Makefile qemu-% target has correct filename pattern"
fi

# ── 5. seccomp allowlist (F-55 + F-58) ──────────────────────────────────
echo ""
echo "── 5. mcpd seccomp allowlist ──"

for sym in SYS_faccessat SYS_faccessat2 SYS_access; do
    if grep -q "libc::${sym}" "$SECCOMP_RS"; then
        pass "seccomp.rs has libc::${sym}"
    else
        fail "seccomp.rs missing libc::${sym}" \
             "add to allowed_syscalls() — see F-55 (faccessat*) / F-58 (access)"
    fi
done

# ── 6. v2.manifest F-51 markers (path-correctness) ──────────────────────
echo ""
echo "── 6. F-51 markers (v2.manifest) ──"

# F-53-shot marker path bug — discovered 2026-07-13
# _target="${chroot}${sp}/${_f}" where sp is the controller/ site-packages
# path. So a marker naming a file OUTSIDE controller/ needs `../` prefix.
# rpa_bridge/bridge.py is a sibling package to controller/, so the marker
# path must be `../rpa_bridge/bridge.py`.
if grep -qF '"F-53-shot:../rpa_bridge/bridge.py:_last_screenshot_error"' "$V2_MANIFEST"; then
    pass "F-53-shot marker path correct (../rpa_bridge/bridge.py)"
elif grep -qF '"F-53-shot:rpa_bridge/bridge.py:_last_screenshot_error"' "$V2_MANIFEST"; then
    fail "v2.manifest F-53-shot marker resolves to controller/rpa_bridge/bridge.py (does not exist)" \
         "prefix path with ../ so it resolves to site-packages/rpa_bridge/bridge.py"
elif grep -qF '"F-53-shot:bridge.py:_last_screenshot_error"' "$V2_MANIFEST"; then
    fail "v2.manifest F-53-shot marker resolves to controller/bridge.py (does not exist)" \
         "change to '../rpa_bridge/bridge.py'"
elif grep -q 'F-53-shot' "$V2_MANIFEST"; then
    pass "F-53-shot marker present (unknown format — manual review)"
else
    pass "F-53-shot marker not present (may be removed intentionally)"
fi

# ── 7. Model file (INV-7) ───────────────────────────────────────────────
echo ""
echo "── 7. Model checksums ──"

if [ -f "$CHECKSUMS" ]; then
    pass "models/checksums.sha256 exists"
    if grep -q "run7_cot_q4km.gguf" "$CHECKSUMS"; then
        pass "run7_cot_q4km.gguf listed in checksums"
    else
        fail "run7_cot_q4km.gguf missing from checksums" \
             "regenerate checksums.sha256 from build.sh Stage 1 auto-refresh"
    fi
else
    fail "models/checksums.sha256 missing" "check working directory"
fi

# ── 8. VM-side checks ────────────────────────────────────────────────────
# If we're already ON the VM (hostname matches), local checks are all we need;
# the "is VM reachable" story doesn't apply.
if [ "$(hostname 2>/dev/null)" = "$VM_NAME" ]; then
    echo ""
    echo "── 8. Running on VM directly — local disk/tools checks ──"
    # Disk — probe $HOME (portable across build VMs / user accounts)
    free_gb="$(df -BG "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d G)"
    if [ -n "$free_gb" ] && [ "$free_gb" -ge 40 ]; then
        pass "VM has ${free_gb}G free (>= 40G required for dual-arch)"
    elif [ -n "$free_gb" ] && [ "$free_gb" -ge 20 ]; then
        pass "VM has ${free_gb}G free (>= 20G — amd64-only OK)"
    else
        fail "VM has only ${free_gb:-?}G free" \
             "clean up: sudo rm -rf ${HOME}/*.bak-* ${HOME}/icebreaker"
    fi
    # Docker + tmux — also probe /usr/bin directly so a stripped PATH
    # (e.g. under tmux non-login shell) doesn't produce a false negative.
    if command -v docker >/dev/null 2>&1 || [ -x /usr/bin/docker ]; then
        pass "Docker installed"
    else
        fail "Docker missing" "apt install docker.io"
    fi
    if command -v tmux >/dev/null 2>&1 || [ -x /usr/bin/tmux ]; then
        pass "tmux installed"
    else
        fail "tmux missing" "apt install tmux"
    fi
    # Model — accept $HOME/models/ OR the in-tree ${REPO_ROOT}/models/
    # so the build can be run from any user account.
    if [ -f "$HOME/models/run7_cot_q4km.gguf" ]; then
        pass "model at $HOME/models/run7_cot_q4km.gguf"
    elif [ -f "${REPO_ROOT}/models/run7_cot_q4km.gguf" ]; then
        pass "model at ${REPO_ROOT}/models/run7_cot_q4km.gguf"
    else
        fail "model missing on VM" "copy run7_cot_q4km.gguf into $HOME/models/ or ${REPO_ROOT}/models/"
    fi
elif [ "$SKIP_VM" -eq 0 ]; then
    echo ""
    echo "── 8. VM ($VM_NAME @ $VM_ZONE) — checked from operator machine ──"

    # gcloud project sanity
    current_project="$(gcloud config get-value project 2>/dev/null || echo '')"
    if [ "$current_project" = "$VM_PROJECT" ]; then
        pass "gcloud project = $VM_PROJECT"
    else
        fail "gcloud project = '$current_project', expected '$VM_PROJECT'" \
             "gcloud config set project $VM_PROJECT"
    fi

    # VM state
    vm_status="$(gcloud compute instances describe "$VM_NAME" \
        --zone="$VM_ZONE" \
        --format='value(status)' 2>/dev/null || echo 'UNREACHABLE')"
    if [ "$vm_status" = "RUNNING" ]; then
        pass "VM $VM_NAME is RUNNING"
    else
        fail "VM $VM_NAME status = '$vm_status'" \
             "gcloud compute instances start $VM_NAME --zone=$VM_ZONE"
    fi

    if [ "$vm_status" = "RUNNING" ]; then
        # Free disk on VM
        vm_free_gb="$(gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" \
            --command='df -BG /home/aditya | awk "NR==2 {print \$4}" | tr -d G' \
            2>/dev/null || echo '0')"
        if [ "$vm_free_gb" -ge 40 ]; then
            pass "VM has ${vm_free_gb}G free (>= 40G required for dual-arch)"
        elif [ "$vm_free_gb" -ge 20 ]; then
            pass "VM has ${vm_free_gb}G free (>= 20G — enough for amd64-only)"
        else
            fail "VM has only ${vm_free_gb}G free" \
                 "clean up: sudo rm -rf /home/aditya/*.bak-* /home/aditya/icebreaker (older duplicate trees)"
        fi

        # Docker present
        vm_docker="$(gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" \
            --command='docker --version 2>/dev/null | head -1' 2>/dev/null || echo '')"
        if [ -n "$vm_docker" ]; then
            pass "VM has Docker: $vm_docker"
        else
            fail "Docker not installed on VM"
        fi

        # tmux present
        vm_tmux="$(gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" \
            --command='which tmux 2>/dev/null' 2>/dev/null || echo '')"
        if [ -n "$vm_tmux" ]; then
            pass "VM has tmux"
        else
            fail "tmux not on VM" "gcloud compute ssh $VM_NAME --zone=$VM_ZONE --command='sudo apt install -y tmux'"
        fi

        # Model file on VM
        vm_model="$(gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" \
            --command='ls -la /home/aditya/models/run7_cot_q4km.gguf 2>/dev/null' 2>/dev/null || echo '')"
        if [ -n "$vm_model" ]; then
            pass "VM has model at /home/aditya/models/run7_cot_q4km.gguf"
        else
            fail "model file missing on VM" \
                 "manually copy run7_cot_q4km.gguf to /home/aditya/models/ (940 MB, verify hash matches checksums.sha256)"
        fi
    fi
else
    echo ""
    echo "── 8. VM checks SKIPPED (--host flag) ──"
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "═══ Summary: ${CHECKS} checks, ${FAILS} failed ═══"

if [ "$FAILS" -eq 0 ]; then
    printf "${GRN}ALL PREFLIGHT CHECKS GREEN — safe to run cx-distro/rebuild/rebuild-v67.sh${RST}\n"
    exit 0
else
    printf "${RED}${FAILS} PREFLIGHT CHECK(S) FAILED — DO NOT run the build${RST}\n"
    echo ""
    echo "Failed checks:"
    for f in "${FAIL_LIST[@]}"; do
        echo "  - $f"
    done
    echo ""
    echo "Fix each failure, then re-run: bash cx-distro/preflight.sh"
    exit 1
fi
