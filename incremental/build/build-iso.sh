#!/usr/bin/env bash
# build-iso.sh — per-version ISO builder for the Icebreaker incremental rebuild.
#
# Usage (on the Linux build VM, as root):
#   sudo bash incremental/build/build-iso.sh <N> [--no-compress]
#
# Steps:
#   1. Untar cached base (build-base.sh must have run once)
#   2. Apply version manifests v1..vN CUMULATIVELY (each is a delta)
#   3. Run smoke-gate.sh — build ABORTS if any check fails (never ship a broken tree)
#   4. squashfs (zstd) + isolinux BIOS + GRUB EFI + xorriso hybrid ISO
#
# Output: incremental/.build/out/icebreaker-v<N>.iso  (+ .sha256)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INC_ROOT}/.." && pwd)"
BUILD_DIR="${INC_ROOT}/.build"
PKG_FILE="${SCRIPT_DIR}/packages-desktop.txt"
UBUNTU_BASE="noble"

info() { echo -e "\033[0;32m[$(date +%H:%M:%S)]\033[0m $*"; }
die()  { echo -e "\033[0;31mFATAL:\033[0m $*" >&2; exit 1; }

# ── Args ────────────────────────────────────────────────────────────────
[ $# -ge 1 ] || die "usage: build-iso.sh <version-number 0-8> [--no-compress] [--label vX.Y] [--arch amd64|arm64]"
VN="$1"; shift
[[ "$VN" =~ ^[0-8]$ ]] || die "version must be 0-8, got: $VN"
COMPRESS_ARGS=(-comp zstd -Xcompression-level 3)
LABEL=""
# V6.6: --arch defaults to amd64 for backwards compat with V0-V6.51.
ARCH="${ARCH:-amd64}"
# v6.13_OC Fix L' Commit 1: --edition selects which content lands in
# the chroot. current (default) = Textual TUI + Gemini. oc = opencode
# TUI + qb_oc.json + no Textual. Also drives the _OC filename suffix.
EDITION="${EDITION:-current}"
while [ $# -gt 0 ]; do
    case "$1" in
        --no-compress) COMPRESS_ARGS=(-noI -noD -noF -noX); shift ;;  # D-1 escape hatch
        --label)       LABEL="$2"; shift 2 ;;  # override version marker + ISO filename (e.g. "v6.1")
        --arch)        ARCH="$2"; shift 2 ;;   # V6.6: target CPU arch (amd64 or arm64)
        --edition)     EDITION="$2"; shift 2 ;; # v6.13_OC: current|oc
        *) die "unknown arg: $1" ;;
    esac
done
case "$EDITION" in
    current|oc) ;;
    *) die "--edition must be current|oc, got: $EDITION" ;;
esac
# LABEL defaults to plain vN when not overridden (backwards-compatible).
[ -z "$LABEL" ] && LABEL="v${VN}"
# Widened 2026-07-20 (v6.10b build): accept revision suffixes so patch
# ISOs on top of a shipped release don't need a spurious minor bump.
# Grammar: v<N>[.<M>[.<P>]][<letter>] — v6, v6.10, v6.10.1, v6.10b.
[[ "$LABEL" =~ ^v[0-9]+(\.[0-9]+){0,2}[a-z]?$ ]] || die "--label must match ^v[0-9]+(\.[0-9]+){0,2}[a-z]?$, got: $LABEL"

# V6.6: source arch config (ARCH, GRUB_FORMAT, EFI_BOOT_NAME, APT_MIRROR,
# LLAMA_BUILD_DIR, BOOT_MODE, etc.). Every downstream reference to those
# variables assumes this happened.
CONF="${REPO_ROOT}/config/archs/${ARCH}.conf"
[ -f "$CONF" ] || die "unknown arch '$ARCH' — expected config at $CONF (only amd64, arm64 defined)"
# shellcheck disable=SC1090
source "$CONF"

[ "$(id -u)" = "0" ] || die "must run as root (sudo)"
# V6.6: mkfs.fat, mcopy still needed for GRUB EFI (both arches).
# isolinux tooling only required for BIOS (hybrid mode = amd64 default).
_REQUIRED_TOOLS=(mksquashfs xorriso zstd grub-mkstandalone mkfs.fat mcopy)
for tool in "${_REQUIRED_TOOLS[@]}"; do
    command -v "$tool" >/dev/null || die "$tool not installed"
done

# V6.6 F-37: verify the arch-specific GRUB module dir exists BEFORE running
# the 90-min manifest chain. arm64-efi lives in grub-efi-arm64-bin, which is
# only in the arm64 package pool on Ubuntu; requires `dpkg --add-architecture
# arm64` + ports.ubuntu.com source + `apt install grub-efi-arm64-bin:arm64`.
[ -f "/usr/lib/grub/${GRUB_FORMAT}/modinfo.sh" ] || die \
    "GRUB target dir /usr/lib/grub/${GRUB_FORMAT}/ missing — install grub-efi-${ARCH}-bin (for cross-arch: dpkg --add-architecture ${ARCH} + apt install grub-efi-${ARCH}-bin:${ARCH})"

# Fail fast, not at xorriso 15 minutes in (F-15): chroot ~6G + squashfs ~2.5G + ISO ~2.5G.
FREE_GB=$(df -BG --output=avail "$INC_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)
[ "${FREE_GB:-0}" -ge 12 ] || die "only ${FREE_GB}G free under ${BUILD_DIR} — need ≥12G. Clean old chroots/ISOs first (see GROUND_TRUTH F-15)."

# ── R8 / F-33: mcpd seccomp harvest gate — MUST PASS before any cloud cost ──
# Runs the mcpd binary against a scratch $HOME with the real seccomp filter
# AND with MCPD_SECCOMP_LOG_ONLY=1, exercising every documented tool. If any
# syscall is outside the allowlist or mcpd dies with SIGSYS, the build aborts
# before we spend 15 min of ISO packaging + 15 min of QEMU gate time.
#
# For V ≥ 6 (mcpd shipped in the ISO) this is mandatory. Below V6 mcpd isn't
# installed so the gate is a no-op — check binary presence first.
# V6.6: prefer per-arch mcpd binary; fall back to legacy unsuffixed path
# for V0-V6.51 rebuilds when no arch suffix was ever emitted.
HARVEST_MCPD_ARCH="${REPO_ROOT}/cx-distro/.build/mcpd-${ARCH}"
HARVEST_MCPD_LEGACY="${REPO_ROOT}/cx-distro/.build/mcpd"
HARVEST_MCPD=""
if [ -x "${HARVEST_MCPD_ARCH}" ]; then
    HARVEST_MCPD="${HARVEST_MCPD_ARCH}"
elif [ -x "${HARVEST_MCPD_LEGACY}" ] && [ "$ARCH" = "amd64" ]; then
    HARVEST_MCPD="${HARVEST_MCPD_LEGACY}"
fi

# V6.6: seccomp harvest only runs when host arch == target arch. Cross-arch
# runs (amd64 host → arm64 mcpd binary) can't exercise the real filter
# because the seccomp filter enforces on the CPU running the binary; qemu-user
# translates syscall numbers, breaking the discovery loop. The qemu-gate L6
# assertions exercise the real filter inside the booted arm64 VM instead.
HOST_ARCH="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
if [ "${VN}" -ge 6 ] && [ -n "${HARVEST_MCPD}" ] && [ "$ARCH" = "$HOST_ARCH" ]; then
    info "Running mcpd seccomp harvest gate (F-33, R8) with ${HARVEST_MCPD}..."
    bash "${SCRIPT_DIR}/mcpd-harvest.sh" --mcpd "${HARVEST_MCPD}" || \
        die "HARVEST GATE FAILED — refusing to ship an ISO with a broken mcpd syscall surface (F-33 / R8). Fix the allowlist in src/mcpd/src/sandbox/seccomp.rs, rebuild mcpd, and retry."
elif [ "${VN}" -ge 6 ] && [ "$ARCH" != "$HOST_ARCH" ]; then
    info "Cross-arch build (host=${HOST_ARCH} → target=${ARCH}) — harvest gate deferred to qemu-gate L6 (seccomp can't be exercised through qemu-user translation)"
elif [ "${VN}" -ge 6 ]; then
    info "WARN: V${VN} expects mcpd at ${HARVEST_MCPD_ARCH} (or legacy ${HARVEST_MCPD_LEGACY}) but binary missing — harvest gate skipped (build will fail later in v6.manifest overlay)"
fi

# ── R9 / F-35: golden intent corpus (offline mode) — pre-build regression guard ──
# Runs the corpus tests against the REPO source (not the ISO/venv) — cheap
# regression check for controller/main.py _SUPPORTED_ACTIONS drift, corpus
# additions, or F-35 short-circuit edits. If the corpus fails, we know the
# Controller will silently accept phantom actions — refuse to ship.
# Runs for V ≥ 5 (Controller ships from V2, but the guard code lives from V6.5).
CORPUS_TEST="${REPO_ROOT}/dual-brain/controller/tests/test_intent_corpus.py"
if [ "${VN}" -ge 5 ] && [ -f "${CORPUS_TEST}" ]; then
    info "Running F-35 golden intent corpus (offline)..."
    if command -v pytest >/dev/null 2>&1; then
        (cd "${REPO_ROOT}/dual-brain" && pytest controller/tests/test_intent_corpus.py -q --no-header) || \
            die "CORPUS GATE FAILED — refusing to ship an ISO with F-35 intent regressions (R9). Check controller/main.py _SUPPORTED_ACTIONS or corpus/intent_corpus.json edits."
    else
        info "WARN: pytest not on VM PATH — corpus gate skipped (install pytest to enable)"
    fi
fi

# ── Locate cached base ──────────────────────────────────────────────────
PKG_LIST="$(grep -vE '^\s*(#|$)' "$PKG_FILE")"
BASE_HASH="$(printf '%s\n%s' "$PKG_LIST" "$UBUNTU_BASE" | sha256sum | cut -c1-12)"
# V6.6: per-arch base cache. Fall back to legacy unsuffixed name if it
# exists AND arch is amd64 — preserves ability to rebuild V0-V6.51 from
# an old base tarball without re-running the 90-min debootstrap.
BASE_TAR_ARCH="${BUILD_DIR}/base-desktop-${BASE_HASH}-${ARCH}.tar.zst"
BASE_TAR_LEGACY="${BUILD_DIR}/base-desktop-${BASE_HASH}.tar.zst"
if [ -f "$BASE_TAR_ARCH" ]; then
    BASE_TAR="$BASE_TAR_ARCH"
elif [ -f "$BASE_TAR_LEGACY" ] && [ "$ARCH" = "amd64" ]; then
    BASE_TAR="$BASE_TAR_LEGACY"
    info "Using legacy unsuffixed base cache: ${BASE_TAR}"
else
    die "no cached base for arch=${ARCH} + package list (hash ${BASE_HASH}).
Run: sudo ARCH=${ARCH} bash incremental/build/build-base.sh"
fi

# ── Fresh chroot from base ──────────────────────────────────────────────
ISO_WORK="${BUILD_DIR}/iso-work"
CHROOT="${ISO_WORK}/chroot"
STAGING="${ISO_WORK}/staging"
OUT_DIR="${BUILD_DIR}/out"
# V6.6+: ISO output name gets arch suffix. For older labels (V0-V6.51
# rebuilds) keep the unsuffixed historical name when ARCH is the default
# amd64 — matches what those ISOs shipped as. Naming convention (revised
# 2026-07-09): no `icebreaker-` prefix (the file lives in the Icebreaker
# repo; the prefix was tautological). Historic ISOs like `v6.51.iso` and
# `v0.iso` predate the multi-arch refactor and stay unsuffixed.
# Scope I (2026-07-12): v6.7+ ALWAYS uses arch suffix. Prior regex
# `^v[7-9]` was v7+ only — v6.7 fell through to unsuffixed amd64,
# breaking the multi-arch naming convention. `v6.7` matches
# `^v6\.[6-9]|^v[7-9]` which covers v6.6, v6.7...v6.9 AND v7+.
# v6.10 rebuild-round-2 fix (2026-07-19): `[6-9]` is a single-digit
# character class — v6.10 fell through to unsuffixed amd64 again,
# rebuild-v67.sh Step 5 then FATAL'd on missing `v6.10-amd64.iso`.
# Widened the second alternation to cover `v6.<multi-digit-minor>`
# (v6.10, v6.11, v6.99...) so all v6.6+ get the arch suffix.
# v6.10b build fix (2026-07-20): `^v6\.[1-9][0-9]+$` didn't match
# `v6.10b` (trailing letter suffix). Widened both v6 alternations to
# accept an optional `[a-z]?` suffix so revision ISOs (v6.10b, v6.10c...)
# get the arch suffix too.
# v6.13_OC Fix L' Commit 1: EDITION=oc appends _OC before the arch suffix
# so both editions can coexist in out/ (`v6.13-arm64.iso` +
# `v6.13_OC-arm64.iso`). The suffix is empty for the current edition.
EDITION_SUFFIX=""
[ "$EDITION" = "oc" ] && EDITION_SUFFIX="_OC"
if [[ "$LABEL" =~ ^v6\.[6-9][a-z]?$ ]] || [[ "$LABEL" =~ ^v6\.[1-9][0-9]+[a-z]?$ ]] || [[ "$LABEL" =~ ^v[7-9] ]] || [ "$ARCH" != "amd64" ]; then
    ISO_FILE="${OUT_DIR}/${LABEL}${EDITION_SUFFIX}-${ARCH}.iso"
else
    ISO_FILE="${OUT_DIR}/${LABEL}${EDITION_SUFFIX}.iso"
fi

cleanup() {
    umount "${CHROOT}/dev/pts" 2>/dev/null || true
    umount "${CHROOT}/dev"     2>/dev/null || true
    umount "${CHROOT}/proc"    2>/dev/null || true
    umount "${CHROOT}/sys"     2>/dev/null || true
}
trap cleanup EXIT

info "Extracting base ${BASE_HASH} (v${VN} build)..."
rm -rf "$ISO_WORK"
mkdir -p "$CHROOT" "${STAGING}/live" "${STAGING}/isolinux" "$OUT_DIR"
zstd -dc "$BASE_TAR" | tar -C "$CHROOT" -xf -

mount --bind /dev  "${CHROOT}/dev"
mount --bind /proc "${CHROOT}/proc"
mount --bind /sys  "${CHROOT}/sys"
mount -t devpts devpts "${CHROOT}/dev/pts"

# ── Apply manifests v1..vN cumulatively ────────────────────────────────
# Each manifest defines: VERSION_PACKAGES (optional) and version_overlay().
# v0 is the bare base — no manifest work.
for (( i=1; i<=VN; i++ )); do
    MANIFEST="${INC_ROOT}/versions/v${i}.manifest"
    [ -f "$MANIFEST" ] || die "manifest missing: $MANIFEST"
    info "── Applying v${i} manifest ──"
    VERSION_PACKAGES=""
    # shellcheck disable=SC1090
    source "$MANIFEST"
    if [ -n "${VERSION_PACKAGES}" ]; then
        info "Installing v${i} packages: ${VERSION_PACKAGES}"
        chroot "$CHROOT" bash -c "
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            apt-get install -y ${VERSION_PACKAGES}
            apt-get clean && rm -rf /var/lib/apt/lists/*
        " < /dev/null
    fi
    version_overlay "$CHROOT" "$REPO_ROOT"
    unset -f version_overlay
done

# ── v6.13_OC Fix L' Commit 2/5: edition overlay ─────────────────────────
# Applied AFTER v1..vN cumulative manifests. Each commit in the L' series
# adds one edition-specific piece:
#   Commit 2: TUI removal + .desktop swap
#   Commit 3: opencode npm install
#   Commit 4: qb_oc.json generation + launcher/wrapper install
#   Commit 5: controller.toml OC-edition override
# EDITION=current is a no-op — the chroot after v1..vN is exactly the
# byte-for-byte v6.12 shape it was before this file existed.
if [ "$EDITION" = "oc" ]; then
    info "── Applying OC edition overlay ──"

    # 2.a — remove Textual TUI from venv site-packages (~8k LOC deleted).
    # OC edition uses opencode's native TUI (installed in Commit 3); the
    # Icebreaker Textual terminal is inapplicable + confusing to ship.
    _TERM_SP="$(find "${CHROOT}/opt/icebreaker/venv/lib" -maxdepth 4 -type d -name terminal 2>/dev/null | head -1)"
    if [ -n "$_TERM_SP" ]; then
        info "[oc] removing Textual TUI from venv: ${_TERM_SP#${CHROOT}}"
        rm -rf "$_TERM_SP"
    else
        info "[oc] Textual TUI not found in venv (already removed?)"
    fi

    # 2.b — swap the AI Terminal .desktop entry. Current-edition v3.manifest
    # installed icebreaker-terminal.desktop → the Textual launcher. Replace
    # with icebreaker-ai-terminal.desktop → the opencode wrapper (Fix P).
    # Other .desktop files (control, chatbot, settings, audit) stay in
    # both editions — they call the Python daemon which stays alive in OC.
    rm -f "${CHROOT}/usr/share/applications/icebreaker-terminal.desktop"
    install -Dm644 "${REPO_ROOT}/cx-distro/distro/icebreaker-ai-terminal.desktop" \
        "${CHROOT}/usr/share/applications/icebreaker-ai-terminal.desktop"
    info "[oc] AI Terminal launcher → icebreaker-ai-terminal.desktop (opencode)"

    # 3 — opencode + Node.js install (v6.13_OC Fix L' Commit 3/5).
    # opencode ships as an npm package (@opencode-ai). We install it
    # globally inside the chroot so /usr/bin/opencode is available to
    # the icebreaker-oc launcher (Fix P).
    #
    # Pinned to 1.18.4 — the exact version verified end-to-end in the
    # D-R2-3 preflight (opencode mcp list showed ✓ icebreaker connected).
    # Bumping this version requires a full preflight re-run — see
    # docs/v6.x_OC/PREFLIGHT_2026-07-24.md.
    _OC_OPENCODE_VERSION="1.18.4"
    info "[oc] installing nodejs + npm + opencode-ai@${_OC_OPENCODE_VERSION}..."
    chroot "$CHROOT" bash -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y --no-install-recommends nodejs npm ca-certificates
        # Suppress npm funding/audit noise inside the chroot — release-time
        # builds don't need these.
        npm config set fund false --global
        npm config set audit false --global
        npm install -g opencode-ai@${_OC_OPENCODE_VERSION} 2>&1 | tail -5
        apt-get clean && rm -rf /var/lib/apt/lists/*
    " < /dev/null
    # Sanity: opencode binary must land at /usr/bin/opencode (npm's global
    # bin path inside the chroot). If npm's global prefix differs, symlink.
    if [ ! -x "${CHROOT}/usr/bin/opencode" ]; then
        _OC_BIN="$(chroot "$CHROOT" bash -c 'command -v opencode || true')"
        [ -n "$_OC_BIN" ] || die "opencode binary not found in chroot after npm install"
        info "[oc] symlinking opencode → /usr/bin/opencode (was at ${_OC_BIN})"
        ln -sf "$_OC_BIN" "${CHROOT}/usr/bin/opencode"
    fi
    _OC_VER="$(chroot "$CHROOT" opencode --version 2>&1 | tail -1)"
    info "[oc] opencode installed: ${_OC_VER}"

    # 4 — qb_oc.json + icebreaker-oc launcher + mcpd-for-oc.sh wrapper
    # (v6.13_OC Fix L' Commit 4/5). All source files were created in Fix P
    # (commit 9989132). This commit wires them into the build.
    #
    # gen-oc-config.sh boots the just-built mcpd + calls tools/list to
    # generate the permission map (Tier ≤ 1 → allow; writes → ask). We
    # always use the amd64 mcpd binary for tools/list because the tool
    # set is arch-independent (compile-time embedded schemas) — avoids
    # needing qemu-user-static to run mcpd-arm64 on an amd64 build host.
    _OC_MCPD_HOST="${REPO_ROOT}/cx-distro/.build/mcpd-amd64"
    [ -x "$_OC_MCPD_HOST" ] || _OC_MCPD_HOST="${REPO_ROOT}/cx-distro/.build/mcpd"
    [ -x "$_OC_MCPD_HOST" ] || die "gen-oc-config: cannot find host mcpd at ${REPO_ROOT}/cx-distro/.build/mcpd(-amd64)"

    info "[oc] installing icebreaker-oc launcher + mcpd-for-oc.sh wrapper..."
    install -Dm755 "${REPO_ROOT}/cx-distro/distro/icebreaker-oc" \
        "${CHROOT}/usr/bin/icebreaker-oc"
    install -Dm755 "${REPO_ROOT}/cx-distro/distro/mcpd-for-oc.sh" \
        "${CHROOT}/usr/libexec/icebreaker/mcpd-for-oc.sh"

    info "[oc] generating qb_oc.json from mcpd tools/list..."
    MCPD_BIN="$_OC_MCPD_HOST" \
        TEMPLATE="${REPO_ROOT}/cx-distro/distro/qb_oc.json.template" \
        OUTPUT="${CHROOT}/etc/icebreaker/qb_oc.json" \
        bash "${INC_ROOT}/build/gen-oc-config.sh" \
        || die "gen-oc-config.sh failed — see stderr above"
    chmod 644 "${CHROOT}/etc/icebreaker/qb_oc.json"

    # 5 — controller.toml OC override (v6.13_OC Fix L' Commit 5/5).
    # v2.manifest installed cx-distro/distro/controller.toml with
    # backend = "gemini" as the default. On OC ISOs we flip the
    # backend to "opencode_oc" so the daemon boots straight into OC
    # mode (starts oc_audit_bridge per Fix Q, skips OpencodeBackend
    # prewarm, NoOpBrainBackend registered but never called).
    #
    # We do a targeted sed rather than shipping a duplicate
    # controller.oc.toml because the file has ~15 sections (hitl, cost,
    # limits, agent_graph, ...) and drift between the two copies would
    # break subtly. Only the [qb] section differs by edition.
    info "[oc] rewriting controller.toml [qb] block for opencode_oc backend..."
    _OC_CTL_TOML="${CHROOT}/etc/icebreaker/controller.toml"
    [ -f "$_OC_CTL_TOML" ] || die "[oc] controller.toml missing — v2.manifest didn't install it?"
    # Python is portable (GNU sed + BSD sed differ on `0,/pat/` range
    # anchoring — Python's re works identically everywhere). Rewrites
    # the FIRST `backend = "..."` line (which is inside the [qb] block
    # before any [qb.gemini] sub-header) and appends the OC section
    # if not already present.
    python3 - "$_OC_CTL_TOML" <<'OC_TOML_PY'
import re, sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

# 1. Flip the primary backend = "..." (first occurrence only — must be
# inside the [qb] top-level block, before any [qb.<name>] sub-header).
new_content, n = re.subn(
    r'^backend\s*=\s*"[^"]*"',
    'backend = "opencode_oc"',
    content,
    count=1,
    flags=re.MULTILINE,
)
if n != 1:
    raise SystemExit(f"controller.toml: expected 1 backend= line, replaced {n}")

# 2. Append [qb.opencode_oc] if absent.
if not re.search(r'^\[qb\.opencode_oc\]', new_content, re.MULTILINE):
    new_content += (
        "\n# v6.13_OC Fix L' Commit 5: OC-edition daemon config.\n"
        "# Populated at build time via incremental/build/build-iso.sh.\n"
        "# See Fix Q's OpencodeOcConfig dataclass for field semantics.\n"
        "# All fields have sensible defaults — this empty section is\n"
        "# enough to make _build_opencode_oc_config return a config\n"
        "# object rather than None (Fix Q gate).\n"
        "[qb.opencode_oc]\n"
    )

with open(path, "w") as f:
    f.write(new_content)
OC_TOML_PY
    # Sanity: verify the backend flip landed + section present.
    grep -q '^backend\s*=\s*"opencode_oc"' "$_OC_CTL_TOML" || \
        die "[oc] controller.toml backend rewrite failed — check sed pattern"
    grep -q '^\[qb\.opencode_oc\]' "$_OC_CTL_TOML" || \
        die "[oc] controller.toml [qb.opencode_oc] section missing after append"
    info "[oc] controller.toml: backend=opencode_oc, [qb.opencode_oc] present"

    info "── OC edition overlay: all 4 content commits done (2/5..5/5) ──"
fi

echo "${LABEL}" > "${CHROOT}/etc/icebreaker-version"

# ── Smoke gate — build aborts on failure ────────────────────────────────
info "Running smoke gate (level ${VN})..."
bash "${SCRIPT_DIR}/smoke-gate.sh" "$CHROOT" "$VN" || \
    die "SMOKE GATE FAILED — not producing an ISO from a broken tree (R6). Fix and rebuild."

# ── Kernel + initrd ─────────────────────────────────────────────────────
VMLINUZ=$(ls "${CHROOT}/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1)
INITRD=$(ls "${CHROOT}/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1)
[ -n "$VMLINUZ" ] || die "no vmlinuz in chroot"
[ -n "$INITRD" ]  || die "no initrd in chroot"
# V6.6 F-38: Ubuntu ships arm64 vmlinuz as gzip-compressed. GRUB's arm64-efi
# `linux` loader needs a plain arm64 Image (starts with `MZ` EFI magic, has
# EFI stub header) — fails otherwise with "plain image kernel not supported
# - rebuild with CONFIG_(U)EFI_STUB enabled". amd64 bzImage GRUB handles
# either way. Decompress on arm64 to be safe.
if [ "$ARCH" = "arm64" ] && file "$VMLINUZ" 2>/dev/null | grep -q "gzip compressed"; then
    info "arm64: decompressing gzip'd vmlinuz for GRUB EFI stub loader..."
    zcat "$VMLINUZ" > "${STAGING}/live/vmlinuz"
    file "${STAGING}/live/vmlinuz" 2>/dev/null | grep -q "ARM64.*Image" || \
        die "F-38: decompressed vmlinuz is not an arm64 Image — GRUB will fail to boot"
else
    cp "$VMLINUZ" "${STAGING}/live/vmlinuz"
fi
cp "$INITRD"  "${STAGING}/live/initrd"
info "Kernel: $(basename "$VMLINUZ")"

# ── Squashfs ────────────────────────────────────────────────────────────
cleanup
trap - EXIT
info "Creating squashfs (${COMPRESS_ARGS[0]} ${COMPRESS_ARGS[1]:-})..."
mksquashfs "$CHROOT" "${STAGING}/live/filesystem.squashfs" \
    "${COMPRESS_ARGS[@]}" -b 1M -no-duplicates \
    -e boot/vmlinuz-\* boot/initrd.img-\* \
    2>&1 | tail -3
info "Squashfs: $(du -h "${STAGING}/live/filesystem.squashfs" | awk '{print $1}')"

_LIVE_APPEND="boot=live toram quiet splash"
_SAFE_APPEND="boot=live toram single nomodeset"

# V6.6: isolinux is BIOS-only. amd64 uses hybrid (BIOS+UEFI); arm64 has
# no BIOS concept — UEFI only. Skip the entire block on arm64.
if [ "$BOOT_MODE" = "hybrid" ]; then
    # ── isolinux (BIOS) ─────────────────────────────────────────────────
    ISOLINUX_BIN=""
    for p in /usr/lib/ISOLINUX/isolinux.bin /usr/lib/syslinux/isolinux.bin; do
        [ -f "$p" ] && ISOLINUX_BIN="$p" && break
    done
    [ -n "$ISOLINUX_BIN" ] || die "isolinux.bin not found — apt-get install isolinux"
    cp "$ISOLINUX_BIN" "${STAGING}/isolinux/"

    SYSLINUX_MOD=""
    for p in /usr/lib/syslinux/modules/bios /usr/lib/syslinux; do
        [ -f "${p}/ldlinux.c32" ] && SYSLINUX_MOD="$p" && break
    done
    [ -n "$SYSLINUX_MOD" ] || die "syslinux modules not found — apt-get install syslinux-common"
    for mod in ldlinux.c32 libcom32.c32 vesamenu.c32 libutil.c32; do
        [ -f "${SYSLINUX_MOD}/${mod}" ] && cp "${SYSLINUX_MOD}/${mod}" "${STAGING}/isolinux/"
    done

    ISOHDPFX=""
    for p in /usr/lib/ISOLINUX/isohdpfx.bin /usr/lib/syslinux/isohdpfx.bin \
             /usr/lib/syslinux/mbr/isohdpfx.bin; do
        [ -f "$p" ] && ISOHDPFX="$p" && break
    done
    [ -n "$ISOHDPFX" ] || die "isohdpfx.bin not found"

    cat > "${STAGING}/isolinux/isolinux.cfg" <<BOOTMENU
UI vesamenu.c32
PROMPT 0
TIMEOUT 50

MENU TITLE Icebreaker Incremental v${VN}

DEFAULT live

LABEL live
  MENU LABEL ^Icebreaker v${VN} (Live)
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_LIVE_APPEND}

LABEL live-safe
  MENU LABEL ^Safe Mode
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd ${_SAFE_APPEND}
BOOTMENU
else
    info "arm64 UEFI-only build — skipping isolinux (BIOS boot)"
    # Clean up the empty isolinux staging dir so xorriso doesn't add it.
    rm -rf "${STAGING}/isolinux"
fi

# ── GRUB EFI ────────────────────────────────────────────────────────────
info "Building GRUB EFI..."
GRUB_CFG_DIR="${ISO_WORK}/grub-embed"
mkdir -p "$GRUB_CFG_DIR" "${STAGING}/boot/grub"

cat > "${GRUB_CFG_DIR}/grub.cfg" <<GRUBCFG
search --no-floppy --set=root --label ICEBREAKER

set default=0
set timeout=5

menuentry "Icebreaker v${VN} (Live)" {
    linux /live/vmlinuz ${_LIVE_APPEND}
    initrd /live/initrd
}

menuentry "Safe Mode" {
    linux /live/vmlinuz ${_SAFE_APPEND}
    initrd /live/initrd
}
GRUBCFG

# V6.6: GRUB EFI format is arch-specific. GRUB_FORMAT and EFI_BOOT_NAME
# come from config/archs/${ARCH}.conf.
grub-mkstandalone \
    --format="${GRUB_FORMAT}" \
    --output="${STAGING}/boot/grub/${EFI_BOOT_NAME}" \
    --modules="part_gpt part_msdos fat iso9660 search search_label linux normal all_video test" \
    "boot/grub/grub.cfg=${GRUB_CFG_DIR}/grub.cfg"
[ -f "${STAGING}/boot/grub/${EFI_BOOT_NAME}" ] || die "grub-mkstandalone failed for ${GRUB_FORMAT}"

EFI_IMG="${STAGING}/boot/grub/efi.img"
GRUB_SIZE_KB=$(( $(stat -c%s "${STAGING}/boot/grub/${EFI_BOOT_NAME}") / 1024 ))
dd if=/dev/zero of="$EFI_IMG" bs=1K count=$(( GRUB_SIZE_KB + 1024 )) 2>/dev/null
mkfs.fat "$EFI_IMG" >/dev/null
mmd -i "$EFI_IMG" ::EFI
mmd -i "$EFI_IMG" ::EFI/BOOT
mcopy -i "$EFI_IMG" "${STAGING}/boot/grub/${EFI_BOOT_NAME}" "::EFI/BOOT/${EFI_BOOT_NAME}"

# ── xorriso ─────────────────────────────────────────────────────────────
# V6.6: amd64 = hybrid (BIOS+UEFI); arm64 = UEFI-only. The isolinux
# arguments only make sense in hybrid mode.
if [ "$BOOT_MODE" = "hybrid" ]; then
    info "Assembling hybrid ISO (BIOS + UEFI)..."
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
        -o "$ISO_FILE" \
        "${STAGING}/" 2>&1 | tail -3
else
    info "Assembling UEFI-only ISO (arm64)..."
    xorriso -as mkisofs \
        -iso-level 3 \
        -e boot/grub/efi.img \
        -no-emul-boot \
        -isohybrid-gpt-basdat \
        -V "ICEBREAKER" \
        -o "$ISO_FILE" \
        "${STAGING}/" 2>&1 | tail -3
fi

[ -f "$ISO_FILE" ] || die "ISO not created"
sha256sum "$ISO_FILE" > "${ISO_FILE}.sha256"

info "════════════════════════════════════════════════"
info "ISO: ${ISO_FILE} ($(du -h "$ISO_FILE" | awk '{print $1}'))"
info "SHA: $(awk '{print $1}' "${ISO_FILE}.sha256")"
info "Arch: ${ARCH}  Boot mode: ${BOOT_MODE}"
info "Next: ARCH=${ARCH} bash incremental/tests/qemu-gate.sh ${ISO_FILE} ${VN} ${LABEL}"
info "════════════════════════════════════════════════"
