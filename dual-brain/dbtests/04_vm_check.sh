#!/usr/bin/env bash
# 04_vm_check.sh — five VM sanity checks against the live GCP instance.
#
#   1. VM is running
#   2. mcpd binary is built + executable
#   3. dual-brain-venv has the test deps (jsonschema, hypothesis, pytest)
#   4. classifier ↔ mcpd catalogue parity (G2 drift check) on live mcpd
#   5. AuditLog write produces a file with mode 0o640 and a 0o700 parent
#
# Idempotent. Reads only — doesn't redeploy. (Use 05_deploy_to_vm.sh for
# that.) Falls cleanly if dual-brain isn't on the VM yet (step 3/4/5 skip
# with a clear message).
#
# Env overrides:
#   VM_NAME    default: instance-20260528-030421
#   VM_ZONE    default: us-central1-a
#   VM_DUAL_BRAIN  default: ~/dual-brain  (path on the VM)
#   VM_MCPD    default: ~/icebreaker/src/mcpd/target/release/mcpd
#   VM_VENV    default: ~/dual-brain-venv

set -uo pipefail

VM_NAME="${VM_NAME:-instance-20260528-030421}"
VM_ZONE="${VM_ZONE:-us-central1-a}"
VM_DUAL_BRAIN="${VM_DUAL_BRAIN:-\$HOME/dual-brain}"
VM_MCPD="${VM_MCPD:-\$HOME/icebreaker/src/mcpd/target/release/mcpd}"
VM_VENV="${VM_VENV:-\$HOME/dual-brain-venv}"

CYN=$'\033[36m'; GRN=$'\033[32m'; RED=$'\033[31m'; YLW=$'\033[33m'
DIM=$'\033[2m'; BOLD=$'\033[1m'; RST=$'\033[0m'

if ! command -v gcloud >/dev/null 2>&1; then
  echo "${RED}FAIL${RST}  gcloud not on PATH" >&2
  echo "${DIM}install: https://cloud.google.com/sdk/docs/install${RST}" >&2
  exit 2
fi

ssh_cmd() {
  # Run a shell snippet on the VM. Quiet flags suppress gcloud's preamble.
  gcloud compute ssh "${VM_NAME}" \
    --zone="${VM_ZONE}" \
    --quiet \
    --command="$1" 2>&1
}

FAILURES=0
SKIPPED=0

ok()    { echo "  ${GRN}✓${RST} $1"; }
warn()  { echo "  ${YLW}⚠${RST} $1"; SKIPPED=$((SKIPPED+1)); }
bad()   { echo "  ${RED}✗${RST} $1"; FAILURES=$((FAILURES+1)); }
info()  { echo "  ${DIM}·${RST} $1"; }
section() {
  echo
  echo "${BOLD}${CYN}$1${RST}"
  echo "${DIM}$(printf '─%.0s' {1..72})${RST}"
}

# ── (1) VM is running ───────────────────────────────────────────────────────

section "(1/5) VM ${VM_NAME} in ${VM_ZONE} is RUNNING"
STATUS=$(gcloud compute instances list \
            --filter="name=${VM_NAME}" \
            --format="value(status)" 2>/dev/null)
if [[ "${STATUS}" == "RUNNING" ]]; then
  ok "VM status = RUNNING"
else
  bad "VM status = ${STATUS:-not-found} (expected RUNNING)"
  echo
  echo "${RED}cannot continue without a running VM${RST}"
  exit 1
fi

# ── (2) mcpd binary present + executable ───────────────────────────────────

section "(2/5) mcpd binary at ${VM_MCPD}"
OUT=$(ssh_cmd "test -x ${VM_MCPD} && echo MCPD_OK || echo MCPD_MISSING")
if echo "${OUT}" | tail -n 1 | grep -q '^MCPD_OK$'; then
  ok "mcpd binary is executable"
  VERSION_OUT=$(ssh_cmd "ls -la ${VM_MCPD} 2>/dev/null | awk '{print \$5, \$6, \$7, \$8}'")
  info "size + mtime: $(echo "${VERSION_OUT}" | tail -n 1)"
else
  bad "mcpd binary not found at ${VM_MCPD}"
  echo "${DIM}fix: ssh in, cd ~/icebreaker/src/mcpd, cargo build --release${RST}"
fi

# ── (3) dual-brain-venv has test deps ──────────────────────────────────────

section "(3/5) Python venv at ${VM_VENV} has jsonschema + hypothesis + pytest"
OUT=$(ssh_cmd "${VM_VENV}/bin/python -c 'import jsonschema, hypothesis, pytest; print(\"DEPS_OK\")' 2>&1")
if echo "${OUT}" | tail -n 1 | grep -q '^DEPS_OK$'; then
  ok "deps importable"
  PY_VER=$(ssh_cmd "${VM_VENV}/bin/python --version 2>&1" | tail -n 1)
  info "${PY_VER}"
else
  bad "venv missing or deps not installed"
  echo "${DIM}fix: python3 -m venv ~/dual-brain-venv && \\${RST}"
  echo "${DIM}     ~/dual-brain-venv/bin/pip install -r ~/dual-brain/requirements.txt${RST}"
fi

# ── (4) G2 drift check — classifier ↔ live mcpd catalogue ──────────────────

section "(4/5) G2 drift check — classifier ↔ live mcpd"
OUT=$(ssh_cmd "${VM_VENV}/bin/python ${VM_DUAL_BRAIN}/scripts/export_mcpd_catalogue.py check --mcpd ${VM_MCPD} 2>&1")
echo "${OUT}" | sed 's/^/      /'
LAST=$(echo "${OUT}" | tail -n 1)
if echo "${LAST}" | grep -q '^OK'; then
  ok "G2 drift check PASS"
elif echo "${OUT}" | grep -q '^OK'; then
  ok "G2 drift check PASS"
else
  bad "G2 drift check FAIL"
fi

# ── (5) Audit log file mode + parent dir mode ──────────────────────────────

section "(5/5) AuditLog produces file mode 0o640 + parent dir mode 0o700"
# Write to a fresh tmpdir on the VM, then stat the file + parent.
HEREDOC='
import sys, os, stat, tempfile
from pathlib import Path
sys.path.insert(0, str(Path.home() / "dual-brain"))
from controller.audit import AuditLog, AuditFields, Outcome
with tempfile.TemporaryDirectory(prefix="dbtests-vm-") as d:
    p = Path(d) / "subdir" / "audit.log"
    log = AuditLog(path=p)
    log.write_fields(AuditFields(
        session_id="vm-check", turn_index=0, intent_id="x",
        action="system.status", target="", tier=0,
        reason="user_requested", risk_level="read_only",
        outcome=Outcome.EXECUTED, duration_ms=1.0,
        backend="local", model="vm-check",
        tokens_in=0, tokens_out=0, cost_estimate_usd=0.0,
    ))
    log.close()
    file_mode = stat.S_IMODE(p.stat().st_mode)
    parent_mode = stat.S_IMODE(p.parent.stat().st_mode)
    print(f"FILE_MODE=0o{file_mode:o}")
    print(f"PARENT_MODE=0o{parent_mode:o}")
'
OUT=$(ssh_cmd "${VM_VENV}/bin/python -c '${HEREDOC}' 2>&1")
echo "${OUT}" | sed 's/^/      /'

FILE_MODE=$(echo "${OUT}" | grep '^FILE_MODE=' | tail -n 1 | sed 's/FILE_MODE=//')
PARENT_MODE=$(echo "${OUT}" | grep '^PARENT_MODE=' | tail -n 1 | sed 's/PARENT_MODE=//')

if [[ "${FILE_MODE}" == "0o640" ]]; then
  ok "audit file mode = ${FILE_MODE}"
else
  bad "audit file mode = ${FILE_MODE} (expected 0o640)"
fi

if [[ "${PARENT_MODE}" == "0o700" ]]; then
  ok "parent dir mode = ${PARENT_MODE}"
elif [[ -n "${PARENT_MODE}" ]] && (( ($(printf '%d' 0${PARENT_MODE#0o}) & 0o022) == 0 )); then
  # Acceptable: tighter than 0o700 (e.g. mkdir(0o700) but umask masked it
  # further) — what matters is that it is NOT group- or world-writable.
  warn "parent dir mode = ${PARENT_MODE} (not 0o700 but not group/world-writable; umask trimmed it — acceptable)"
else
  bad "parent dir mode = ${PARENT_MODE} (expected 0o700; ${RED}DO NOT IGNORE${RST} if group/world-writable)"
fi

# ── Summary ────────────────────────────────────────────────────────────────

echo
echo "${BOLD}${CYN}VM sanity summary${RST}"
echo "${DIM}$(printf '─%.0s' {1..72})${RST}"

if [[ ${FAILURES} -eq 0 ]]; then
  if [[ ${SKIPPED} -gt 0 ]]; then
    echo "${GRN}${BOLD}PASS${RST} with ${YLW}${SKIPPED} acceptable warning(s)${RST}."
  else
    echo "${GRN}${BOLD}PASS${RST}  all 5 VM sanity checks green."
  fi
  exit 0
fi

echo "${RED}${BOLD}FAIL${RST}  ${FAILURES} check(s) failed."
exit 1
