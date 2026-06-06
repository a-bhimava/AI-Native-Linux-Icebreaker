#!/usr/bin/env bash
# 01_pytest_local.sh — run the automated controller test suite locally.
#
# Detects whether a project venv exists at ~/dual-brain-venv (VM) and
# activates it; otherwise falls back to system python3 (Mac developer).
# Surfaces the pytest summary line prominently and exits non-zero on
# any failure.
#
# Flags:
#   --quick        skip hypothesis fuzz (-k "not fuzz")
#   --verbose|-v   verbose pytest output

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUAL_BRAIN="$(cd "${HERE}/.." && pwd)"

CYN=$'\033[36m'; GRN=$'\033[32m'; RED=$'\033[31m'; YLW=$'\033[33m'
DIM=$'\033[2m'; BOLD=$'\033[1m'; RST=$'\033[0m'

QUICK=0
VERBOSE=0
for arg in "$@"; do
  case "$arg" in
    --quick)         QUICK=1 ;;
    --verbose|-v)    VERBOSE=1 ;;
    *) echo "${RED}unknown flag: ${arg}${RST}" >&2; exit 2 ;;
  esac
done

# ── Python selection ────────────────────────────────────────────────────────

PY=""
if [[ -x "${HOME}/dual-brain-venv/bin/python" ]]; then
  PY="${HOME}/dual-brain-venv/bin/python"
  echo "${CYN}python${RST}  ${DIM}venv:${RST}  ${PY}"
else
  PY="$(command -v python3 || true)"
  if [[ -z "${PY}" ]]; then
    echo "${RED}FAIL${RST}  python3 not found on PATH; install Python 3.10+" >&2
    exit 2
  fi
  echo "${CYN}python${RST}  ${DIM}system:${RST} ${PY}"
fi

# Sanity-import jsonschema / hypothesis / pytest — common first-time setup gotcha.
if ! "${PY}" -c "import jsonschema, hypothesis, pytest" 2>/dev/null; then
  echo "${RED}FAIL${RST}  missing test deps. Install with:" >&2
  echo "    ${PY} -m pip install -r ${DUAL_BRAIN}/requirements.txt" >&2
  exit 2
fi

# ── Run pytest ──────────────────────────────────────────────────────────────

PYTEST_FLAGS=("--timeout=180")
if [[ "${VERBOSE}" -eq 1 ]]; then
  PYTEST_FLAGS+=("-v")
else
  PYTEST_FLAGS+=("-q")
fi
if [[ "${QUICK}" -eq 1 ]]; then
  PYTEST_FLAGS+=("-k" "not fuzz")
  echo "${YLW}--quick${RST} mode: skipping hypothesis fuzz tests"
fi

echo "${CYN}pytest${RST}  ${DIM}controller/tests/${RST}"
echo

cd "${DUAL_BRAIN}"
PYTHONPATH=. "${PY}" -m pytest controller/tests/ "${PYTEST_FLAGS[@]}"
RC=$?

echo
if [[ $RC -eq 0 ]]; then
  echo "${GRN}${BOLD}PASS${RST}  pytest exit 0"
else
  echo "${RED}${BOLD}FAIL${RST}  pytest exit ${RC}"
fi
exit $RC
