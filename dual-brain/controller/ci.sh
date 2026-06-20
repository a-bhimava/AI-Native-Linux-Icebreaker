#!/usr/bin/env bash
# ci.sh — Icebreaker Phase 2 + Phase 5 + Phase 6T exit-gate verification (G1–G11, G5.1–G5.P2c, G16–G22).
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

# Source the gitignored deploy.env (if present) so ANTHROPIC_API_KEY / GEMINI_API_KEY
# and ICEBREAKER_PORT_QB are available to gates G9 (latency) and G10 (backend parity)
# without a manual export. Template: scripts/deploy.env.example.
_DEPLOY_ENV="$DUAL_BRAIN/scripts/deploy.env"
# shellcheck source=/dev/null
[ -f "$_DEPLOY_ENV" ] && source "$_DEPLOY_ENV"
unset _DEPLOY_ENV

pass()  { printf "  \033[32m✓\033[0m G%-2s %s\n" "$1" "$2"; }
warn()  { printf "  \033[33m⚠\033[0m G%-2s %s\n" "$1" "$2"; }
fail()  { printf "  \033[31m✗\033[0m G%-2s %s\n" "$1" "$2"; FAILED_GATES+=("G${1}"); }

FAILED_GATES=()

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Icebreaker Phase 2 + Phase 5 + Phase 6T — Exit Gate Verification"
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

# The llama local-backend tests health-probe the QB llama-server on construction,
# so they fail without it. Include them only when the server is reachable — on the
# VM it is up (started before the gate run), so they DO run there. Mirrors the
# exclusion deploy_to_vm.sh already applies for the same reason.
if ! curl -s --max-time 1 "http://127.0.0.1:${ICEBREAKER_PORT_QB:-8081}/health" >/dev/null 2>&1; then
  IGNORE_OPTS+=("--ignore=controller/tests/test_llama_local_backend.py")
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
else
  set +e
  G2_OUT="$(python3 scripts/export_mcpd_catalogue.py check 2>&1)"
  G2_RC=$?
  set -e
  if [ "$G2_RC" -eq 0 ]; then
    pass 2 "no catalogue drift (classifier ↔ mcpd parity)"
  elif [ "$G2_RC" -eq 2 ]; then
    # exit 2 == mcpd binary not found (export_mcpd_catalogue._find_mcpd). Build
    # src/mcpd on the VM (cargo build --release) to enable the real drift check.
    warn 2 "mcpd binary not found — build src/mcpd on the VM to enable (skipping)"
  else
    fail 2 "catalogue drift detected — see: $G2_OUT"
  fi
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
  pass 6 "0/75 payloads caused unintended dispatch (30 injections + 20 reflections + 15 multi-turn + 10 bypass)"
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
for _ in range(20):
    t0 = time.monotonic()
    backend.complete(system=system_prompt, user='show disk usage', schema=None, max_retries=1)
    times.append((time.monotonic() - t0) * 1000)
times.sort()
# 20 samples: index 18 = nearest-rank p95 (int(20*0.95) = 19 = max,
# so use index 18 for true p95 with nearest-rank method)
p95 = times[int(len(times) * 0.95) - 1]
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

# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 gates (G5.1–G5.6)
# ═══════════════════════════════════════════════════════════════════════════════

# ── G5.1: HITL spoof-sanitization + a11y fallback ───────────────────────────
echo "G5.1: HITL spoof-sanitization + a11y fallback..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_hitl_sanitize.py \
    controller/tests/test_hitl_a11y.py -x -q 2>&1; then
  pass 5.1 "HITL sanitize + a11y OK"
else
  fail 5.1 "HITL sanitize / a11y test failed"
fi

echo ""

# ── G5.2: Lockout / raw-input ──────────────────────────────────────────────
echo "G5.2: Lockout / raw-input..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_hitl_rawinput.py -x -q 2>&1; then
  pass 5.2 "raw-input lockout OK"
else
  fail 5.2 "raw-input lockout test failed"
fi

echo ""

# ── G5.3: Keymap ───────────────────────────────────────────────────────────
echo "G5.3: Keymap..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_keymap.py -x -q 2>&1; then
  pass 5.3 "keymap OK"
else
  fail 5.3 "keymap test failed"
fi

echo ""

# ── G5.4: Actions + trust store + audit fields ────────────────────────────
echo "G5.4: Actions + trust store + audit fields..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_hitl_actions.py \
    controller/tests/test_trust_store.py \
    controller/tests/test_hitl_audit_fields.py -x -q 2>&1; then
  pass 5.4 "actions + trust store + audit fields OK"
else
  fail 5.4 "actions / trust store / audit fields test failed"
fi

echo ""

# ── G5.5: Tier-2 escalate-only + classifier registry ─────────────────────
echo "G5.5: Tier-2 escalate-only + classifier registry..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_tier2_review.py \
    controller/tests/test_classifier_registry.py -x -q 2>&1; then
  pass 5.5 "tier-2 review + classifier registry OK"
else
  fail 5.5 "tier-2 review / classifier registry test failed"
fi

echo ""

# ── G5.6: Audit hash-chain + redaction ────────────────────────────────────
echo "G5.6: Audit hash-chain + redaction..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_audit_chain.py \
    controller/tests/test_audit_redaction.py -x -q 2>&1; then
  pass 5.6 "audit hash-chain + redaction OK"
else
  fail 5.6 "audit hash-chain / redaction test failed"
fi

echo ""

# ── G5.6b: CLI verify smoke ──────────────────────────────────────────────
echo "G5.6b: CLI audit --verify smoke..."
_SMOKE_LOG=$(mktemp /tmp/audit-smoke-XXXXXX.log)
if PYTHONPATH=. python3 -c "
from controller.audit import AuditLog, AuditFields, Outcome
log = AuditLog(path='${_SMOKE_LOG}', fsync_each_write=False)
try:
    log.write_fields(AuditFields(
        session_id='ci-smoke', turn_index=0, intent_id='ci-0',
        action='system.status', target='', tier=0, reason='ci',
        risk_level='read_only', outcome=Outcome.EXECUTED,
        duration_ms=1.0, backend='local', model='test',
        tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
    ))
finally:
    log.close()
" 2>&1 && PYTHONPATH=. python3 -m controller.audit --verify "$_SMOKE_LOG" 2>&1; then
  pass 5.6b "CLI verify smoke OK"
else
  fail 5.6b "CLI verify smoke failed"
fi
rm -f "$_SMOKE_LOG"

echo ""

# ── G5.P1a: Credential & resource hygiene ────────────────────────────────
echo "G5.P1a: Credential & resource hygiene..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_mcpd_env_scrub.py \
    controller/tests/test_repl_history.py \
    controller/tests/test_cost_limits.py \
    controller/tests/test_toctou.py -x -q 2>&1; then
  pass 5.P1a "env scrub + history + cost/limits + TOCTOU OK"
else
  fail 5.P1a "credential / resource hygiene test failed"
fi

# ── G5.P1b: Audit viewer ──────────────────────────────────────────────
echo "G5.P1b: Audit viewer..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_audit_viewer.py \
    controller/tests/test_audit_viewer_repl.py -x -q 2>&1; then
  pass 5.P1b "audit viewer OK"
else
  fail 5.P1b "audit viewer test failed"
fi

echo ""

# ── G5.P1b-cli: Audit viewer CLI smoke ───────────────────────────────
echo "G5.P1b-cli: Audit viewer CLI smoke..."
_VIEWER_LOG=$(mktemp /tmp/audit-viewer-smoke-XXXXXX.log)
if PYTHONPATH=. python3 -c "
from controller.audit import AuditLog, AuditFields, Outcome
log = AuditLog(path='${_VIEWER_LOG}', fsync_each_write=False)
try:
    for i in range(5):
        log.write_fields(AuditFields(
            session_id='viewer-smoke', turn_index=i, intent_id=f'intent-{i}',
            action='system.status', target='', tier=0, reason='ci',
            risk_level='read_only', outcome=Outcome.EXECUTED,
            duration_ms=1.0, backend='local', model='test',
            tokens_in=10, tokens_out=5, cost_estimate_usd=0.0,
        ))
finally:
    log.close()
" 2>&1 && PYTHONPATH=. python3 -m controller.audit_viewer --json "$_VIEWER_LOG" 2>&1 | python3 -c "
import json, sys
lines = sys.stdin.read().strip().split('\n')
assert len(lines) == 5, f'expected 5 lines, got {len(lines)}'
for line in lines:
    entry = json.loads(line)
    assert 'session_id' in entry
print(f'  OK ({len(lines)} entries)')
"; then
  pass 5.P1b-cli "CLI smoke OK"
else
  fail 5.P1b-cli "CLI smoke failed"
fi
rm -f "$_VIEWER_LOG"

# ── G5.P1c: Streaming + progress + cancel ────────────────────────────
echo "G5.P1c: Streaming + progress + cancel..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_turn_events.py \
    controller/tests/test_streaming.py \
    controller/tests/test_stream_backends.py \
    controller/tests/test_progress_repl.py -x -q 2>&1; then
  pass 5.P1c "streaming + progress + cancel OK"
else
  fail 5.P1c "streaming / progress / cancel test failed"
fi

# ── G5.P1d: Undo scaffold ────────────────────────────────────────────
echo "G5.P1d: Undo scaffold..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_undo.py \
    controller/tests/test_undo_repl.py -x -q 2>&1; then
  pass 5.P1d "undo scaffold OK"
else
  fail 5.P1d "undo scaffold test failed"
fi

# ── G5.P2a: OpenAI backend + verifier voting ──────────────────────
echo "G5.P2a: OpenAI backend + verifier voting..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_openai_backend.py \
    controller/tests/test_verifier.py -x -q 2>&1; then
  pass 5.P2a "OpenAI backend + verifier OK"
else
  fail 5.P2a "OpenAI backend / verifier test failed"
fi

# ── G5.P2b: Daemon + client + protocol ────────────────────────────
echo "G5.P2b: Daemon + client + protocol..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_daemon.py \
    controller/tests/test_client.py \
    controller/tests/test_protocol.py -x -q 2>&1; then
  pass 5.P2b "daemon/client/protocol OK"
else
  fail 5.P2b "daemon/client/protocol test failed"
fi

# ── G5.P2c: Presenter registry + screen-reader + GTK ──────────────
echo "G5.P2c: Presenter registry + screen-reader + GTK..."
if PYTHONPATH=. python3 -m pytest controller/tests/test_presenter_registry.py \
    controller/tests/test_screen_reader_presenter.py \
    controller/tests/test_gtk_presenter.py -x -q 2>&1; then
  pass 5.P2c "presenter registry OK"
else
  fail 5.P2c "presenter registry test failed"
fi

# ── Phase 6: ISO Distribution Gates (G12–G15) ──────────────────────────
CX_DISTRO="${DUAL_BRAIN}/../cx-distro"
if [ -d "${CX_DISTRO}" ] && [ -f "${CX_DISTRO}/ci.sh" ]; then
    echo ""
    echo "Phase 6 — ISO Distribution Gates (G12–G15)"
    if bash "${CX_DISTRO}/ci.sh"; then
        pass 6 "Phase 6 gates passed"
    else
        fail 6 "Phase 6 gates failed"
    fi
fi

# ── Phase 6T: AI Terminal Gates (G16–G22) ────────────────────────────────
CX_DIR="${DUAL_BRAIN}/../cx-distro"
echo ""
echo "Phase 6T — AI Terminal Gates (G16–G22)"

# ── G16: GUI Agent sandbox integrity ─────────────────────────────────────
echo "G16: GUI Agent sandbox integrity..."
if bash "${CX_DIR}/tests/test_gui_agent.sh" "${DUAL_BRAIN}"; then
  pass 16 "GUI Agent sandbox integrity OK"
else
  fail 16 "GUI Agent sandbox integrity failed"
fi

echo ""

# ── G17: GUI tool schema validation ─────────────────────────────────────
echo "G17: GUI tool schema validation..."
if PYTHONPATH="${DUAL_BRAIN}" python3 -c "
from gui_agent.protocol import _PARAM_SCHEMAS, ALL_GUI_METHODS
from controller._mcpd_tools import ALL_GUI_TOOLS, GUI_READONLY_TOOLS, GUI_WRITE_TOOLS

# Every method must have a schema
missing = [m for m in ALL_GUI_METHODS if m not in _PARAM_SCHEMAS]
assert not missing, f'methods without schemas: {missing}'

# Read + write = all
combined = GUI_READONLY_TOOLS | GUI_WRITE_TOOLS
assert combined == ALL_GUI_TOOLS, (
    f'GUI_READONLY_TOOLS | GUI_WRITE_TOOLS != ALL_GUI_TOOLS; '
    f'diff: {combined.symmetric_difference(ALL_GUI_TOOLS)}'
)
print('  schema coverage complete; read|write == all')
" 2>&1; then
  pass 17 "GUI tool schema validation OK"
else
  fail 17 "GUI tool schema validation failed"
fi

echo ""

# ── G18: RPA sandbox integrity ──────────────────────────────────────────
echo "G18: RPA sandbox integrity..."
_G18_OK=true
if ! bash "${CX_DIR}/tests/test_rpa_bridge.sh" "${DUAL_BRAIN}"; then
  _G18_OK=false
fi
if ! PYTHONPATH="${DUAL_BRAIN}" python3 -c "
from controller._mcpd_tools import RPA_WRITE_TOOLS
from controller.risk_classifier import classify, Tier
from controller.trust_store import TrustStore

# All RPA write tools must classify as HIGH
for tool in RPA_WRITE_TOOLS:
    r = classify({'action': tool, 'target': '', 'risk_level': ''})
    assert r.tier == Tier.HIGH, f'{tool} classified as {r.tier}, expected HIGH'

# Trust store must reject rpa.execute_workflow grant
store = TrustStore()
try:
    store.grant(action='rpa.execute_workflow', target_prefix='',
                max_tier=Tier.LOW, session_id='ci', ttl_seconds=300)
    assert False, 'trust store should reject rpa.execute_workflow grant'
except ValueError:
    pass
print('  RPA write tools = HIGH; trust store rejects rpa.execute_workflow')
" 2>&1; then
  _G18_OK=false
fi
if $_G18_OK; then
  pass 18 "RPA sandbox integrity OK"
else
  fail 18 "RPA sandbox integrity failed"
fi

echo ""

# ── G19: Display sanitization (best-effort — warn only) ─────────────────
echo "G19: Display sanitization..."
_G19_SUSPECT=$(grep -rn 'os\.system\|subprocess\.call.*shell=True' \
    "${DUAL_BRAIN}/gui_agent/" "${DUAL_BRAIN}/rpa_bridge/" \
    "${DUAL_BRAIN}/terminal/" \
    --include='*.py' 2>/dev/null \
    | grep -v __pycache__ | grep -v '/tests/' || true)
if [ -z "$_G19_SUSPECT" ]; then
  pass 19 "no suspicious shell patterns in display modules"
else
  warn 19 "suspicious shell patterns found (review manually): ${_G19_SUSPECT}"
fi

echo ""

# ── G20: CoT event coverage ─────────────────────────────────────────────
echo "G20: CoT event coverage..."
if PYTHONPATH="${DUAL_BRAIN}" python3 -m pytest controller/tests/test_cot_streaming.py -x -q 2>&1; then
  pass 20 "CoT event coverage OK"
else
  fail 20 "CoT event coverage failed"
fi

echo ""

# ── G20.1: Companion panel dual-mode ────────────────────────────────────
echo "G20.1: Companion panel dual-mode..."
if PYTHONPATH="${DUAL_BRAIN}" python3 -m pytest terminal/tests/test_tui.py -x -q 2>&1; then
  pass 20.1 "companion panel dual-mode OK"
else
  fail 20.1 "companion panel dual-mode failed"
fi

echo ""

# ── G21: Screenshot cleanup ────────────────────────────────────────────
echo "G21: Screenshot cleanup..."
if PYTHONPATH="${DUAL_BRAIN}" python3 -m pytest gui_agent/tests/test_screenshots.py -x -q 2>&1; then
  pass 21 "screenshot cleanup OK"
else
  fail 21 "screenshot cleanup failed"
fi

echo ""

# ── G22: RPA timeout enforcement ────────────────────────────────────────
echo "G22: RPA timeout enforcement..."
if PYTHONPATH="${DUAL_BRAIN}" python3 -m pytest rpa_bridge/tests/test_bridge_integration.py -x -q 2>&1; then
  pass 22 "RPA timeout enforcement OK"
else
  fail 22 "RPA timeout enforcement failed"
fi

echo ""
echo "═══════════════════════════════════════════════════════════"

if [ ${#FAILED_GATES[@]} -eq 0 ]; then
  echo "  All gates passed. Phase 2 + Phase 5 (P0/P1/P2) + Phase 6T ready for PR review."
  echo "═══════════════════════════════════════════════════════════"
  echo ""
  exit 0
else
  echo "  FAILED gates: ${FAILED_GATES[*]}"
  echo "═══════════════════════════════════════════════════════════"
  echo ""
  exit 1
fi
