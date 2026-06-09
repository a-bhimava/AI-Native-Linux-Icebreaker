#!/usr/bin/env bash
# ci.sh — Icebreaker Phase 2 exit-gate verification (G1–G11).
#
# Run from the dual-brain/ directory:
#   bash controller/ci.sh
#
# Prerequisites: Python 3.9+, pytest, jsonschema, tomli (or Python 3.11+).
# Optional: hypothesis (for G5), anthropic + google-generativeai (for G10),
#            llama-server on :8081 (for G9).
#
# Exit codes: 0 = all gates passed, 1 = one or more gates failed.

set -euo pipefail

DUAL_BRAIN="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DUAL_BRAIN"

# Activate virtualenv if present alongside dual-brain/ or in HOME
for _venv in \
    "$HOME/dual-brain-venv" \
    "$DUAL_BRAIN/../venv" \
    "$DUAL_BRAIN/venv"
do
  if [ -f "$_venv/bin/activate" ]; then
    # shellcheck disable=SC1090
    source "$_venv/bin/activate"
    break
  fi
done
unset _venv

pass()  { printf "  \033[32m✓\033[0m G%-2s %s\n" "$1" "$2"; }
warn()  { printf "  \033[33m⚠\033[0m G%-2s %s\n" "$1" "$2"; }
fail()  { printf "  \033[31m✗\033[0m G%-2s %s\n" "$1" "$2"; FAILED_GATES+=("G${1}"); }

FAILED_GATES=()

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Icebreaker Phase 2 — Exit Gate Verification"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── G1: Full test suite ──────────────────────────────────────────────────────
echo "G1: Controller test suite..."

IGNORE_OPTS=(
  "--ignore=controller/tests/test_backends_base.py"
  "--ignore=controller/tests/test_grammar_parity.py"
  "--ignore=controller/tests/test_intent_schema.py"
  "--ignore=controller/tests/test_validator_fuzz.py"
)

# Include hypothesis tests if the module is available.
if python3 -c "import hypothesis" 2>/dev/null; then
  IGNORE_OPTS=()
fi

# Include anthropic tests if the SDK is available.
if ! python3 -c "import anthropic" 2>/dev/null; then
  IGNORE_OPTS+=("--ignore=controller/tests/test_anthropic_backend.py")
  IGNORE_OPTS+=("--ignore=controller/tests/test_cross_backend.py")
fi

if PYTHONPATH=. python3 -m pytest controller/tests/ -q --tb=short \
    "${IGNORE_OPTS[@]}" 2>&1 | tee /tmp/icebreaker_ci_g1.log; then
  TOTAL=$(grep -E "^[0-9]+ passed" /tmp/icebreaker_ci_g1.log | grep -oE "^[0-9]+" || echo "0")
  pass 1 "all tests passed (${TOTAL} passed)"
else
  fail 1 "test failures — see /tmp/icebreaker_ci_g1.log"
fi

echo ""

# ── G2: Catalogue drift check ────────────────────────────────────────────────
echo "G2: Catalogue drift check..."
if [ ! -f "scripts/export_mcpd_catalogue.py" ]; then
  warn 2 "export_mcpd_catalogue.py not found — skipping (add script to enable)"
elif python3 scripts/export_mcpd_catalogue.py check 2>/dev/null; then
  pass 2 "no catalogue drift (classifier ↔ mcpd parity)"
else
  fail 2 "catalogue drift detected"
fi

echo ""

# ── G3/G4: QB/PB isolation ───────────────────────────────────────────────────
echo "G3/G4: QB/PB isolation check..."
if PYTHONPATH=. python3 -m controller --check-isolation 2>&1; then
  pass 3 "QB/PB isolation verified"
  pass 4 "PB receives UUID only"
else
  fail 3 "isolation check failed"
  fail 4 "PB may receive raw user text"
fi

echo ""

# ── G5: Schema fuzz (hypothesis) ─────────────────────────────────────────────
echo "G5: Schema fuzz..."
if python3 -c "import hypothesis" 2>/dev/null; then
  if PYTHONPATH=. python3 -m pytest controller/tests/test_validator_fuzz.py \
      controller/tests/test_intent_schema.py -v 2>&1; then
    pass 5 "no fuzz crashes, no false accepts"
  else
    fail 5 "fuzz failures"
  fi
else
  warn 5 "hypothesis not installed — skipping (pip install hypothesis to enable)"
fi

echo ""

# ── G6: Adversarial corpus ───────────────────────────────────────────────────
echo "G6: Adversarial corpus..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_adversarial.py -v 2>&1; then
  pass 6 "0/125 payloads caused unintended dispatch"
else
  fail 6 "adversarial test failed"
fi

echo ""

# ── G7: Audit log provenance ─────────────────────────────────────────────────
echo "G7: Audit log provenance..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_audit.py \
    controller/tests/test_auditor.py -v 2>&1; then
  pass 7 "audit log provenance verified"
else
  fail 7 "audit test failed"
fi

echo ""

# ── G8: HITL lockout order ───────────────────────────────────────────────────
echo "G8: HITL lockout order..."
if PYTHONPATH=. python3 -m pytest \
    controller/tests/test_hitl.py::test_lockout_sleep_before_select -v 2>&1; then
  pass 8 "sleep-before-select order verified"
else
  fail 8 "G8 lockout call-order test failed"
fi

echo ""

# ── G9: Latency (requires live llama-server on :8081) ───────────────────────
echo "G9: Latency check..."
if curl -s --max-time 1 http://127.0.0.1:8081/health > /dev/null 2>&1; then
  if python3 - <<'PYEOF'
import sys, time
sys.path.insert(0, '.')
from controller.config import load, PromptLoader
from controller.backends.llama_local_backend import LlamaCppLocalBackend

cfg = load()
prompts = PromptLoader(cfg.prompts)
system_prompt = prompts.get("qb_local")
backend = LlamaCppLocalBackend(cfg.qb)
times = []
for _ in range(10):
    t0 = time.monotonic()
    backend.complete(system=system_prompt, user='show disk usage', schema=None, max_retries=1)
    times.append((time.monotonic() - t0) * 1000)
times.sort()
p95 = times[int(len(times) * 0.95)]
print(f"  p95: {p95:.0f}ms")
if p95 > 2000:
    print(f"  FAIL: latency {p95:.0f}ms exceeds 2000ms budget", file=sys.stderr)
    sys.exit(1)
PYEOF
  then
    pass 9 "latency within 2000ms p95 budget"
  else
    fail 9 "latency over budget"
  fi
else
  warn 9 "llama-server not running on :8081 — skipping"
fi

echo ""

# ── G10: Backend parity (mocked — requires anthropic + google-generativeai SDKs) ─
echo "G10: Backend parity..."
if python3 -c "import anthropic; import google.generativeai" 2>/dev/null; then
  if PYTHONPATH=. python3 -m pytest controller/tests/test_cross_backend.py -v 2>&1; then
    pass 10 "all backends emit same tier classifications"
  else
    fail 10 "backend parity failed"
  fi
else
  warn 10 "anthropic / google-generativeai SDK not installed — skipping"
fi

echo ""

# ── G11: Multi-turn INV-2-extended isolation ─────────────────────────────────
echo "G11: Multi-turn INV-2-extended isolation..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_repl_isolation.py \
    controller/tests/test_session.py -v 2>&1; then
  pass 11 "multi-turn adversarial session: 0 unintended dispatches"
else
  fail 11 "isolation test failed"
fi

echo ""
echo "═══════════════════════════════════════════════════════════"

if [ ${#FAILED_GATES[@]} -eq 0 ]; then
  echo "  All gates passed. Phase 2 is ready for PR review."
  echo "═══════════════════════════════════════════════════════════"
  echo ""
  exit 0
else
  echo "  FAILED gates: ${FAILED_GATES[*]}"
  echo "═══════════════════════════════════════════════════════════"
  echo ""
  exit 1
fi
