#!/usr/bin/env bash
# ci.sh — Phase 6 ISO distribution gate verification (G12–G15).
#
# Run from the repo root:
#   bash cx-distro/ci.sh
#
# Gates skip cleanly when prerequisites are absent (macOS dev, no models).
# Exit code 0 = all gates passed, 1 = one or more gates failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CX_DIR="${SCRIPT_DIR}"
REPO_ROOT="$(cd "${CX_DIR}/.." && pwd)"
CHROOT="${CX_DIR}/config/includes.chroot"
SYSTEMD_DIR="${REPO_ROOT}/dual-brain/controller/systemd"

pass()  { printf "  \033[32m✓\033[0m G%-2s %s\n" "$1" "$2"; }
warn()  { printf "  \033[33m⚠\033[0m G%-2s %s\n" "$1" "$2"; }
fail()  { printf "  \033[31m✗\033[0m G%-2s %s\n" "$1" "$2"; FAILED_GATES+=("G${1}"); }

FAILED_GATES=()

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 6 — ISO Distribution Gate Verification (G12–G15)"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── G12: Model checksum verification (INV-7) ───────────────────────────
echo "G12: Model checksum verification (INV-7)..."

CHECKSUMS="${REPO_ROOT}/models/checksums.sha256"
if [ ! -f "${CHECKSUMS}" ]; then
    warn 12 "checksums.sha256 not found (skipped)"
else
    GGUF_COUNT=$(find "${REPO_ROOT}/models" -maxdepth 1 -name '*.gguf' 2>/dev/null | wc -l | tr -d ' ')
    if [ "$GGUF_COUNT" -eq 0 ]; then
        warn 12 "no .gguf files found (skipped)"
    else
        G12_FAIL=0
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            [[ "$line" == \#* ]] && continue

            expected_hash=$(echo "$line" | awk '{print $1}')
            raw_path=$(echo "$line" | awk '{print $2}')
            filename=$(basename "$raw_path")
            model_file="${REPO_ROOT}/models/${filename}"

            if [ ! -f "$model_file" ]; then
                echo "    SKIP: ${filename} (not present)"
                continue
            fi

            actual_hash=$(sha256sum "$model_file" | awk '{print $1}')
            if [ "$expected_hash" != "$actual_hash" ]; then
                echo "    MISMATCH: ${filename}"
                echo "      expected: ${expected_hash}"
                echo "      actual:   ${actual_hash}"
                G12_FAIL=1
            fi
        done < "${CHECKSUMS}"

        if [ "$G12_FAIL" -eq 0 ]; then
            pass 12 "all model checksums verified (${GGUF_COUNT} files)"
        else
            fail 12 "model checksum verification failed"
        fi
    fi

    MCPD_BIN="${REPO_ROOT}/src/mcpd/target/release/mcpd"
    if [ -f "$MCPD_BIN" ] && command -v strings >/dev/null 2>&1; then
        if strings "$MCPD_BIN" | grep -q MCPD_FS_TEST_ROOTS; then
            fail 12 "release mcpd contains test-only feature fs-test-roots"
        fi
    fi
fi

# ── G13: Systemd unit validation ───────────────────────────────────────
echo "G13: Systemd unit validation..."

G13_FAIL=0

if command -v systemd-analyze >/dev/null 2>&1; then
    for unit in "${SYSTEMD_DIR}"/*.service "${SYSTEMD_DIR}"/*.socket; do
        [ -f "$unit" ] || continue
        if ! systemd-analyze verify "$unit" 2>/dev/null; then
            echo "    systemd-analyze verify failed: $(basename "$unit")"
            G13_FAIL=1
        fi
    done
else
    warn 13 "systemd-analyze not available (skipped verify)"
fi

for svc in "${SYSTEMD_DIR}"/*.service; do
    [ -f "$svc" ] || continue
    svc_name=$(basename "$svc")
    if ! grep -q 'NoNewPrivileges=yes' "$svc"; then
        echo "    MISSING NoNewPrivileges=yes: ${svc_name}"
        G13_FAIL=1
    fi
done

for svc in icebreaker-pbd.service icebreaker-qbd.service; do
    svc_path="${SYSTEMD_DIR}/${svc}"
    if [ -f "$svc_path" ] && ! grep -q 'ProtectSystem=strict' "$svc_path"; then
        echo "    MISSING ProtectSystem=strict: ${svc}"
        G13_FAIL=1
    fi
done

FB_UNIT="${SYSTEMD_DIR}/icebreaker-first-boot.service"
if [ -f "$FB_UNIT" ] && ! grep -q 'ConditionPathExists=!/var/lib/icebreaker/.first-boot-complete' "$FB_UNIT"; then
    echo "    MISSING ConditionPathExists on first-boot.service"
    G13_FAIL=1
fi

CTRL_UNIT="${SYSTEMD_DIR}/icebreaker-controller.service"
if [ -f "$CTRL_UNIT" ] && ! grep -q 'After=.*icebreaker-pbd.service' "$CTRL_UNIT"; then
    echo "    MISSING After=icebreaker-pbd.service on controller.service"
    G13_FAIL=1
fi

if [ "$G13_FAIL" -eq 0 ]; then
    pass 13 "systemd units validated"
else
    fail 13 "systemd unit validation failed"
fi

# ── G14: Static checks ────────────────────────────────────────────────
echo "G14: Static checks..."

G14_FAIL=0

if bash "${CX_DIR}/tests/test_build_output.sh" --static-only 2>&1; then
    :
else
    echo "    test_build_output.sh --static-only failed"
    G14_FAIL=1
fi

if [ -f "${CX_DIR}/tests/test_first_boot.sh" ]; then
    if bash "${CX_DIR}/tests/test_first_boot.sh" 2>&1; then
        :
    else
        echo "    test_first_boot.sh failed"
        G14_FAIL=1
    fi
fi

if [ "$G14_FAIL" -eq 0 ]; then
    pass 14 "static checks passed"
else
    fail 14 "static checks failed"
fi

# ── G15: Python venv integrity ─────────────────────────────────────────
echo "G15: Python venv integrity..."

VENV_PYTHON="${CHROOT}/opt/icebreaker/venv/bin/python3"
if [ ! -f "$VENV_PYTHON" ]; then
    warn 15 "chroot venv not found (post-build only, skipped)"
else
    if [ -x "$VENV_PYTHON" ]; then
        pass 15 "chroot venv python3 exists and is executable"
    else
        fail 15 "chroot venv python3 is not executable"
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════"

if [ ${#FAILED_GATES[@]} -eq 0 ]; then
    echo "  Phase 6 gates passed."
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    exit 0
else
    echo "  FAILED gates: ${FAILED_GATES[*]}"
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    exit 1
fi
