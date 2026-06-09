#!/usr/bin/env bash
# run_all.sh — orchestrator: runs every dbtests check in order, prints
# a color-coded summary, exits non-zero if any check failed.
#
# Steps:
#   [1/4] pytest                (01_pytest_local.sh)
#   [2/4] cross-module smoke    (02_cross_module_smoke.py)
#   [3/4] schema × catalogue    (03_schema_parity.py)
#   [4/4] VM sanity             (04_vm_check.sh) — skipped if SKIP_VM=1
#                                                  or gcloud is missing
#
# Flags:
#   --quick           passed through to 01 (skips hypothesis fuzz)
#
# Env:
#   SKIP_VM=1         skip step 4 even if gcloud is available
#
# Exit code 0 = all steps passed (skipped steps don't count as failure).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}"

CYN=$'\033[36m'; GRN=$'\033[32m'; RED=$'\033[31m'; YLW=$'\033[33m'
DIM=$'\033[2m'; BOLD=$'\033[1m'; RST=$'\033[0m'

PASS_LABEL="${GRN}PASS${RST}"
FAIL_LABEL="${RED}FAIL${RST}"
SKIP_LABEL="${YLW}SKIP${RST}"

# Sub-script flags pass-through
SUB_FLAGS=()
for arg in "$@"; do
  SUB_FLAGS+=("$arg")
done

step() {
  local n="$1" total="$2" label="$3"
  echo
  echo "${BOLD}${CYN}[${n}/${total}] ${label}${RST}"
  echo "${DIM}$(printf '─%.0s' {1..72})${RST}"
}

run_step() {
  local cmd=("$@")
  if "${cmd[@]}"; then return 0; else return 1; fi
}

# ── Step 1 — pytest ─────────────────────────────────────────────────────────

step 1 4 "pytest unit + integration  (01_pytest_local.sh)"
if bash "${HERE}/01_pytest_local.sh" "${SUB_FLAGS[@]}"; then
  R1=pass
else
  R1=fail
fi

# ── Step 2 — cross-module smoke ─────────────────────────────────────────────

step 2 4 "cross-module integration smoke  (02_cross_module_smoke.py)"
if python3 "${HERE}/02_cross_module_smoke.py"; then
  R2=pass
else
  R2=fail
fi

# ── Step 3 — schema × catalogue parity ──────────────────────────────────────

step 3 4 "schema × catalogue parity  (03_schema_parity.py)"
if python3 "${HERE}/03_schema_parity.py"; then
  R3=pass
else
  R3=fail
fi

# ── Step 4 — VM sanity ──────────────────────────────────────────────────────

step 4 4 "VM sanity  (04_vm_check.sh)"
if [[ "${SKIP_VM:-0}" -eq 1 ]]; then
  echo "${YLW}SKIP_VM=1 set — skipping VM check${RST}"
  R4=skip
elif ! command -v gcloud >/dev/null 2>&1; then
  echo "${YLW}gcloud not on PATH — skipping VM check${RST}"
  echo "${DIM}install: https://cloud.google.com/sdk/docs/install${RST}"
  R4=skip
else
  if bash "${HERE}/04_vm_check.sh"; then
    R4=pass
  else
    R4=fail
  fi
fi

# ── Summary ────────────────────────────────────────────────────────────────

echo
echo "${BOLD}${CYN}Summary${RST}"
echo "${DIM}$(printf '─%.0s' {1..72})${RST}"

label_for() {
  case "$1" in
    pass) echo "${PASS_LABEL}" ;;
    fail) echo "${FAIL_LABEL}" ;;
    skip) echo "${SKIP_LABEL}" ;;
  esac
}

printf "  [1/4] %s  pytest\n"                  "$(label_for "$R1")"
printf "  [2/4] %s  cross-module smoke\n"      "$(label_for "$R2")"
printf "  [3/4] %s  schema × catalogue parity\n" "$(label_for "$R3")"
printf "  [4/4] %s  VM sanity\n"               "$(label_for "$R4")"

# Fail if any step failed (skip is not a failure).
for r in "$R1" "$R2" "$R3" "$R4"; do
  if [[ "$r" == "fail" ]]; then
    echo
    echo "${RED}${BOLD}One or more steps failed.${RST}  See output above and dbtests/what_to_look_for.md."
    exit 1
  fi
done

echo
echo "${GRN}${BOLD}All green.${RST}  (Set SKIP_VM=0 + ensure gcloud auth to also run step 4 if you skipped it.)"
exit 0
