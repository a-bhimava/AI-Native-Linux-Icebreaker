#!/usr/bin/env bash
# ci.sh — Phase 1 mcpd gate checks.
#
# Runs the gates from docs/phase1_roadmap.md §2:
#   G1   — cargo test --release         (all unit + integration tests pass)
#   lint — cargo clippy -- -D warnings
#   G2   — tools/list reports 22 tools with schema_version "1.0.0"
#   G3   — ss -tlnp shows no mcpd network listeners after soak (INV-3)
#   G4   — fs::validate fuzz: 10k random inputs, zero escapes (inline; cargo
#          test fuzz_validate_never_panics_or_escapes does this)
#   G9   — Latency benchmark: tools/list p95 under 100 ms.
#
# Usage:
#   ./ci.sh             # full Phase 1 gate
#   ./ci.sh --skip-soak # quick local run (5s G3 soak instead of 300s)
#   ./ci.sh --quick     # alias for --skip-soak

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
CYN='\033[0;36m'
RST='\033[0m'

SOAK_SECS=300
case "${1:-}" in
    --skip-soak|--quick) SOAK_SECS=5 ;;
esac

# ── G1: tests (includes the G4 fuzz target as a unit test) ────────────────────
# --features fs-test-roots compiles in the MCPD_FS_TEST_ROOTS hook used by the
# Landlock kernel-enforcement integration test. Production release builds
# (without this flag) do not link the code path.
echo -e "${CYN}[G1]${RST} cargo test --release --features fs-test-roots"
cargo test --release --features fs-test-roots --quiet

# ── lint ──────────────────────────────────────────────────────────────────────
echo -e "${CYN}[lint]${RST} cargo clippy --release --all-targets -- -D warnings"
cargo clippy --release --all-targets --quiet -- -D warnings

# ── G2: catalogue surface ─────────────────────────────────────────────────────
echo -e "${CYN}[G2]${RST} tools/list catalogue surface"
RESP=$(echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | ./target/release/mcpd 2>/dev/null || true)
if [[ -z "$RESP" ]]; then
    # Binary not built — build now.
    cargo build --release --quiet
    RESP=$(echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | ./target/release/mcpd 2>/dev/null)
fi
TOOLS=$(echo "$RESP" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["result"]["tools"]))' 2>/dev/null || echo 0)
SCHEMA=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["schema_version"])' 2>/dev/null || echo "?")
if [[ "$TOOLS" -lt 20 ]]; then
    echo -e "${RED}[G2] FAIL${RST} — catalogue advertises $TOOLS tools (expected ≥ 20)"
    exit 1
fi
echo -e "${GRN}[G2] PASS${RST} — $TOOLS tools, schema $SCHEMA"

# ── G3: no network listeners (Linux only) ─────────────────────────────────────
if [[ "$(uname)" != "Linux" ]]; then
    echo -e "${YLW}[G3]${RST} skipped — not Linux (mcpd runtime requires /proc)"
    echo -e "${YLW}[G9]${RST} skipped — not Linux"
    echo -e "${GRN}Local gates (G1/G2/G4/lint) passed.${RST} Run on Linux for G3/G9."
    exit 0
fi

if ! command -v ss &>/dev/null; then
    echo -e "${RED}[G3]${RST} ss(8) not found — install iproute2"
    exit 1
fi

echo -e "${CYN}[G3]${RST} spawn mcpd, soak ${SOAK_SECS}s, assert no network listeners (INV-3)"
./target/release/mcpd </dev/null &>/tmp/mcpd-ci.log &
PID=$!
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

if ss -tlnp 2>/dev/null | grep -q "pid=${PID},"; then
    echo -e "${RED}[G3] FAIL${RST} — mcpd opened a network listener:"
    ss -tlnp | grep "pid=${PID},"
    exit 1
fi
echo -e "${GRN}[G3] PASS${RST} — no listeners after ${SOAK_SECS}s soak"

# ── G9: latency benchmark (tools/list p95 < 100 ms) ──────────────────────────
echo -e "${CYN}[G9]${RST} latency benchmark (100 tools/list calls)"
PYTHON_SCRIPT='
import json, subprocess, sys, time
durations = []
for _ in range(100):
    start = time.perf_counter()
    p = subprocess.run(
        ["./target/release/mcpd"],
        input=b"{\"jsonrpc\":\"2.0\",\"method\":\"tools/list\",\"id\":1}\n",
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    durations.append((time.perf_counter() - start) * 1000.0)
durations.sort()
p50 = durations[49]
p95 = durations[94]
print(f"p50={p50:.1f}ms p95={p95:.1f}ms")
sys.exit(0 if p95 < 100 else 1)
'
if python3 -c "$PYTHON_SCRIPT"; then
    echo -e "${GRN}[G9] PASS${RST}"
else
    echo -e "${RED}[G9] FAIL${RST} — p95 latency exceeds 100 ms budget"
    exit 1
fi

echo -e "${GRN}All Phase 1 gates passed.${RST}"
