#!/usr/bin/env bash
# cx-distro/rebuild/rebuild-v67.sh — canonical v6.7 (and future) ISO builder
#
# ONE script, top-to-bottom, no partial invocations. If any step fails,
# fix the requirement in cx-distro/BUILD_REQUIREMENTS.md + preflight.sh
# and re-run this from the top.
#
# Usage (on the GCP VM inside tmux):
#   tmux new -d -s v67 'bash cx-distro/rebuild/rebuild-v67.sh'
#   tmux attach -t v67
#
# Env vars:
#   LABEL              — ISO version label (default: v6.7)
#   VN                 — version number (default: 6)
#   V67_INCLUDE_ARM64  — 0 = amd64 only; 1 = also build arm64 (default: 1)
#   V67_LOG            — log path (default: /home/aditya/v67-build.log)
#
# Exit codes:
#   0 — all ISOs produced
#   1 — preflight failed OR a build step failed

set -Eeuo pipefail

# ── config ───────────────────────────────────────────────────────────────
REPO="${REPO:-$HOME/Icebreaker}"
LABEL="${LABEL:-v6.7}"
VN="${VN:-6}"
V67_INCLUDE_ARM64="${V67_INCLUDE_ARM64:-1}"
V67_LOG="${V67_LOG:-${HOME}/v67-build.log}"
# v6.13_OC Fix L' Commit 1: EDITION plumbing.
#   current — Textual TUI is the AI Terminal (v6.12 shape, default).
#   oc      — opencode's TUI is the AI Terminal; mcpd exposed via MCP.
#   both    — build both editions sequentially, 4 ISOs total.
V67_EDITION="${V67_EDITION:-current}"
case "$V67_EDITION" in
    current|oc|both) ;;
    *) echo "FATAL: V67_EDITION must be current|oc|both, got: $V67_EDITION" >&2; exit 1 ;;
esac
DOCKER_TAG="icebreaker-build:${LABEL}"

# fail-fast trap
trap 'echo "FATAL: line ${LINENO} exited non-zero (last cmd: ${BASH_COMMAND:-?})" | tee -a "$V67_LOG" >&2; exit 1' ERR
exec > >(tee -a "$V67_LOG") 2>&1

echo "══════════════════════════════════════════════════════════════════"
echo "═══ Icebreaker ${LABEL} canonical build @ $(date -u) ═══"
echo "═══ arm64 included: ${V67_INCLUDE_ARM64}                         ═══"
echo "═══ edition:        ${V67_EDITION}                                    ═══"
echo "══════════════════════════════════════════════════════════════════"

cd "$REPO"

# ── Step 0: preflight ────────────────────────────────────────────────────
echo ""
echo "▶ Step 0: preflight"
# --host: skip the operator-side VM check because rebuild-v67.sh runs
# ON the build VM itself (the VM checking whether it can reach itself
# via gcloud is chicken-and-egg AND requires gcloud creds inside the VM).
bash "$REPO/cx-distro/preflight.sh" --quiet --host || {
    echo "FATAL: preflight failed. Fix the failed checks above, then re-run."
    exit 1
}
echo "✓ Step 0: preflight green"

# ── Step 1: Docker image (--no-cache, always) ────────────────────────────
echo ""
echo "▶ Step 1: Docker image build (--no-cache) $(date -u)"
# --no-cache is CHEAP relative to what happens when a cache-hit hides a
# Dockerfile change. Previous builds lost ~30 min discovering this.
sudo docker build --no-cache -f cx-distro/Dockerfile.build -t "$DOCKER_TAG" .
echo "✓ Step 1: Docker image ${DOCKER_TAG} built"

# ── Step 2: build.sh Stages 0-5 inside Docker ────────────────────────────
echo ""
echo "▶ Step 2: mcpd + llama-server + venv + chroot + standalone ISO $(date -u)"
# --no-models: skip Stage 0 model checksum verify (which requires the
# 940 MB model to be mounted into the container). The model gets embedded
# LATER by v6.manifest:60 which runs on the host and reads directly from
# /home/aditya/models/. Stage 4 embeds under --no-models=0 only for the
# standalone Docker ISO (which we don't ship — only make iso-* output ships).
sudo docker run --rm --privileged -v "$REPO":/build "$DOCKER_TAG" --force --skip-to=0 --no-models
echo "✓ Step 2: Docker-side pipeline complete"

# ── Step 3: verify all binaries produced ─────────────────────────────────
echo ""
echo "▶ Step 3: verify binaries $(date -u)"

_required_bins=(
    "cx-distro/.build/mcpd-amd64"
    "cx-distro/.build/llama.cpp/build-portable/bin/llama-server"
)
[ "$V67_INCLUDE_ARM64" = "1" ] && _required_bins+=(
    "cx-distro/.build/mcpd-arm64"
    "cx-distro/.build/llama.cpp/build-arm64/bin/llama-server"
)

for bin in "${_required_bins[@]}"; do
    [ -f "$bin" ] || { echo "FATAL: missing $bin"; exit 1; }
    file "$bin"
done

# F-21 rule: llama-server must be > 1 MB (static, not thin)
for arch in amd64 arm64; do
    [ "$arch" = "arm64" ] && [ "$V67_INCLUDE_ARM64" != "1" ] && continue
    dir="build-portable"
    [ "$arch" = "arm64" ] && dir="build-arm64"
    llama="cx-distro/.build/llama.cpp/${dir}/bin/llama-server"
    llama_size="$(stat -c %s "$llama")"
    if [ "$llama_size" -lt 1000000 ]; then
        echo "FATAL: llama-server ($arch) is ${llama_size} bytes — F-21: not static"
        exit 1
    fi
    echo "  llama-server $arch: ${llama_size} bytes (static, F-21-safe)"
done
echo "✓ Step 3: binaries verified"

# ── Step 4: mcpd-harvest.sh on host ──────────────────────────────────────
echo ""
echo "▶ Step 4: mcpd-harvest.sh on fresh amd64 mcpd $(date -u)"
sudo bash incremental/build/mcpd-harvest.sh
echo "✓ Step 4: harvest gate GREEN"

# v6.13_OC Fix L' Commit 1: build one arch × one edition. Called once per
# edition when V67_EDITION=current|oc, or twice when V67_EDITION=both.
# ISO filename gains _OC suffix when EDITION=oc — see build-iso.sh.
_build_arch() {
    local arch="$1"
    local edition="$2"
    local step="$3"
    local iso_suffix=""
    [ "$edition" = "oc" ] && iso_suffix="_OC"
    local iso_path="incremental/.build/out/${LABEL}${iso_suffix}-${arch}.iso"
    echo ""
    echo "▶ Step ${step}: make iso-${arch} EDITION=${edition} $(date -u)"
    sudo make "iso-${arch}" LABEL="$LABEL" VN="$VN" EDITION="$edition"
    [ -f "$iso_path" ] || { echo "FATAL: expected $iso_path missing"; exit 1; }
    echo "✓ Step ${step}: ${iso_path} produced ($(du -h "$iso_path" | awk '{print $1}'))"
}

# Editions to build, in order.
_editions_to_build=()
case "$V67_EDITION" in
    current) _editions_to_build=(current) ;;
    oc)      _editions_to_build=(oc) ;;
    both)    _editions_to_build=(current oc) ;;
esac

_step=5
for _ed in "${_editions_to_build[@]}"; do
    # ── amd64 ISO for this edition ───────────────────────────────────────
    _build_arch amd64 "$_ed" "$_step"
    _step=$((_step + 1))

    # ── arm64 ISO for this edition (optional) ────────────────────────────
    if [ "$V67_INCLUDE_ARM64" = "1" ]; then
        _build_arch arm64 "$_ed" "$_step"
    else
        echo ""
        echo "▶ Step ${_step}: arm64 skipped (V67_INCLUDE_ARM64=0) [edition=${_ed}]"
    fi
    _step=$((_step + 1))
done

# ── Step 7: SHA-256 + report ─────────────────────────────────────────────
# Glob covers both editions: "${LABEL}-*.iso" (current) + "${LABEL}_OC-*.iso" (oc).
echo ""
echo "▶ Step 7: SHA-256s"
sha256sum "incremental/.build/out/${LABEL}"-*.iso "incremental/.build/out/${LABEL}_OC-"*.iso 2>/dev/null || true

echo ""
echo "══════════════════════════════════════════════════════════════════"
echo "═══ ${LABEL} build COMPLETE at $(date -u) [edition=${V67_EDITION}] ═══"
echo "══════════════════════════════════════════════════════════════════"
echo ""
echo "Artifacts on VM:"
ls -lh "incremental/.build/out/${LABEL}"-*.iso "incremental/.build/out/${LABEL}_OC-"*.iso 2>/dev/null || true
echo ""
echo "Next steps:"
echo ""
echo "1. QEMU gate (amd64, ~5 min KVM):"
echo "   sudo bash incremental/tests/qemu-gate.sh --arch amd64"
if [ "$V67_INCLUDE_ARM64" = "1" ]; then
    echo ""
    echo "2. QEMU gate (arm64, ~40 min TCG):"
    echo "   sudo bash incremental/tests/qemu-gate.sh --arch arm64"
fi
echo ""
echo "3. Download to Mac:"
echo "   gcloud compute scp icebreaker-build-vm:${REPO}/incremental/.build/out/${LABEL}-\\*.iso ~/Documents/Icebreaker/ISO/incremental/ --zone=us-central1-a --project=project-ef281c18-2a28-4139-a89"
echo ""
echo "4. Boot in QEMU (amd64, Intel Mac / KVM) or UTM (Apple Silicon)."
