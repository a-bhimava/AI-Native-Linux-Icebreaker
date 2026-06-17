#!/usr/bin/env bash
# test_config_layering.sh — Integration test for 2-tier config layering.
#
# Uses the actual distro controller.toml as the system config template.
# Works on macOS (no systemd required).
#
# Exit code 0 = all checks pass. Non-zero = at least one failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${CX_DIR}/.." && pwd)"
DUAL_BRAIN="${REPO_ROOT}/dual-brain"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0

check() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        printf "${GREEN}PASS${NC}: %s\n" "$desc"
        PASS=$((PASS + 1))
    else
        printf "${RED}FAIL${NC}: %s\n" "$desc"
        FAIL=$((FAIL + 1))
    fi
}

TMPDIR_ROOT=$(mktemp -d)
trap 'rm -rf "${TMPDIR_ROOT}"' EXIT

LOCAL_TOML='[qb]
backend = "local"

[qb.local]
model_id = "qwen-2.5-1.5b-instruct-q4_k_m"
endpoint = "http://127.0.0.1:8081"
max_tokens = 512
timeout_seconds = 30
'

echo "=== Config layering integration tests ==="

# ── Test 1: System-only config loads ────────────────────────────────────
SYS1="${TMPDIR_ROOT}/sys1.toml"
echo "$LOCAL_TOML" > "$SYS1"

check "system-only config loads" \
    python3 -c "
import sys
sys.path.insert(0, '${DUAL_BRAIN}')
from unittest.mock import patch
from pathlib import Path
from controller.config import load_layered
with patch('controller.config._SYSTEM_CONFIG_PATH', Path('${SYS1}')), \
     patch('controller.config._default_config_path', lambda: Path('${TMPDIR_ROOT}/nonexistent.toml')):
    cfg = load_layered()
    assert cfg.qb.name == 'local', f'expected local, got {cfg.qb.name}'
"

# ── Test 2: User-only config loads ─────────────────────────────────────
USER2="${TMPDIR_ROOT}/user2.toml"
echo "$LOCAL_TOML" > "$USER2"

check "user-only config loads (XDG override)" \
    python3 -c "
import sys
sys.path.insert(0, '${DUAL_BRAIN}')
from unittest.mock import patch
from pathlib import Path
from controller.config import load_layered
with patch('controller.config._SYSTEM_CONFIG_PATH', Path('${TMPDIR_ROOT}/nonexistent.toml')), \
     patch('controller.config._default_config_path', lambda: Path('${USER2}')):
    cfg = load_layered()
    assert cfg.qb.name == 'local', f'expected local, got {cfg.qb.name}'
"

# ── Test 3: System+user merge ──────────────────────────────────────────
SYS3="${TMPDIR_ROOT}/sys3.toml"
USER3="${TMPDIR_ROOT}/user3.toml"

cat > "$SYS3" <<'SYS_EOF'
[qb]
backend = "local"

[qb.local]
model_id = "qwen-2.5-1.5b-instruct-q4_k_m"
endpoint = "http://127.0.0.1:8081"
max_tokens = 512
timeout_seconds = 30

[session]
max_turns = 100
show_spinner = true
SYS_EOF

cat > "$USER3" <<'USER_EOF'
[qb]
backend = "local"

[qb.local]
model_id = "qwen-2.5-1.5b-instruct-q4_k_m"
endpoint = "http://127.0.0.1:8081"
max_tokens = 512
timeout_seconds = 30

[session]
max_turns = 25
USER_EOF

check "system+user merge: user [session] replaces system [session]" \
    python3 -c "
import sys
sys.path.insert(0, '${DUAL_BRAIN}')
from unittest.mock import patch
from pathlib import Path
from controller.config import load_layered
with patch('controller.config._SYSTEM_CONFIG_PATH', Path('${SYS3}')), \
     patch('controller.config._default_config_path', lambda: Path('${USER3}')):
    cfg = load_layered()
    assert cfg.session.max_turns == 25, f'expected 25, got {cfg.session.max_turns}'
    assert cfg.qb.name == 'local', f'qb section should be preserved from system'
"

# ── Summary ────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
printf "Passed: ${GREEN}%d${NC}  Failed: ${RED}%d${NC}\n" "$PASS" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
