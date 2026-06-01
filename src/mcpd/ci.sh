#!/usr/bin/env bash
# ci.sh — Phase 1 mcpd gate checks.
#
# Runs the three gates from docs/phase1_roadmap.md §M1.0:
#   G1   — cargo test --release   (all unit + integration tests pass)
#   lint — cargo clippy -- -D warnings
#   G3   — ss -tlnp shows no mcpd network listeners (INV-3)
#
# Usage:
#   ./ci.sh             # run all checks
#   ./ci.sh --skip-soak # skip the 5-minute listener soak (use 5s instead)
#
# Exits 0 only when every gate passes. macOS dev mode skips the listener check
# (mcpd's runtime is Linux-only — see roadmap §1).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
CYN='\033[0;36m'
RST='\033[0m'

SOAK_SECS=300
[[ "${1:-}" == "--skip-soak" ]] && SOAK_SECS=5

# ── G1: tests ─────────────────────────────────────────────────────────────────
echo -e "${CYN}[G1]${RST} cargo test --release"
cargo test --release --quiet

# ── lint: clippy clean ────────────────────────────────────────────────────────
echo -e "${CYN}[lint]${RST} cargo clippy -- -D warnings"
cargo clippy --release --all-targets --quiet -- -D warnings

# ── G3: no network listeners (Linux only) ────────────────────────────────────
if [[ "$(uname)" != "Linux" ]]; then
    echo -e "${YLW}[G3]${RST} skipped — not Linux (mcpd runtime requires /proc)"
    echo -e "${GRN}Local gates passed.${RST} Run on Linux for G3."
    exit 0
fi

if ! command -v ss &>/dev/null; then
    echo -e "${RED}[G3]${RST} ss(8) not found — install iproute2"
    exit 1
fi

echo -e "${CYN}[G3]${RST} spawn mcpd, soak ${SOAK_SECS}s, assert no network listeners (INV-3)"
./target/release/mcpd </dev/null &>/tmp/mcpd-ci.log &
PID=$!
# Give it a moment to settle.
sleep 1

if ! kill -0 "$PID" 2>/dev/null; then
    echo -e "${RED}[G3]${RST} mcpd died on startup — check /tmp/mcpd-ci.log"
    cat /tmp/mcpd-ci.log
    exit 1
fi

cleanup() {
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
}
trap cleanup EXIT

sleep "$SOAK_SECS"

# Any listener attributable to this PID is an INV-3 violation.
if ss -tlnp 2>/dev/null | grep -q "pid=${PID},"; then
    echo -e "${RED}[G3] FAIL${RST} — mcpd opened a network listener:"
    ss -tlnp | grep "pid=${PID},"
    exit 1
fi

echo -e "${GRN}[G3] PASS${RST} — no listeners after ${SOAK_SECS}s soak"
echo -e "${GRN}All Phase 1 gates passed.${RST}"
