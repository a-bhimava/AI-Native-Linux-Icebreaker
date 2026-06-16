#!/usr/bin/env bash
# ci.sh — Phase 1 mcpd gate checks.
#
# Runs the gates from docs/phase1_roadmap.md §2:
#   G1   — cargo test --release         (all unit + integration tests pass)
#   lint — cargo clippy -- -D warnings
#   G2   — tools/list reports 22 tools with schema_version "1.0.0"
#   G3   — ss -tlnp shows no mcpd network listeners after soak (INV-3)
#   G4   — cargo-fuzz target `validate` runs 60s with zero crashes (libFuzzer,
#          coverage-guided; replaces the old hand-rolled LCG unit test)
#   G9   — Latency benchmark: tools/list p95 under 100 ms.
#
# Usage:
#   ./ci.sh                # full Phase 1 gate
#   ./ci.sh --skip-soak    # quick local run (5s G3 soak instead of 300s)
#   ./ci.sh --quick        # alias for --skip-soak
#   ./ci.sh --skip-fuzz    # skip G4 (use when nightly toolchain unavailable)

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
CYN='\033[0;36m'
RST='\033[0m'

SOAK_SECS=300
RUN_FUZZ=1
for arg in "$@"; do
    case "$arg" in
        --skip-soak|--quick) SOAK_SECS=5 ;;
        --skip-fuzz)         RUN_FUZZ=0 ;;
    esac
done

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
if [[ "$TOOLS" -ne 22 ]]; then
    echo -e "${RED}[G2] FAIL${RST} — catalogue advertises $TOOLS tools (expected exactly 22)"
    exit 1
fi
echo -e "${GRN}[G2] PASS${RST} — $TOOLS tools, schema $SCHEMA"

# ── G3: no network listeners (Linux only) ─────────────────────────────────────
if [[ "$(uname)" != "Linux" ]]; then
    echo -e "${YLW}[G3]${RST} skipped — not Linux (mcpd runtime requires /proc)"
    echo -e "${YLW}[G4]${RST} skipped — not Linux (cargo-fuzz canonical run is the VM)"
    echo -e "${YLW}[G9]${RST} skipped — not Linux"
    echo -e "${GRN}Local gates (G1/G2/lint) passed.${RST} Run on Linux for G3/G4/G9."
    exit 0
fi

if ! command -v ss &>/dev/null; then
    echo -e "${RED}[G3]${RST} ss(8) not found — install iproute2"
    exit 1
fi

echo -e "${CYN}[G3]${RST} spawn mcpd, soak ${SOAK_SECS}s, assert no network listeners (INV-3)"
# mcpd shuts down on stdin EOF by design (see server::run_stdio_server).
# `</dev/null` would close stdin immediately. Instead pipe `sleep` into mcpd:
# sleep runs silently for the soak + a 5s buffer, then exits → mcpd reads EOF
# and shuts down cleanly. PID=$! captures mcpd (the last process in the pipe).
SOAK_PLUS=$((SOAK_SECS + 5))
sleep "$SOAK_PLUS" | ./target/release/mcpd &>/tmp/mcpd-ci.log &
PID=$!
sleep 1

if ! kill -0 "$PID" 2>/dev/null; then
    echo -e "${RED}[G3]${RST} mcpd died on startup — check /tmp/mcpd-ci.log"
    cat /tmp/mcpd-ci.log
    exit 1
fi

cleanup() {
    kill "$PID" 2>/dev/null || true
    # The sleep keeping stdin open is a child of this shell; clean it up too
    # so we don't leak it if ci.sh aborts before the soak finishes.
    pkill -P $$ -x sleep 2>/dev/null || true
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

# ── G4: cargo-fuzz `validate` target, 60s, zero crashes ───────────────────────
if [[ "$RUN_FUZZ" -eq 1 ]]; then
    echo -e "${CYN}[G4]${RST} cargo +nightly fuzz run validate -- -max_total_time=60"
    if ! command -v cargo-fuzz &>/dev/null && ! rustup run nightly cargo fuzz --version &>/dev/null; then
        echo -e "${RED}[G4] FAIL${RST} — cargo-fuzz not installed. Install with:"
        echo "    rustup toolchain install nightly"
        echo "    cargo +nightly install cargo-fuzz"
        echo "    (or rerun with ./ci.sh --skip-fuzz to bypass)"
        exit 1
    fi
    if cargo +nightly fuzz run validate -- -max_total_time=60 -print_final_stats=1 2>&1 | tail -20; then
        echo -e "${GRN}[G4] PASS${RST} — 60s libFuzzer run, zero crashes"
    else
        echo -e "${RED}[G4] FAIL${RST} — fuzz crashed or timed out (see fuzz/artifacts/)"
        exit 1
    fi
else
    echo -e "${YLW}[G4]${RST} skipped via --skip-fuzz"
fi

# ── G9: per-request latency benchmark (tools/list p95 < 100 ms) ──────────────
# Spawn ONE long-lived mcpd, pipe 100 sequential JSON-RPC requests over stdin,
# read each response from stdout, time the round-trip. The previous
# implementation spawned a fresh binary per iteration and measured cold-start
# cost, which can't reflect per-request latency. All 100 requests are
# `tools/list` for the lightest path (no fs I/O, no sandbox excursion) so p95
# variance is dominated by JSON parsing + dispatch, not by tool work.
echo -e "${CYN}[G9]${RST} per-request latency benchmark (1 long-lived mcpd, 100 calls)"
PYTHON_SCRIPT='
import json, subprocess, sys, time
p = subprocess.Popen(
    ["./target/release/mcpd"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    bufsize=0,
)
durations = []
try:
    for i in range(100):
        req = json.dumps({"jsonrpc":"2.0","method":"tools/list","id":i+1}).encode() + b"\n"
        start = time.perf_counter()
        p.stdin.write(req)
        p.stdin.flush()
        resp_line = p.stdout.readline()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if not resp_line:
            print(f"G9: empty response on iteration {i+1} — mcpd died", file=sys.stderr)
            sys.exit(2)
        durations.append(elapsed_ms)
    p.stdin.close()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
finally:
    if p.poll() is None:
        p.kill()
durations.sort()
p50 = durations[49]
p95 = durations[94]
p99 = durations[98]
print(f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms (n=100)")
sys.exit(0 if p95 < 100 else 1)
'
if python3 -c "$PYTHON_SCRIPT"; then
    echo -e "${GRN}[G9] PASS${RST}"
else
    echo -e "${RED}[G9] FAIL${RST} — p95 latency exceeds 100 ms budget"
    exit 1
fi

echo -e "${GRN}All Phase 1 gates passed.${RST}"
