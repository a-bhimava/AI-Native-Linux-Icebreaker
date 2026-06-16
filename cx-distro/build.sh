#!/usr/bin/env bash
# build.sh — Master build script for the Icebreaker AI-Native OS ISO.
#
# Produces a bootable Ubuntu live ISO with both AI brains (llama-server),
# mcpd, the Controller, model weights, and system configuration.
#
# Usage:
#   cd cx-distro && sudo bash build.sh [OPTIONS]
#
# Options:
#   --skip-to=N     Skip stages 0..N-1 (e.g. --skip-to=4 to skip binary builds)
#   --force         Allow running outside Docker
#   --no-models     Skip GGUF model embedding (fast ISO for testing boot flow)
#
# Stages:
#   0  Preflight    — verify repo state, checksums, dependencies
#   1  mcpd         — cargo build --release
#   2  llama-server — clone + build llama.cpp at pinned commit
#   3  venv         — create Python venv, pip install dual-brain/
#   4  chroot       — assemble config/includes.chroot/ tree
#   5  iso          — lb config && lb build
#
# SECURITY: This file is listed in CLAUDE.md Security-Critical Files.
# Any change requires human review from the module owner.
set -euo pipefail

# ── Constants ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${SCRIPT_DIR}/.build"
CHROOT="${SCRIPT_DIR}/config/includes.chroot"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# ── Helpers ─────────────────────────────────────────────────────────────

die() {
    printf "${RED}FATAL:${NC} %s\n" "$*" >&2
    exit 1
}

info() {
    printf "${GREEN}[%s]${NC} %s\n" "$(date +%H:%M:%S)" "$*"
}

warn() {
    printf "${YELLOW}WARN:${NC} %s\n" "$*" >&2
}

stage_banner() {
    printf "\n${BOLD}════ Stage %s: %s ════${NC}\n\n" "$1" "$2"
}

# ── Argument parsing ────────────────────────────────────────────────────
SKIP_TO=0
FORCE=0
NO_MODELS=0

for arg in "$@"; do
    case "$arg" in
        --skip-to=*)
            SKIP_TO="${arg#--skip-to=}"
            if ! [[ "$SKIP_TO" =~ ^[0-5]$ ]]; then
                die "--skip-to must be 0-5, got: $SKIP_TO"
            fi
            ;;
        --force)
            FORCE=1
            ;;
        --no-models)
            NO_MODELS=1
            ;;
        --help|-h)
            head -25 "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            die "unknown argument: $arg"
            ;;
    esac
done

# ── Stage 0: Preflight ─────────────────────────────────────────────────

if [ "$SKIP_TO" -le 0 ]; then
    stage_banner 0 "Preflight"

    # Check we are inside Docker (or --force).
    if [ "$FORCE" -eq 0 ] && [ ! -f /.dockerenv ]; then
        die "build.sh must run inside Docker. Use: docker build -t icebreaker-build -f Dockerfile.build .. && docker run --privileged -v \$(pwd)/..:/build icebreaker-build   (or --force for local builds)"
    fi

    # Verify repo structure.
    [ -f "${REPO_ROOT}/models/checksums.sha256" ] || \
        die "models/checksums.sha256 not found (INV-7)"
    [ -d "${REPO_ROOT}/src/mcpd" ] || \
        die "src/mcpd/ not found"
    [ -f "${REPO_ROOT}/dual-brain/pyproject.toml" ] || \
        die "dual-brain/pyproject.toml not found"

    # INV-7: Verify all model checksums.
    if [ "$NO_MODELS" -eq 0 ]; then
        info "Verifying model checksums (INV-7)..."
        CHECKSUM_FAILURES=0
        while IFS= read -r line; do
            # Skip empty lines and comments.
            [ -z "$line" ] && continue
            [[ "$line" == \#* ]] && continue

            EXPECTED_HASH=$(echo "$line" | awk '{print $1}')
            RAW_PATH=$(echo "$line" | awk '{print $2}')
            FILENAME=$(basename "$RAW_PATH")

            MODEL_FILE="${REPO_ROOT}/models/${FILENAME}"
            if [ ! -f "$MODEL_FILE" ]; then
                warn "model file not found: ${FILENAME} (listed in checksums.sha256)"
                CHECKSUM_FAILURES=$((CHECKSUM_FAILURES + 1))
                continue
            fi

            ACTUAL_HASH=$(sha256sum "$MODEL_FILE" | awk '{print $1}')
            if [ "$EXPECTED_HASH" != "$ACTUAL_HASH" ]; then
                warn "checksum MISMATCH for ${FILENAME}"
                warn "  expected: ${EXPECTED_HASH}"
                warn "  actual:   ${ACTUAL_HASH}"
                CHECKSUM_FAILURES=$((CHECKSUM_FAILURES + 1))
            else
                info "  ${FILENAME}: checksum OK"
            fi
        done < "${REPO_ROOT}/models/checksums.sha256"

        if [ "$CHECKSUM_FAILURES" -gt 0 ]; then
            die "INV-7 violated: ${CHECKSUM_FAILURES} checksum failure(s). Aborting."
        fi
        info "All model checksums verified."
    else
        warn "--no-models: skipping GGUF checksum verification"
    fi

    # Create build directory.
    mkdir -p "${BUILD_DIR}"

    # Write build manifest.
    cat > "${BUILD_DIR}/manifest.json" <<MANIFEST
{
  "build_date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "git_sha": "$(cd "${REPO_ROOT}" && git rev-parse HEAD 2>/dev/null || echo 'unknown')",
  "git_dirty": $(cd "${REPO_ROOT}" && git diff --quiet 2>/dev/null && echo false || echo true),
  "ubuntu_base": "$(cat "${SCRIPT_DIR}/UBUNTU_BASE")",
  "llama_cpp_commit": "$(cat "${SCRIPT_DIR}/LLAMA_CPP_COMMIT")",
  "no_models": ${NO_MODELS}
}
MANIFEST
    info "Build manifest written to .build/manifest.json"
fi

# ── Stage 1: Build mcpd ────────────────────────────────────────────────

if [ "$SKIP_TO" -le 1 ]; then
    stage_banner 1 "Build mcpd"

    cd "${REPO_ROOT}/src/mcpd"
    info "Building mcpd (release)..."
    cargo build --release 2>&1 | tail -5

    MCPD_BIN="target/release/mcpd"
    [ -f "$MCPD_BIN" ] || die "mcpd binary not found after build"

    # Security check: no test-only features compiled in.
    if strings "$MCPD_BIN" | grep -q MCPD_FS_TEST_ROOTS; then
        die "mcpd compiled with test-only feature fs-test-roots (CLAUDE.md § Test-Only Knobs)"
    fi
    info "mcpd binary OK ($(du -h "$MCPD_BIN" | awk '{print $1}'), no test features)"

    cp "$MCPD_BIN" "${BUILD_DIR}/mcpd"
    cd "${SCRIPT_DIR}"
fi

# ── Stage 2: Build llama-server ─────────────────────────────────────────

if [ "$SKIP_TO" -le 2 ]; then
    stage_banner 2 "Build llama-server"

    LLAMA_COMMIT=$(cat "${SCRIPT_DIR}/LLAMA_CPP_COMMIT" | tr -d '[:space:]')
    LLAMA_SRC="${BUILD_DIR}/llama.cpp"

    # Clone or update.
    if [ -d "${LLAMA_SRC}/.git" ]; then
        info "llama.cpp source exists, fetching..."
        cd "${LLAMA_SRC}" && git fetch origin
    else
        info "Cloning llama.cpp..."
        rm -rf "${LLAMA_SRC}"
        git clone --filter=blob:none https://github.com/ggerganov/llama.cpp "${LLAMA_SRC}"
        cd "${LLAMA_SRC}"
    fi

    info "Checking out commit ${LLAMA_COMMIT}..."
    git checkout "${LLAMA_COMMIT}" || die "failed to checkout llama.cpp commit ${LLAMA_COMMIT}"

    info "Building llama-server (CPU-only)..."
    cmake -B build \
        -DGGML_CUDA=OFF \
        -DGGML_METAL=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_SERVER=ON \
        2>&1 | tail -3
    cmake --build build --target llama-server -j"$(nproc)" 2>&1 | tail -5

    LLAMA_BIN="build/bin/llama-server"
    [ -f "$LLAMA_BIN" ] || die "llama-server binary not found after build"
    info "llama-server OK ($(du -h "$LLAMA_BIN" | awk '{print $1}'))"

    cp "$LLAMA_BIN" "${BUILD_DIR}/llama-server"
    cd "${SCRIPT_DIR}"
fi

# ── Stage 3: Build Python venv ──────────────────────────────────────────

if [ "$SKIP_TO" -le 3 ]; then
    stage_banner 3 "Build Python venv"

    VENV_DIR="${BUILD_DIR}/venv"
    rm -rf "${VENV_DIR}"
    info "Creating venv..."
    python3 -m venv "${VENV_DIR}"

    info "Installing icebreaker-controller..."
    "${VENV_DIR}/bin/pip" install --no-cache-dir "${REPO_ROOT}/dual-brain/" 2>&1 | tail -5

    # Verify entry point.
    "${VENV_DIR}/bin/python3" -m controller --help >/dev/null 2>&1 || \
        die "venv broken: python3 -m controller --help failed"
    info "Venv OK ($(du -sh "${VENV_DIR}" | awk '{print $1}'))"
fi

# ── Stage 4: Assemble chroot tree ──────────────────────────────────────

if [ "$SKIP_TO" -le 4 ]; then
    stage_banner 4 "Assemble chroot tree"

    # Clean previous chroot assembly.
    rm -rf "${CHROOT}"
    mkdir -p "${CHROOT}"

    # ── /etc/icebreaker/ ────────────────────────────────────────────────
    install -Dm644 "${SCRIPT_DIR}/distro/controller.toml" \
        "${CHROOT}/etc/icebreaker/controller.toml"
    install -Dm644 "${SCRIPT_DIR}/distro/locations.env" \
        "${CHROOT}/etc/icebreaker/locations.env"

    # ── /etc/systemd/system/ ────────────────────────────────────────────
    for unit in "${REPO_ROOT}"/dual-brain/controller/systemd/*.service \
                "${REPO_ROOT}"/dual-brain/controller/systemd/*.socket; do
        [ -f "$unit" ] || continue
        install -Dm644 "$unit" "${CHROOT}/etc/systemd/system/$(basename "$unit")"
    done

    # ── /etc/sysusers.d/ and /etc/tmpfiles.d/ ──────────────────────────
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/systemd/icebreaker.sysusers.d.conf" \
        "${CHROOT}/etc/sysusers.d/icebreaker.conf"
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/systemd/icebreaker.tmpfiles.d.conf" \
        "${CHROOT}/etc/tmpfiles.d/icebreaker.conf"

    # ── /usr/bin/ ───────────────────────────────────────────────────────
    install -Dm755 "${SCRIPT_DIR}/distro/icebreaker-cli" \
        "${CHROOT}/usr/bin/icebreaker"

    # ── /usr/libexec/icebreaker/ ────────────────────────────────────────
    install -Dm755 "${BUILD_DIR}/mcpd" \
        "${CHROOT}/usr/libexec/icebreaker/mcpd"
    install -Dm755 "${BUILD_DIR}/llama-server" \
        "${CHROOT}/usr/libexec/icebreaker/llama-server"
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/start-pbd" \
        "${CHROOT}/usr/libexec/icebreaker/start-pbd"
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/start-qbd" \
        "${CHROOT}/usr/libexec/icebreaker/start-qbd"

    # ── /usr/share/icebreaker/ ──────────────────────────────────────────
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/catalogue.toml" \
        "${CHROOT}/usr/share/icebreaker/catalogue.toml"
    install -Dm644 "${REPO_ROOT}/dual-brain/controller/grammars/qb_intent.gbnf" \
        "${CHROOT}/usr/share/icebreaker/grammars/qb_intent.gbnf"

    # Schemas: controller + mcpd tool schemas in one directory.
    for schema in "${REPO_ROOT}"/dual-brain/controller/schemas/*.json \
                  "${REPO_ROOT}"/src/mcpd/schemas/*.json; do
        [ -f "$schema" ] || continue
        install -Dm644 "$schema" \
            "${CHROOT}/usr/share/icebreaker/schemas/$(basename "$schema")"
    done

    # System prompts.
    for prompt in "${REPO_ROOT}"/dual-brain/controller/prompts/*.txt; do
        [ -f "$prompt" ] || continue
        install -Dm644 "$prompt" \
            "${CHROOT}/usr/share/icebreaker/prompts/$(basename "$prompt")"
    done

    # ── /opt/icebreaker/venv/ ───────────────────────────────────────────
    mkdir -p "${CHROOT}/opt/icebreaker"
    cp -a "${BUILD_DIR}/venv" "${CHROOT}/opt/icebreaker/venv"

    # ── /var/lib/icebreaker/models/ ─────────────────────────────────────
    install -Dm644 "${REPO_ROOT}/models/checksums.sha256" \
        "${CHROOT}/var/lib/icebreaker/models/checksums.sha256"

    if [ "$NO_MODELS" -eq 0 ]; then
        for gguf in "${REPO_ROOT}"/models/*.gguf; do
            [ -f "$gguf" ] || continue
            info "Embedding model: $(basename "$gguf") ($(du -h "$gguf" | awk '{print $1}'))"
            install -Dm644 "$gguf" \
                "${CHROOT}/var/lib/icebreaker/models/$(basename "$gguf")"
        done
    else
        warn "--no-models: skipping GGUF embedding"
    fi

    # ── Build manifest ──────────────────────────────────────────────────
    install -Dm644 "${BUILD_DIR}/manifest.json" \
        "${CHROOT}/usr/share/icebreaker/build-manifest.json"

    # Count installed files.
    FILE_COUNT=$(find "${CHROOT}" -type f | wc -l | tr -d ' ')
    info "Chroot assembled: ${FILE_COUNT} files"
fi

# ── Stage 5: Build ISO ─────────────────────────────────────────────────

if [ "$SKIP_TO" -le 5 ]; then
    stage_banner 5 "Build ISO"

    cd "${SCRIPT_DIR}"
    UBUNTU_BASE=$(cat "${SCRIPT_DIR}/UBUNTU_BASE" | tr -d '[:space:]')

    info "Configuring live-build (${UBUNTU_BASE})..."
    lb config \
        --distribution "${UBUNTU_BASE}" \
        --archive-areas "main restricted universe" \
        --bootloaders grub-efi \
        --binary-images iso-hybrid \
        --iso-application "Icebreaker AI-Native OS" \
        --iso-volume "ICEBREAKER" \
        2>&1 | tail -3

    info "Building ISO (this may take several minutes)..."
    lb build 2>&1 | tee "${BUILD_DIR}/lb-build.log" | tail -20

    ISO_FILE=$(ls -1 live-image-*.iso 2>/dev/null | head -1)
    if [ -z "$ISO_FILE" ]; then
        die "ISO file not found after lb build"
    fi

    sha256sum "$ISO_FILE" > "${BUILD_DIR}/iso.sha256"
    ISO_SIZE=$(du -h "$ISO_FILE" | awk '{print $1}')
    ISO_HASH=$(awk '{print $1}' "${BUILD_DIR}/iso.sha256")

    info "ISO built: ${ISO_FILE} (${ISO_SIZE})"
    info "SHA-256: ${ISO_HASH}"
    info "Build complete."
fi
