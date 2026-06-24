#!/usr/bin/env bash
# test_build_output.sh — Post-build and static verification for cx-distro.
#
# Usage:
#   bash test_build_output.sh                 # full checks (inside Docker after build)
#   bash test_build_output.sh --static-only   # committed-file checks (macOS-safe)
#
# Exit code 0 = all checks pass. Non-zero = at least one failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${CX_DIR}/.." && pwd)"
CHROOT="${CX_DIR}/config/includes.chroot"

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

check_file_exists() {
    check "file exists: $1" test -f "$1"
}

STATIC_ONLY=0
if [ "${1:-}" = "--static-only" ]; then
    STATIC_ONLY=1
fi

# ── Static checks (always run, macOS-safe) ─────────────────────────────

echo "=== Static checks ==="

# Shell syntax.
check "build.sh syntax" bash -n "${CX_DIR}/build.sh"
check "icebreaker-cli syntax" bash -n "${CX_DIR}/distro/icebreaker-cli"
check "chroot hook syntax" bash -n "${CX_DIR}/config/hooks/live/0100-icebreaker-setup.hook.chroot"
check "test script syntax" bash -n "${SCRIPT_DIR}/test_build_output.sh"
check "first-boot syntax" bash -n "${CX_DIR}/distro/first-boot"
check "safe-mode syntax" bash -n "${CX_DIR}/distro/safe-mode"

# Pin files.
check "LLAMA_CPP_COMMIT is 40-char hex" \
    grep -qE '^[0-9a-f]{40}$' "${CX_DIR}/LLAMA_CPP_COMMIT"
check "UBUNTU_BASE is a single lowercase word" \
    grep -qE '^[a-z]+$' "${CX_DIR}/UBUNTU_BASE"

# Distro config files exist.
check_file_exists "${CX_DIR}/distro/controller.toml"
check_file_exists "${CX_DIR}/distro/locations.env"
check_file_exists "${CX_DIR}/distro/icebreaker-cli"
check_file_exists "${CX_DIR}/config/package-lists/icebreaker.list.chroot"
check_file_exists "${CX_DIR}/config/hooks/live/0100-icebreaker-setup.hook.chroot"
check_file_exists "${CX_DIR}/Dockerfile.build"

# EFI boot support.
check "Dockerfile installs grub-efi-amd64-bin" \
    grep -q 'grub-efi-amd64-bin' "${CX_DIR}/Dockerfile.build"

# CLI wrapper uses #!/bin/sh and exec.
check "icebreaker-cli shebang is /bin/sh" \
    bash -c 'head -1 "$1" | grep -q "#!/bin/sh"' _ "${CX_DIR}/distro/icebreaker-cli"
check "icebreaker-cli uses exec" \
    grep -q '^exec ' "${CX_DIR}/distro/icebreaker-cli"

# locations.env has no shell expansion (pure KEY=VALUE for systemd EnvironmentFile).
check "locations.env has no shell expansion" \
    bash -c '! grep -qE "\\$\\(|\\$\\{|\`" "${CX_DIR}/distro/locations.env"'

# Distro config uses UNIX transport.
check "controller.toml uses unix transport for QB" \
    grep -q 'transport.*=.*"unix"' "${CX_DIR}/distro/controller.toml"
check "controller.toml uses unix transport for PB" \
    grep -q 'pb_transport.*=.*"unix"' "${CX_DIR}/distro/controller.toml"

# Package list is not empty.
check "package list has entries" \
    bash -c 'grep -cvE "^#|^$" "$1" | grep -qv "^0$"' _ "${CX_DIR}/config/package-lists/icebreaker.list.chroot"

# Distro config validates as TOML (if python3 available).
if command -v python3 >/dev/null 2>&1; then
    check "controller.toml parses as valid TOML" \
        python3 -c "
import sys
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
tomllib.loads(open('${CX_DIR}/distro/controller.toml').read())
"
fi

# ── Chroot checks (post-build only) ───────────────────────────────────

if [ "$STATIC_ONLY" -eq 1 ]; then
    echo ""
    echo "=== Static-only mode: skipping chroot/build checks ==="
else
    echo ""
    echo "=== Chroot checks ==="

    # FHS paths (files that must exist after Stage 4).
    EXPECT_644=(
        "etc/icebreaker/controller.toml"
        "etc/icebreaker/locations.env"
        "etc/sysusers.d/icebreaker.conf"
        "etc/tmpfiles.d/icebreaker.conf"
        "usr/share/icebreaker/catalogue.toml"
        "usr/share/icebreaker/grammars/qb_intent.gbnf"
        "var/lib/icebreaker/models/checksums.sha256"
    )
    for f in "${EXPECT_644[@]}"; do
        check_file_exists "${CHROOT}/${f}"
    done

    EXPECT_755=(
        "usr/bin/icebreaker"
        "usr/libexec/icebreaker/mcpd"
        "usr/libexec/icebreaker/llama-server"
        "usr/libexec/icebreaker/start-pbd"
        "usr/libexec/icebreaker/start-qbd"
        "usr/libexec/icebreaker/first-boot"
        "usr/libexec/icebreaker/safe-mode"
    )
    for f in "${EXPECT_755[@]}"; do
        check_file_exists "${CHROOT}/${f}"
        check "executable: ${f}" test -x "${CHROOT}/${f}"
    done

    # Systemd units.
    for unit in icebreaker-controller.service icebreaker-pbd.service \
                icebreaker-qbd.service icebreaker-mcpd@.service \
                icebreaker-first-boot.service \
                icebreaker-controller.socket; do
        check_file_exists "${CHROOT}/etc/systemd/system/${unit}"
    done

    # Schemas.
    check "intent.json in schemas" test -f "${CHROOT}/usr/share/icebreaker/schemas/intent.json"
    check "fs.read.json in schemas" test -f "${CHROOT}/usr/share/icebreaker/schemas/fs.read.json"

    # Prompts.
    check "pb.txt in prompts" test -f "${CHROOT}/usr/share/icebreaker/prompts/pb.txt"
    check "qb_local.txt in prompts" test -f "${CHROOT}/usr/share/icebreaker/prompts/qb_local.txt"

    # Venv.
    check "venv python3 exists" test -f "${CHROOT}/opt/icebreaker/venv/bin/python3"

    # mcpd security.
    if command -v strings >/dev/null 2>&1; then
        check "mcpd has no test features" \
            bash -c '! strings "${CHROOT}/usr/libexec/icebreaker/mcpd" | grep -q MCPD_FS_TEST_ROOTS'
    fi
fi

# ── Summary ─────────────────────────────────────────────────────────────

echo ""
echo "=== Results ==="
printf "Passed: ${GREEN}%d${NC}  Failed: ${RED}%d${NC}\n" "$PASS" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
