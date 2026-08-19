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
#   --profile=NAME  Build profile: vm (default) or desktop
#                     vm      — XFCE, LightDM, nomodeset, no toram (low RAM, emulation)
#                     desktop — GNOME, GDM, toram, full GPU (bare-metal / native virt)
#   --skip-to=N     Skip stages 0..N-1 (e.g. --skip-to=4 to skip binary builds)
#   --stop-after=N  Stop after stage N (e.g. --stop-after=2 for binaries only)
#   --force         Allow running outside Docker
#   --no-models     Skip GGUF model embedding (fast ISO for testing boot flow)
#
# Stages:
#   0  Preflight    — verify repo state, checksums, dependencies
#   1  mcpd         — cargo build --release
#   2  llama-server — clone + build llama.cpp at pinned commit
#   3  venv         — create Python venv, pip install dual-brain/
#   4  chroot       — assemble config/includes.chroot/ tree
#   5  iso          — debootstrap + mksquashfs + xorriso (hybrid BIOS+EFI)
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
STOP_AFTER=5
FORCE=0
NO_MODELS=0
PROFILE="vm"

for arg in "$@"; do
    case "$arg" in
        --profile=*)
            PROFILE="${arg#--profile=}"
            if [[ "$PROFILE" != "vm" && "$PROFILE" != "desktop" ]]; then
                die "--profile must be 'vm' or 'desktop', got: $PROFILE"
            fi
            ;;
        --skip-to=*)
            SKIP_TO="${arg#--skip-to=}"
            if ! [[ "$SKIP_TO" =~ ^[0-5]$ ]]; then
                die "--skip-to must be 0-5, got: $SKIP_TO"
            fi
            ;;
        --stop-after=*)
            STOP_AFTER="${arg#--stop-after=}"
            if ! [[ "$STOP_AFTER" =~ ^[0-5]$ ]]; then
                die "--stop-after must be 0-5, got: $STOP_AFTER"
            fi
            ;;
        --force)
            FORCE=1
            ;;
        --no-models)
            NO_MODELS=1
            ;;
        --help|-h)
            head -28 "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            die "unknown argument: $arg"
            ;;
    esac
done

info "Build profile: ${PROFILE}"

# ── Stage 0: Preflight ─────────────────────────────────────────────────

if [ "$SKIP_TO" -le 0 ] && [ "$STOP_AFTER" -ge 0 ]; then
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
    if [ "$PROFILE" = "vm" ]; then
        [ -f "${SCRIPT_DIR}/vendor/appearance-sources.sha256" ] || die "appearance artifact lock file missing"
        (cd "${SCRIPT_DIR}/vendor" && sha256sum -c appearance-sources.sha256 --quiet) || die "appearance artifact checksum failure"
    fi

    # `--force` is used for the dedicated cloud build VM. Fail before any
    # compile/clone work if the selected stages' host tools are absent; the
    # Dockerfile remains the canonical way to provision them.
    REQUIRED_BUILD_TOOLS=()
    if [ "$SKIP_TO" -le 1 ] && [ "$STOP_AFTER" -ge 1 ]; then
        REQUIRED_BUILD_TOOLS+=(cargo strings file)
    fi
    if [ "$SKIP_TO" -le 2 ] && [ "$STOP_AFTER" -ge 2 ]; then
        REQUIRED_BUILD_TOOLS+=(cmake git c++ aarch64-linux-gnu-gcc aarch64-linux-gnu-g++)
    fi
    for tool in "${REQUIRED_BUILD_TOOLS[@]}"; do
        command -v "$tool" >/dev/null 2>&1 || die \
            "required build tool not found: $tool (use cx-distro/Dockerfile.build or install it before --force)"
    done

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
  "no_models": ${NO_MODELS},
  "profile": "${PROFILE}"
}
MANIFEST
    info "Build manifest written to .build/manifest.json"
fi

# ── Stage 1: Build mcpd ────────────────────────────────────────────────

if [ "$SKIP_TO" -le 1 ] && [ "$STOP_AFTER" -ge 1 ]; then
    stage_banner 1 "Build mcpd (per-arch)"

    cd "${REPO_ROOT}/src/mcpd"

    # Scope G (2026-07-11): loop over both arches so a single build.sh run
    # produces mcpd-amd64 AND mcpd-arm64. Downstream (build-iso.sh:82-107,
    # v2.manifest:174-197) already dispatches on mcpd-${ARCH}. The target and
    # any cross-linker come from config/archs/${arch}.conf (BP-1).
    for target_arch in amd64 arm64; do
        arch_conf="${REPO_ROOT}/config/archs/${target_arch}.conf"
        [ -f "$arch_conf" ] || die "missing architecture config: $arch_conf"
        # Keep linker variables scoped to this target.  This prevents an ARM
        # cross-linker from accidentally leaking into a later native build.
        (
            # shellcheck source=/dev/null
            source "$arch_conf"
            # Use tr rather than Bash 4-only case conversion so the build
            # script remains syntax/logic-checkable on the macOS host too.
            linker_var="$(printf 'CARGO_TARGET_%s_LINKER' "$RUST_TRIPLE" | tr '[:lower:]-' '[:upper:]_')"
            if [ -n "${RUST_LINKER:-}" ]; then
                command -v "$RUST_LINKER" >/dev/null || die \
                    "cross-linker for ${target_arch} not found: ${RUST_LINKER}"
                export "${linker_var}=${RUST_LINKER}"
            else
                unset "$linker_var"
            fi

            info "Building mcpd for ${target_arch} (${RUST_TRIPLE})..."
            cargo build --release --target="${RUST_TRIPLE}" 2>&1 | tail -5

            MCPD_BIN="target/${RUST_TRIPLE}/release/mcpd"
            [ -f "$MCPD_BIN" ] || die "mcpd binary not found after build (target=${target_arch})"

            # Security check: no test-only features compiled in.
            if strings "$MCPD_BIN" | grep -q MCPD_FS_TEST_ROOTS; then
                die "mcpd (${target_arch}) compiled with test-only feature fs-test-roots (CLAUDE.md § Test-Only Knobs)"
            fi

            # F-40 arch cross-check: file(1) description must name the target
            # arch. Same guard v2.manifest:188-197 does at ISO-manifest time;
            # we run it here to catch cross-compile silent-fallthrough (e.g.
            # cargo silently produces amd64 despite --target=aarch64) BEFORE
            # spending an ISO-build cycle on it.
            if command -v file >/dev/null 2>&1; then
                file_desc="$(file "$MCPD_BIN" 2>/dev/null || true)"
                case "${target_arch}:${file_desc}" in
                    amd64:*"x86-64"*|amd64:*"x86_64"*) : ;;
                    arm64:*"ARM aarch64"*) : ;;
                    *) die "arch mismatch after build (target=${target_arch}, file: ${file_desc})" ;;
                esac
            fi

            info "mcpd-${target_arch} OK ($(du -h "$MCPD_BIN" | awk '{print $1}'), no test features, arch verified)"

            # Per-arch output naming — consumed by build-iso.sh + v2.manifest.
            cp "$MCPD_BIN" "${BUILD_DIR}/mcpd-${target_arch}"
        ) || die "mcpd build failed for ${target_arch}"
    done

    # Backward compat: legacy unsuffixed mcpd = mcpd-amd64. Some V0-V6.51
    # rebuild paths still `cp cx-distro/.build/mcpd` directly.
    cp "${BUILD_DIR}/mcpd-amd64" "${BUILD_DIR}/mcpd"

    # Scope G (2026-07-11): refresh models/checksums.sha256 with the fresh
    # mcpd binaries. INV-7 says every shipped binary carries an authoritative
    # hash. Existing model entries (run7_cot_q4km.gguf etc.) are preserved;
    # only mcpd-* lines are replaced.
    CHECKSUMS="${REPO_ROOT}/models/checksums.sha256"
    if [ -f "$CHECKSUMS" ]; then
        TMP_CHECKSUMS="$(mktemp)"
        # Keep everything that isn't a mcpd-* entry
        grep -v -E ' (mcpd|mcpd-amd64|mcpd-arm64)$' "$CHECKSUMS" > "$TMP_CHECKSUMS" || true
        # Append fresh mcpd hashes (paths relative to repo root, sha256sum -c friendly)
        (cd "${REPO_ROOT}" && sha256sum \
            "cx-distro/.build/mcpd-amd64" \
            "cx-distro/.build/mcpd-arm64" \
            2>/dev/null | awk '{ print $1 "  " $2 }') >> "$TMP_CHECKSUMS"
        mv "$TMP_CHECKSUMS" "$CHECKSUMS"
        info "Updated ${CHECKSUMS} with fresh mcpd-amd64 + mcpd-arm64 hashes"
    fi

    cd "${SCRIPT_DIR}"
fi

# ── Stage 2: Build llama-server ─────────────────────────────────────────

if [ "$SKIP_TO" -le 2 ] && [ "$STOP_AFTER" -ge 2 ]; then
    stage_banner 2 "Build llama-server (per-arch)"

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

    # Scope I (2026-07-12): per-arch build. v6.manifest:37 hard-codes the
    # expected path as .build/llama.cpp/${LLAMA_BUILD_DIR}/bin/llama-server
    # where LLAMA_BUILD_DIR = build-portable (amd64) or build-arm64 (arm64).
    # config/archs/{arch}.conf is the single source of truth for both the
    # cmake flags and the build-dir name (BP-1).
    for target_arch in amd64 arm64; do
        # Source per-arch config for LLAMA_CMAKE_FLAGS + LLAMA_BUILD_DIR.
        arch_conf="${REPO_ROOT}/config/archs/${target_arch}.conf"
        [ -f "$arch_conf" ] || die "config/archs/${target_arch}.conf missing"
        # shellcheck source=/dev/null
        (
            source "$arch_conf"
            info "Building llama-server for ${target_arch} (${LLAMA_BUILD_DIR})..."
            # Common CPU-only flags + arch-specific from ${LLAMA_CMAKE_FLAGS}.
            LLAMA_CMAKE_FLAGS_COMMON=(
                -DCMAKE_BUILD_TYPE=Release
                -DLLAMA_BUILD_TESTS=OFF
                -DLLAMA_BUILD_EXAMPLES=OFF
                -DLLAMA_BUILD_SERVER=ON
                # F-21 (2026-07-04): the default shared-library build ships
                # a 17-20 KB llama-server binary that dlopen's
                # libllama-server-impl.so at runtime. That .so is not in
                # the ISO chroot → ldd check at v6.manifest:72-76 hard-fails.
                # Force static so llama-server is a self-contained binary.
                -DBUILD_SHARED_LIBS=OFF
            )
            # Arch-specific cross-compile: for arm64 point cmake at the
            # aarch64-linux-gnu cross toolchain that Dockerfile.build installs.
            if [ "$target_arch" = "arm64" ]; then
                LLAMA_CMAKE_FLAGS_ARCH=(
                    -DCMAKE_SYSTEM_NAME=Linux
                    -DCMAKE_SYSTEM_PROCESSOR=aarch64
                    -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc
                    -DCMAKE_CXX_COMPILER=aarch64-linux-gnu-g++
                )
            else
                LLAMA_CMAKE_FLAGS_ARCH=()
            fi
            # Split ${LLAMA_CMAKE_FLAGS} (from arch conf) into an array.
            read -r -a llama_flags_from_conf <<<"$LLAMA_CMAKE_FLAGS"

            rm -rf "${LLAMA_BUILD_DIR}"
            cmake -B "${LLAMA_BUILD_DIR}" \
                "${LLAMA_CMAKE_FLAGS_COMMON[@]}" \
                "${LLAMA_CMAKE_FLAGS_ARCH[@]}" \
                "${llama_flags_from_conf[@]}" \
                2>&1 | tail -3
            cmake --build "${LLAMA_BUILD_DIR}" --target llama-server -j"$(nproc)" 2>&1 | tail -5

            LLAMA_BIN="${LLAMA_BUILD_DIR}/bin/llama-server"
            [ -f "$LLAMA_BIN" ] || die "llama-server binary not found for ${target_arch} at ${LLAMA_BIN}"

            # Arch cross-check (same guard as mcpd Stage 1).
            if command -v file >/dev/null 2>&1; then
                file_desc="$(file "$LLAMA_BIN" 2>/dev/null || true)"
                case "${target_arch}:${file_desc}" in
                    amd64:*"x86-64"*|amd64:*"x86_64"*) : ;;
                    arm64:*"ARM aarch64"*) : ;;
                    *) die "arch mismatch after llama-server build (target=${target_arch}, file: ${file_desc})" ;;
                esac
            fi

            info "llama-server ${target_arch} OK ($(du -h "$LLAMA_BIN" | awk '{print $1}'), arch verified)"
        ) || die "llama-server build failed for ${target_arch}"
    done

    # Backward compat: preserve the pre-Scope-I unsuffixed `llama-server`
    # copy that build.sh Stage 4 (line ~374) uses. Points at amd64.
    cp "build-portable/bin/llama-server" "${BUILD_DIR}/llama-server"

    cd "${SCRIPT_DIR}"
fi

# ── Stage 3: Build Python venv ──────────────────────────────────────────

if [ "$SKIP_TO" -le 3 ] && [ "$STOP_AFTER" -ge 3 ]; then
    stage_banner 3 "Build Python venv"

    VENV_DIR="${BUILD_DIR}/venv"
    rm -rf "${VENV_DIR}"
    info "Creating venv..."
    python3 -m venv --system-site-packages "${VENV_DIR}"

    info "Installing icebreaker-controller..."
    "${VENV_DIR}/bin/pip" install --no-cache-dir "${REPO_ROOT}/dual-brain/[gui,rpa]" 2>&1 | tail -5

    # pip doesn't ship data files — copy them into the installed package.
    CONTROLLER_PKG=$(find "${VENV_DIR}" -path '*/site-packages/controller/model_registry.py' \
        -exec dirname {} \; | head -1)
    if [ -n "$CONTROLLER_PKG" ]; then
        cp "${REPO_ROOT}/dual-brain/controller/catalogue.toml" "${CONTROLLER_PKG}/catalogue.toml"
        cp "${REPO_ROOT}/dual-brain/controller/tool_catalogue.yaml" "${CONTROLLER_PKG}/tool_catalogue.yaml"
        cp -a "${REPO_ROOT}/dual-brain/controller/schemas"  "${CONTROLLER_PKG}/schemas"
        cp -a "${REPO_ROOT}/dual-brain/controller/prompts"  "${CONTROLLER_PKG}/prompts"
        cp -a "${REPO_ROOT}/dual-brain/controller/grammars" "${CONTROLLER_PKG}/grammars"
        cp -a "${REPO_ROOT}/dual-brain/controller/manifests" "${CONTROLLER_PKG}/manifests"
        info "Data files copied into venv package"
    fi

    # Verify entry point.
    "${VENV_DIR}/bin/python3" -m controller --help >/dev/null 2>&1 || \
        die "venv broken: python3 -m controller --help failed"
    info "Venv OK ($(du -sh "${VENV_DIR}" | awk '{print $1}'))"
fi

# ── Stage 4: Assemble chroot tree ──────────────────────────────────────

if [ "$SKIP_TO" -le 4 ] && [ "$STOP_AFTER" -ge 4 ]; then
    stage_banner 4 "Assemble chroot tree"

    # The VM visual profile consumes pinned, third-party appearance artifacts.
    # Re-check here so a Stage-4-only rerun cannot bypass the Stage-0 lock.
    if [ "$PROFILE" = "vm" ]; then
        (cd "${SCRIPT_DIR}/vendor" && sha256sum -c appearance-sources.sha256 --quiet) || \
            die "appearance artifact checksum failure"
    fi

    # Clean previous chroot assembly.
    rm -rf "${CHROOT}"
    mkdir -p "${CHROOT}"

    # ── /etc/icebreaker/ ────────────────────────────────────────────────
    install -Dm644 "${SCRIPT_DIR}/distro/controller.toml" \
        "${CHROOT}/etc/icebreaker/controller.toml"
    install -Dm644 "${SCRIPT_DIR}/distro/locations.env" \
        "${CHROOT}/etc/icebreaker/locations.env"
    install -Dm644 "${SCRIPT_DIR}/distro/10-icebreaker.rules" \
        "${CHROOT}/etc/polkit-1/rules.d/10-icebreaker.rules"

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
    install -Dm755 "${SCRIPT_DIR}/distro/mount-mac-share" \
        "${CHROOT}/usr/bin/mount-mac-share"
    # ib_debug.py — Icebreaker System Diagnostic Monitor
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/ib_debug.py" \
        "${CHROOT}/usr/bin/ib_debug.py"
    # convenience symlink: 'ib-debug snapshot' instead of 'python3 /usr/bin/ib_debug.py snapshot'
    ln -sf /usr/bin/ib_debug.py "${CHROOT}/usr/bin/ib-debug" 2>/dev/null || true
    # v6.10 P5 (F-72): v6.8 sprint debug/corpus helpers. All three were
    # written for the AgentGraph migration and left out of every ISO cut
    # since. In-guest debugging of a broken pipeline required manually
    # scp'ing them from the host every time.
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/ib_debug_v68.py" \
        "${CHROOT}/usr/bin/ib_debug_v68.py"
    ln -sf /usr/bin/ib_debug_v68.py "${CHROOT}/usr/bin/ib-debug-v68" 2>/dev/null || true
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/ib_run_v68.py" \
        "${CHROOT}/usr/bin/ib_run_v68.py"
    ln -sf /usr/bin/ib_run_v68.py "${CHROOT}/usr/bin/ib-run-v68" 2>/dev/null || true
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/ib_run_corpus.py" \
        "${CHROOT}/usr/bin/ib_run_corpus.py"
    ln -sf /usr/bin/ib_run_corpus.py "${CHROOT}/usr/bin/ib-run-corpus" 2>/dev/null || true
    # ib_bundle.py — diagnostic bundle collector (Scope G.5, 2026-07-12).
    # Collects logs, journals, harvest output, boot-report, models, markers,
    # config (redacted) into ~/icebreaker-bundle-<ts>-<arch>.tar.zst.
    # Called by first-boot on red; user runs manually when something is
    # broken and they want to attach a diag to a GitHub issue.
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/ib_bundle.py" \
        "${CHROOT}/usr/local/bin/ib-bundle"

    # ── /usr/libexec/icebreaker/ ────────────────────────────────────────
    install -Dm755 "${BUILD_DIR}/mcpd" \
        "${CHROOT}/usr/libexec/icebreaker/mcpd"
    install -Dm755 "${BUILD_DIR}/llama-server" \
        "${CHROOT}/usr/libexec/icebreaker/llama-server"
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/start-pbd" \
        "${CHROOT}/usr/libexec/icebreaker/start-pbd"
    install -Dm755 "${REPO_ROOT}/dual-brain/scripts/start-qbd" \
        "${CHROOT}/usr/libexec/icebreaker/start-qbd"
    install -Dm755 "${SCRIPT_DIR}/distro/first-boot" \
        "${CHROOT}/usr/libexec/icebreaker/first-boot"
    install -Dm755 "${SCRIPT_DIR}/distro/safe-mode" \
        "${CHROOT}/usr/libexec/icebreaker/safe-mode"
    install -Dm755 "${SCRIPT_DIR}/distro/wait-for-sockets" \
        "${CHROOT}/usr/libexec/icebreaker/wait-for-sockets"

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

    # Shell # trigger — installed for all users via /etc/skel.
    install -Dm755 "${REPO_ROOT}/shell/ib_trigger.bash" \
        "${CHROOT}/usr/share/icebreaker/shell/ib_trigger.bash"
    mkdir -p "${CHROOT}/etc/skel"
    {
        echo ""
        echo "# Icebreaker # trigger — type \"# <intent>\" to run AI commands"
        echo "source /usr/share/icebreaker/shell/ib_trigger.bash"
    } >> "${CHROOT}/etc/skel/.bashrc"

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

    # ── .desktop files ─────────────────────────────────────────────────
    for desktop_file in "${SCRIPT_DIR}"/distro/*.desktop; do
        [ -f "$desktop_file" ] || continue
        install -Dm644 "$desktop_file" \
            "${CHROOT}/usr/share/applications/$(basename "$desktop_file")"
    done

    # ── Wallpaper + GNOME defaults ─────────────────────────────────────
    install -Dm644 "${SCRIPT_DIR}/distro/icebreaker-wallpaper.png" \
        "${CHROOT}/usr/share/backgrounds/icebreaker-wallpaper.png"
    install -Dm644 "${SCRIPT_DIR}/distro/99_icebreaker.gschema.override" \
        "${CHROOT}/usr/share/glib-2.0/schemas/99_icebreaker.gschema.override"

    # ── XFCE defaults (vm profile) ────────────────────────────────────
    for xfconf_file in xfce4-desktop.xml xfce4-panel.xml xsettings.xml xfwm4.xml xfce4-keyboard-shortcuts.xml; do
        if [ -f "${SCRIPT_DIR}/distro/${xfconf_file}" ]; then
            install -Dm644 "${SCRIPT_DIR}/distro/${xfconf_file}" \
                "${CHROOT}/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/${xfconf_file}"
        fi
    done

    if [ "$PROFILE" = "vm" ]; then
        install -Dm755 "${SCRIPT_DIR}/distro/icebreaker-appearance-apply" "${CHROOT}/usr/libexec/icebreaker/appearance-apply"
        install -Dm644 "${SCRIPT_DIR}/distro/icebreaker-appearance.desktop" "${CHROOT}/etc/xdg/autostart/icebreaker-appearance.desktop"
        install -Dm644 "${SCRIPT_DIR}/distro/picom-frosted.conf" "${CHROOT}/etc/xdg/icebreaker/picom-frosted.conf"
        install -Dm644 "${SCRIPT_DIR}/distro/dunstrc" "${CHROOT}/etc/xdg/dunst/dunstrc"
        install -Dm644 "${SCRIPT_DIR}/distro/plank-settings" "${CHROOT}/etc/xdg/plank/dock1/settings"
        for dock_item in "${SCRIPT_DIR}"/distro/plank-dockitems/*.dockitem; do
            install -Dm644 "$dock_item" "${CHROOT}/etc/xdg/plank/dock1/launchers/$(basename "$dock_item")"
        done
        install -Dm644 "${SCRIPT_DIR}/distro/icebreaker-frosted-graphite-wallpaper.jpeg" "${CHROOT}/usr/share/backgrounds/icebreaker-frosted-graphite-wallpaper.jpeg"
        install -Dm644 "${SCRIPT_DIR}/vendor/MacTahoe-Dark.tar.xz" "${CHROOT}/tmp/MacTahoe-Dark.tar.xz"
        install -Dm644 "${SCRIPT_DIR}/vendor/MacTahoe-LICENSE" "${CHROOT}/usr/share/doc/icebreaker/third-party/MacTahoe-LICENSE"
    fi

    # Autostart of Icebreaker Terminal + Chatbot removed 2026-07-12 —
    # the user experience was: user boots the ISO for the first time,
    # BEFORE they've entered API keys or configured anything, and the
    # terminal auto-launched full-screen, obscuring the desktop and the
    # Control Center they need to open first. The correct entry points
    # are `icebreaker-control.desktop` (Control Center, launched from
    # app menu after user reads the welcome notification) and
    # `icebreaker-terminal.desktop` (launched on demand from the app
    # menu once the user has configured keys and is ready to use it).
    # First-boot self-test notification (Option B) tells the user
    # what to open first.

    # ── Build manifest ──────────────────────────────────────────────────
    install -Dm644 "${BUILD_DIR}/manifest.json" \
        "${CHROOT}/usr/share/icebreaker/build-manifest.json"

    # Count installed files.
    FILE_COUNT=$(find "${CHROOT}" -type f | wc -l | tr -d ' ')
    info "Chroot assembled: ${FILE_COUNT} files"
fi

# ── Stage 5: Build ISO ─────────────────────────────────────────────────
# Uses debootstrap + mksquashfs + xorriso directly.
# live-build (lb) 3.0 on Noble is broken: its grub and syslinux bootloader
# paths reference packages removed from Ubuntu years ago (grub-legacy,
# syslinux-themes-ubuntu-oneiric). Manual assembly avoids that entirely.

if [ "$SKIP_TO" -le 5 ] && [ "$STOP_AFTER" -ge 5 ]; then
    stage_banner 5 "Build ISO"

    cd "${SCRIPT_DIR}"
    UBUNTU_BASE=$(cat "${SCRIPT_DIR}/UBUNTU_BASE" | tr -d '[:space:]')
    ISO_WORK="${BUILD_DIR}/iso-work"
    ISO_CHROOT="${ISO_WORK}/chroot"
    ISO_STAGING="${ISO_WORK}/staging"
    ISO_FILE="${SCRIPT_DIR}/icebreaker.iso"

    rm -rf "${ISO_WORK}"
    mkdir -p "${ISO_CHROOT}" "${ISO_STAGING}/live" "${ISO_STAGING}/isolinux"

    # ── 5a: Debootstrap base system ────────────────────────────────────
    info "Debootstrapping ${UBUNTU_BASE}..."
    debootstrap --variant=minbase "${UBUNTU_BASE}" "${ISO_CHROOT}" \
        http://archive.ubuntu.com/ubuntu

    cat > "${ISO_CHROOT}/etc/apt/sources.list" <<SOURCES
deb http://archive.ubuntu.com/ubuntu ${UBUNTU_BASE} main restricted universe
deb http://archive.ubuntu.com/ubuntu ${UBUNTU_BASE}-updates main restricted universe
deb http://security.ubuntu.com/ubuntu ${UBUNTU_BASE}-security main restricted universe
SOURCES

    # ── 5b: Install kernel + live-boot inside chroot ───────────────────
    info "Installing kernel and live-boot packages..."
    mount --bind /dev  "${ISO_CHROOT}/dev"
    mount --bind /proc "${ISO_CHROOT}/proc"
    mount --bind /sys  "${ISO_CHROOT}/sys"
    mount -t devpts devpts "${ISO_CHROOT}/dev/pts"

    # ── Profile-specific desktop/DM setup ─────────────────────────────
    if [ "$PROFILE" = "desktop" ]; then
        _DESKTOP_PKGS="ubuntu-desktop ubuntu-standard libreoffice vlc gimp thunderbird gdm3 gnome-terminal gnome-text-editor nautilus"
    else
        _DESKTOP_PKGS="xfce4 xfce4-terminal lightdm lightdm-gtk-greeter thunar mousepad zenity"
    fi

    chroot "${ISO_CHROOT}" bash -c "
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq

        # Core system packages — NO --no-install-recommends so all recommended
        # deps are pulled in. This is a testing build; we want everything.
        apt-get install -y \
            linux-generic \
            live-boot \
            systemd-sysv \
            sudo bash coreutils python3 python3-venv python3-pip python3-full \
            curl wget ca-certificates \
            net-tools iproute2 iputils-ping iputils-tracepath \
            openssh-client openssh-server \
            less vim nano gedit \
            locales man-db manpages \
            dbus-x11 dbus-broker \
            bash-completion

        # X server — full install, not minimal
        apt-get install -y \
            xserver-xorg \
            xserver-xorg-core \
            xserver-xorg-video-all \
            xinit \
            x11-xserver-utils \
            x11-utils \
            x11-apps \
            spice-vdagent \
            qemu-guest-agent

        # Network management — full stack
        apt-get install -y \
            network-manager \
            network-manager-gnome \
            isc-dhcp-client \
            wireless-tools \
            wpasupplicant \
            nmap \
            netcat-openbsd \
            tcpdump \
            traceroute \
            dnsutils \
            whois

        # Desktop environment (profile-selected) — full with recommends
        apt-get install -y ${_DESKTOP_PKGS}

        # GTK4 + LibAdwaita + accessibility stack
        apt-get install -y \
            python3-gi \
            gir1.2-gtk-4.0 \
            gir1.2-adw-1 \
            libadwaita-1-0 \
            adwaita-icon-theme \
            at-spi2-core \
            libatk-bridge2.0-0 \
            libglib2.0-bin \
            dconf-cli \
            dconf-gsettings-backend

        if [ "${PROFILE}" = "vm" ]; then
            apt-get install -y rofi xfce4-whiskermenu-plugin dunst xfdashboard picom
        fi

        # Fonts — needed for GTK4/GNOME rendering
        apt-get install -y \
            fonts-dejavu \
            fonts-dejavu-core \
            fonts-dejavu-extra \
            fonts-liberation \
            fonts-noto-core \
            fonts-ubuntu \
            fontconfig

        # Mesa / GPU for GNOME rendering and compositing in QEMU/UTM
        # Note: libgl1-mesa-glx and libgles2-mesa were renamed in Ubuntu Noble
        apt-get install -y \
            mesa-vulkan-drivers \
            mesa-utils \
            libgl1-mesa-dri \
            libglx-mesa0 \
            libgles2

        # System debugging and testing tools (the 'bloat' that is actually useful)
        apt-get install -y \
            htop \
            btop \
            iotop \
            strace \
            ltrace \
            lsof \
            tree \
            jq \
            file \
            xxd \
            hexdump \
            pv \
            rsync \
            git \
            unzip \
            zip \
            tar \
            gzip \
            bzip2 \
            xz-utils \
            socat \
            screen \
            tmux \
            inxi \
            dmidecode \
            usbutils \
            pciutils \
            lshw \
            sysstat \
            acpi \
            bsdextrautils

        # Python tooling for running ib_debug.py and other scripts
        apt-get install -y \
            python3-rich \
            python3-requests \
            python3-toml \
            python3-psutil \
            python3-dbus \
            python3-gi-cairo \
            python3-seccomp

        # Polkit for D-Bus auth
        apt-get install -y \
            policykit-1 \
            polkitd

        # Log viewing
        apt-get install -y \
            lnav \
            multitail

        locale-gen en_US.UTF-8

        # Virtio kernel modules for QEMU/UTM emulated NICs and disk.
        cat >> /etc/initramfs-tools/modules << 'VIRTIO'
virtio
virtio_pci
virtio_net
virtio_blk
virtio_scsi
9p
9pnet_virtio
e1000
e1000e
VIRTIO
        update-initramfs -u -k all 2>&1 | tail -3
    "

    # ── Display manager enablement + auto-login ────────────────────────
    if [ "$PROFILE" = "desktop" ]; then
        chroot "${ISO_CHROOT}" bash -c "systemctl enable gdm || true"
        chroot "${ISO_CHROOT}" bash -c "systemctl enable NetworkManager || true"

        chroot "${ISO_CHROOT}" bash -c "
            groupadd -rf icebreaker-users
            useradd -m -s /bin/bash -G sudo,icebreaker-users icebreaker
            echo 'icebreaker:icebreaker' | chpasswd
            echo 'icebreaker ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/icebreaker
            # sudo refuses any sudoers file that is not mode 440 (world-readable = broken sudo).
            chmod 440 /etc/sudoers.d/icebreaker
            mkdir -p /etc/gdm3
            cat > /etc/gdm3/custom.conf <<'GDMCFG'
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=icebreaker
WaylandEnable=false
[security]
[xdmcp]
[chooser]
[debug]
GDMCFG
        "
    else
        chroot "${ISO_CHROOT}" bash -c "systemctl enable lightdm || true"
        chroot "${ISO_CHROOT}" bash -c "systemctl enable NetworkManager || true"

        chroot "${ISO_CHROOT}" bash -c "
            groupadd -rf autologin
            groupadd -rf icebreaker-users
            useradd -m -s /bin/bash -G sudo,autologin,icebreaker-users icebreaker
            echo 'icebreaker:icebreaker' | chpasswd
            echo 'icebreaker ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/icebreaker
            # sudo refuses any sudoers file that is not mode 440 (world-readable = broken sudo).
            chmod 440 /etc/sudoers.d/icebreaker
            mkdir -p /etc/lightdm/lightdm.conf.d
            cat > /etc/lightdm/lightdm.conf.d/50-autologin.conf <<'LDMCFG'
[Seat:*]
autologin-user=icebreaker
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
LDMCFG
        "
    fi

    if [ "$PROFILE" = "vm" ]; then
        # Plank Reloaded is pinned in vendor/; do not add its mutable PPA.
        install -Dm644 "${SCRIPT_DIR}/vendor/plank-reloaded_0.11.172_amd64.deb" "${ISO_CHROOT}/tmp/plank-reloaded_amd64.deb"
        install -Dm644 "${SCRIPT_DIR}/vendor/plank-reloaded_0.11.172_arm64.deb" "${ISO_CHROOT}/tmp/plank-reloaded_arm64.deb"
        chroot "${ISO_CHROOT}" bash -c '
            case "$(dpkg --print-architecture)" in
                amd64) dpkg -i /tmp/plank-reloaded_amd64.deb ;;
                arm64) dpkg -i /tmp/plank-reloaded_arm64.deb ;;
                *) echo "unsupported Plank architecture" >&2; exit 1 ;;
            esac
            apt-get -f install -y
            rm -f /tmp/plank-reloaded_amd64.deb /tmp/plank-reloaded_arm64.deb
        '
    fi

    chroot "${ISO_CHROOT}" bash -c "apt-get clean && rm -rf /var/lib/apt/lists/*"
    echo "icebreaker" > "${ISO_CHROOT}/etc/hostname"

    # ── 5c: Overlay Icebreaker artifacts from Stage 4 ──────────────────
    info "Overlaying Icebreaker artifacts..."
    [ -d "${CHROOT}" ] || die "includes.chroot not found — run Stage 4 first"
    cp -a "${CHROOT}"/* "${ISO_CHROOT}/"

    if [ "$PROFILE" = "vm" ]; then
        # Only consume the reviewed release archive; no upstream installer or
        # global GTK4/libadwaita override runs in the ISO build.
        chroot "${ISO_CHROOT}" bash -c '
            mkdir -p /usr/share/themes
            tar -xJf /tmp/MacTahoe-Dark.tar.xz -C /usr/share/themes
            rm -f /tmp/MacTahoe-Dark.tar.xz
        '
    fi

    # Wire # trigger into the primary user's .bashrc.
    # Must happen AFTER cp-a and AFTER useradd (which creates ~/.bashrc from skel).
    if [ -f "${ISO_CHROOT}/home/icebreaker/.bashrc" ]; then
        {
            echo ""
            echo "# Icebreaker # trigger — type \"# <intent>\" to run AI commands"
            echo "source /usr/share/icebreaker/shell/ib_trigger.bash"
        } >> "${ISO_CHROOT}/home/icebreaker/.bashrc"
        info "Wired # trigger into /home/icebreaker/.bashrc"
    fi

    # ── 5c2: Compile GSettings schemas (wallpaper override) ─────────────
    if [ -f "${ISO_CHROOT}/usr/share/glib-2.0/schemas/99_icebreaker.gschema.override" ]; then
        chroot "${ISO_CHROOT}" glib-compile-schemas /usr/share/glib-2.0/schemas/ 2>/dev/null || true
        info "GSettings schemas compiled (wallpaper override applied)"
    fi

    # ── 5c3: Enable Icebreaker systemd services ──────────────────────
    info "Enabling Icebreaker systemd services..."
    mkdir -p "${ISO_CHROOT}/etc/systemd/system/multi-user.target.wants"
    mkdir -p "${ISO_CHROOT}/etc/systemd/system/sockets.target.wants"
    # Enable Icebreaker systemd services (excluding icebreaker-qbd.service per selection constraint)
    for svc in icebreaker-first-boot.service icebreaker-pbd.service \
               icebreaker-controller.service; do
        ln -sf "/etc/systemd/system/${svc}" \
            "${ISO_CHROOT}/etc/systemd/system/multi-user.target.wants/${svc}"
    done
    # Systemd socket activation is disabled to prevent socket binding conflicts with the python controller daemon.
    # ln -sf /etc/systemd/system/icebreaker-controller.socket \
    #     "${ISO_CHROOT}/etc/systemd/system/sockets.target.wants/icebreaker-controller.socket"

    # ── 5d: Extract kernel + initrd ────────────────────────────────────
    info "Extracting kernel and initrd..."
    VMLINUZ=$(ls "${ISO_CHROOT}/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1)
    INITRD=$(ls "${ISO_CHROOT}/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1)
    [ -n "$VMLINUZ" ] || die "no vmlinuz found in chroot"
    [ -n "$INITRD" ]  || die "no initrd found in chroot"
    cp "$VMLINUZ" "${ISO_STAGING}/live/vmlinuz"
    cp "$INITRD"  "${ISO_STAGING}/live/initrd"
    info "Kernel: $(basename "$VMLINUZ")"

    # ── 5e: Unmount + create squashfs ──────────────────────────────────
    info "Unmounting chroot filesystems..."
    umount "${ISO_CHROOT}/dev/pts" 2>/dev/null || true
    umount "${ISO_CHROOT}/dev"     2>/dev/null || true
    umount "${ISO_CHROOT}/proc"    2>/dev/null || true
    umount "${ISO_CHROOT}/sys"     2>/dev/null || true

    info "Creating squashfs (this takes several minutes)..."
    mksquashfs "${ISO_CHROOT}" "${ISO_STAGING}/live/filesystem.squashfs" \
        -noI -noD -noF -noX -b 1M -no-duplicates \
        -e boot/vmlinuz-\* boot/initrd.img-\* \
        2>&1 | tail -5
    info "Squashfs: $(du -h "${ISO_STAGING}/live/filesystem.squashfs" | awk '{print $1}')"

    # ── 5f: Set up isolinux bootloader ─────────────────────────────────
    info "Setting up isolinux bootloader..."

    ISOLINUX_BIN=""
    for p in /usr/lib/ISOLINUX/isolinux.bin /usr/lib/syslinux/isolinux.bin; do
        [ -f "$p" ] && ISOLINUX_BIN="$p" && break
    done
    [ -n "$ISOLINUX_BIN" ] || die "isolinux.bin not found — install isolinux package"
    cp "$ISOLINUX_BIN" "${ISO_STAGING}/isolinux/"

    SYSLINUX_MOD=""
    for p in /usr/lib/syslinux/modules/bios /usr/lib/syslinux; do
        [ -f "${p}/ldlinux.c32" ] && SYSLINUX_MOD="$p" && break
    done
    [ -n "$SYSLINUX_MOD" ] || die "syslinux modules not found — install syslinux-common"
    for mod in ldlinux.c32 libcom32.c32 vesamenu.c32 libutil.c32; do
        [ -f "${SYSLINUX_MOD}/${mod}" ] && cp "${SYSLINUX_MOD}/${mod}" "${ISO_STAGING}/isolinux/"
    done

    ISOHDPFX=""
    for p in /usr/lib/ISOLINUX/isohdpfx.bin /usr/lib/syslinux/isohdpfx.bin \
             /usr/lib/syslinux/mbr/isohdpfx.bin; do
        [ -f "$p" ] && ISOHDPFX="$p" && break
    done
    [ -n "$ISOHDPFX" ] || die "isohdpfx.bin not found — install isolinux package"

    # Boot parameters differ by profile.
    if [ "$PROFILE" = "desktop" ]; then
        _LIVE_APPEND="boot=live toram quiet splash"
        _SAFE_APPEND="boot=live toram single nomodeset"
    else
        _LIVE_APPEND="boot=live nomodeset quiet splash"
        _SAFE_APPEND="boot=live nomodeset"
    fi

    cat > "${ISO_STAGING}/isolinux/isolinux.cfg" <<BOOTMENU
UI vesamenu.c32
PROMPT 0
TIMEOUT 50

MENU TITLE Icebreaker AI-Native OS

DEFAULT live

LABEL live
  MENU LABEL ^Icebreaker AI-Native OS (Live)
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_LIVE_APPEND}

LABEL live-safe
  MENU LABEL ^Safe Mode
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_SAFE_APPEND}
BOOTMENU

    # ── 5f2: Set up GRUB EFI bootloader ──────────────────────────────────
    # The isolinux setup above (5f) handles BIOS boot. This sub-stage adds
    # EFI boot support so the ISO works on UEFI firmware (VirtualBox on
    # macOS, OVMF/QEMU, post-2012 hardware). Both paths load the same
    # kernel with the same parameters.
    info "Setting up GRUB EFI bootloader..."

    command -v grub-mkstandalone >/dev/null 2>&1 || \
        die "grub-mkstandalone not found — install grub-efi-amd64-bin"

    GRUB_CFG_DIR="${ISO_WORK}/grub-embed"
    mkdir -p "${GRUB_CFG_DIR}"

    cat > "${GRUB_CFG_DIR}/grub.cfg" <<GRUBCFG
search --no-floppy --set=root --label ICEBREAKER

set default=0
set timeout=5

menuentry "Icebreaker AI-Native OS (Live)" {
    linux /live/vmlinuz ${_LIVE_APPEND}
    initrd /live/initrd
}

menuentry "Safe Mode" {
    linux /live/vmlinuz ${_SAFE_APPEND}
    initrd /live/initrd
}
GRUBCFG

    mkdir -p "${ISO_STAGING}/boot/grub"
    grub-mkstandalone \
        --format=x86_64-efi \
        --output="${ISO_STAGING}/boot/grub/BOOTX64.EFI" \
        --modules="part_gpt part_msdos fat iso9660 search search_label linux normal all_video test" \
        "boot/grub/grub.cfg=${GRUB_CFG_DIR}/grub.cfg"
    [ -f "${ISO_STAGING}/boot/grub/BOOTX64.EFI" ] || \
        die "grub-mkstandalone failed to produce BOOTX64.EFI"
    info "GRUB EFI binary: $(du -h "${ISO_STAGING}/boot/grub/BOOTX64.EFI" | awk '{print $1}')"

    EFI_IMG="${ISO_STAGING}/boot/grub/efi.img"
    GRUB_SIZE_KB=$(( $(stat -c%s "${ISO_STAGING}/boot/grub/BOOTX64.EFI" 2>/dev/null || stat -f%z "${ISO_STAGING}/boot/grub/BOOTX64.EFI") / 1024 ))
    EFI_IMG_SIZE_KB=$(( GRUB_SIZE_KB + 1024 ))  # GRUB binary + 1 MB headroom for FAT metadata

    dd if=/dev/zero of="${EFI_IMG}" bs=1K count="${EFI_IMG_SIZE_KB}" 2>/dev/null
    mkfs.fat "${EFI_IMG}" >/dev/null
    mmd -i "${EFI_IMG}" ::EFI
    mmd -i "${EFI_IMG}" ::EFI/BOOT
    mcopy -i "${EFI_IMG}" "${ISO_STAGING}/boot/grub/BOOTX64.EFI" ::EFI/BOOT/BOOTX64.EFI

    info "EFI image: $(du -h "${EFI_IMG}" | awk '{print $1}')"

    # ── 5g: Assemble ISO with xorriso ──────────────────────────────────
    info "Creating ISO image..."
    xorriso -as mkisofs \
        -iso-level 3 \
        -isohybrid-mbr "$ISOHDPFX" \
        -c isolinux/boot.cat \
        -b isolinux/isolinux.bin \
        -no-emul-boot \
        -boot-load-size 4 \
        -boot-info-table \
        -eltorito-alt-boot \
        -e boot/grub/efi.img \
        -no-emul-boot \
        -isohybrid-gpt-basdat \
        -V "ICEBREAKER" \
        -o "${ISO_FILE}" \
        "${ISO_STAGING}/" 2>&1 | tail -5

    [ -f "${ISO_FILE}" ] || die "ISO file not created"

    sha256sum "$ISO_FILE" > "${BUILD_DIR}/iso.sha256"
    ISO_SIZE=$(du -h "$ISO_FILE" | awk '{print $1}')
    ISO_HASH=$(awk '{print $1}' "${BUILD_DIR}/iso.sha256")

    info "ISO built: ${ISO_FILE} (${ISO_SIZE})"
    info "SHA-256: ${ISO_HASH}"

    # v6.9 (2026-07-16): the pre-v6.9 code hardcoded a Mac path
    # (/Users/aditya/…) and used `mv`. Inside Docker with --rm this
    # copied the ISO to the container's ephemeral overlay, then
    # destroyed both the source (via mv's unlink step across
    # filesystems) and the destination (via --rm) — losing the ISO
    # entirely. Fix: default the destination to a path inside the
    # bind-mounted REPO_ROOT so it survives container teardown, and
    # `cp` instead of `mv` so the build-side source stays too. Allows
    # env override via ISO_DEST for operators who need a different
    # target.
    ISO_DEST="${ISO_DEST:-${REPO_ROOT}/ISO/icebreaker_full_ubuntu.iso}"
    mkdir -p "$(dirname "$ISO_DEST")"
    cp "$ISO_FILE" "$ISO_DEST"
    info "Final ISO copied to ${ISO_DEST}"
    info "Build complete."
fi
