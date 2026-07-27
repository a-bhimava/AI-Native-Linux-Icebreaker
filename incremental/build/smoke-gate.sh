#!/usr/bin/env bash
# smoke-gate.sh — in-chroot assertions before an ISO is allowed to exist.
#
# Usage: smoke-gate.sh <chroot-path> <version-level 0-8>
#
# Checks are cumulative: level N runs all checks for levels 0..N.
# Exit 0 = all green. Exit 1 = at least one failure (build must abort).
# Every failure prints WHAT failed and WHY (R6 — no silent failure).

set -uo pipefail

CHROOT="${1:?usage: smoke-gate.sh <chroot> <level>}"
LEVEL="${2:?usage: smoke-gate.sh <chroot> <level>}"

# v6.13_OC Fix L' Commit 6: edition-aware smoke gate.
#   current — Textual TUI is the AI Terminal; gemini QB backend (v6.12 shape).
#   oc      — opencode's TUI is the AI Terminal; opencode_oc QB backend; no TUI package.
# Some L3 (terminal) and L5 (QB) checks are edition-specific — see per-check branches.
EDITION="${EDITION:-current}"
case "$EDITION" in
    current|oc) ;;
    *) echo "FATAL: EDITION must be current|oc, got: $EDITION" >&2; exit 2 ;;
esac

FAILURES=0
pass() { echo -e "  \033[0;32m[PASS]\033[0m $*"; }
fail() { echo -e "  \033[0;31m[FAIL]\033[0m $*"; FAILURES=$((FAILURES+1)); }
check_file() { [ -f "${CHROOT}$1" ] && pass "$1" || fail "$1 missing: $2"; }
check_exec() { [ -x "${CHROOT}$1" ] && pass "$1 (executable)" || fail "$1 missing/not executable: $2"; }
in_chroot()  { chroot "$CHROOT" bash -c "$1" >/dev/null 2>&1; }

VENV_PY="/opt/icebreaker/venv/bin/python3"

echo "── Smoke gate: level ${LEVEL}, edition=${EDITION} ──"

# ═══ Level 0: bootable GNOME base ═══
echo "[L0] Base system"
ls "${CHROOT}/boot/vmlinuz-"* >/dev/null 2>&1 && pass "kernel present" || fail "no vmlinuz in /boot"
ls "${CHROOT}/boot/initrd.img-"* >/dev/null 2>&1 && pass "initrd present" || fail "no initrd in /boot"
check_file /etc/gdm3/custom.conf "GDM autologin not configured"
grep -q "AutomaticLogin=icebreaker" "${CHROOT}/etc/gdm3/custom.conf" 2>/dev/null \
    && pass "autologin=icebreaker" || fail "AutomaticLogin missing from gdm3/custom.conf"
[ -L "${CHROOT}/etc/systemd/system/display-manager.service" ] \
    && pass "display-manager enabled" || fail "gdm not enabled (no display-manager.service symlink)"
in_chroot "id icebreaker" && pass "icebreaker user exists" || fail "icebreaker user missing"
in_chroot "id icebreaker | grep -q icebreaker-users" \
    && pass "user in icebreaker-users group (PKG-4)" || fail "icebreaker not in icebreaker-users group"
check_file /etc/icebreaker-version "version marker not written"

# ═══ Level 1: SSH + diagnostics ═══
if [ "$LEVEL" -ge 1 ]; then
    echo "[L1] SSH + ib-debug"
    in_chroot "systemctl is-enabled ssh" && pass "sshd enabled" || fail "ssh.service not enabled"
    check_exec /usr/local/bin/ib-debug "ib-debug not installed"
    in_chroot "python3 /usr/local/bin/ib-debug --help" \
        && pass "ib-debug --help runs" || fail "ib-debug crashes on --help (check python3-rich etc.)"
fi

# ═══ Level 2: controller daemon + venv ═══
if [ "$LEVEL" -ge 2 ]; then
    echo "[L2] Controller + venv"
    check_exec "$VENV_PY" "venv not created at /opt/icebreaker/venv"
    in_chroot "$VENV_PY -c 'import controller'" \
        && pass "import controller" || fail "controller package not importable in venv"
    in_chroot "$VENV_PY -m controller --help" \
        && pass "controller --help" || fail "python3 -m controller --help crashes"
    # PKG-1: package data must be physically present in site-packages
    SP="$(chroot "$CHROOT" bash -c "$VENV_PY -c 'import controller,os;print(os.path.dirname(controller.__file__))'" 2>/dev/null || true)"
    if [ -n "$SP" ]; then
        for d in schemas prompts; do
            [ -d "${CHROOT}${SP}/${d}" ] && pass "package data: ${d}/" || fail "PKG-1: ${SP}/${d} missing — pip does not ship data files"
        done
        [ -f "${CHROOT}${SP}/catalogue.toml" ] && pass "package data: catalogue.toml" || fail "PKG-1: catalogue.toml missing"
    else
        fail "cannot locate controller package dir"
    fi
    check_file /etc/systemd/system/icebreaker-controller.service "controller unit not installed"
    [ -L "${CHROOT}/etc/systemd/system/multi-user.target.wants/icebreaker-controller.service" ] \
        && pass "controller unit enabled" || fail "controller unit not enabled in multi-user.target.wants"
    # F-91 regression guard: LogsDirectory must include `mcpd` so the shipped
    # unit auto-creates /var/log/mcpd for the OC edition audit bridge. Even in
    # the current edition the dir is harmless (empty when the bridge is off);
    # this assertion blocks a future edit that reverts to LogsDirectory=icebreaker
    # alone and re-introduces the v6.13_OC crash-loop.
    grep -qE '^LogsDirectory=.*\bmcpd\b' "${CHROOT}/etc/systemd/system/icebreaker-controller.service" \
        && pass "F-91: LogsDirectory includes mcpd (bridge audit dir writable)" \
        || fail "F-91 regression: LogsDirectory missing mcpd — OC daemon will crash-loop on /var/log/mcpd absent"
    check_file /etc/icebreaker/controller.toml "system config missing"
    in_chroot "python3 -c \"import tomllib;tomllib.load(open('/etc/icebreaker/controller.toml','rb'))\"" \
        && pass "controller.toml parses" || fail "controller.toml is not valid TOML"
    in_chroot "command -v systemd-analyze && systemd-analyze verify /etc/systemd/system/icebreaker-*.service" \
        && pass "systemd-analyze verify" || echo "  [SKIP] systemd-analyze verify (not available in chroot)"
    # Daemon spawns mcpd unconditionally — required from V2 (see v2.manifest).
    check_exec /usr/libexec/icebreaker/mcpd "mcpd binary missing — daemon will crash-loop"
    in_chroot "strings /usr/libexec/icebreaker/mcpd | grep -q MCPD_FS_TEST_ROOTS" \
        && fail "mcpd built with test-only feature (CLAUDE.md Test-Only Knobs)" || pass "mcpd has no test features"
    check_exec /usr/libexec/icebreaker/wait-for-sockets "ExecStartPre helper missing"
    grep -q "ICEBREAKER_SOCKET_TIMEOUT=0" "${CHROOT}/etc/icebreaker/locations.env" 2>/dev/null \
        && pass "socket wait disabled (no pbd yet)" || { [ "$LEVEL" -ge 6 ] && pass "socket wait enabled (pbd present)" || fail "ICEBREAKER_SOCKET_TIMEOUT=0 missing — controller start stalls 60 s"; }
    [ -f "${CHROOT}/home/icebreaker/.ssh/authorized_keys" ] \
        && pass "authorized_keys baked (inner loop)" || echo "  [WARN] no authorized_keys — ib-update needs a password"
fi

# ═══ Level 3: AI Terminal (edition-branched) ═══
if [ "$LEVEL" -ge 3 ]; then
    echo "[L3] AI Terminal (edition=${EDITION})"
    if [ "$EDITION" = "current" ]; then
        # Current edition: Textual TUI ships as the AI Terminal.
        in_chroot "$VENV_PY -c 'import terminal'" \
            && pass "import terminal" || fail "terminal package not importable"
        in_chroot "$VENV_PY -c 'import textual'" \
            && pass "import textual" || fail "textual not in venv"
        TSP="$(chroot "$CHROOT" bash -c "$VENV_PY -c 'import terminal,os;print(os.path.dirname(terminal.__file__))'" 2>/dev/null || true)"
        [ -n "$TSP" ] && [ -f "${CHROOT}${TSP}/styles.tcss" ] \
            && pass "styles.tcss present" || echo "  [WARN] styles.tcss missing (fallback CSS covers it — F-8 area)"
        check_file /usr/share/applications/icebreaker-terminal.desktop "terminal .desktop missing"
    else
        # OC edition: opencode TUI ships as the AI Terminal; Textual removed.
        # NB: opencode's binary at /usr/bin/opencode is a symlink to an npm
        # global-prefix path (e.g. /usr/lib/node_modules/.bin/opencode). That
        # target resolves correctly inside the chroot but NOT from the operator
        # rootfs, so a plain `[ -x ${CHROOT}/usr/bin/opencode ]` (check_exec)
        # false-fails. Test executability INSIDE the chroot instead.
        in_chroot "[ -x /usr/bin/opencode ] || command -v opencode >/dev/null" \
            && pass "opencode binary present on PATH" \
            || fail "opencode binary missing (Fix L' Commit 3)"
        # opencode --version prints a bare version string; require the pinned 1.18.4.
        OC_VER="$(chroot "$CHROOT" bash -c 'opencode --version 2>/dev/null | tr -d "[:space:]"' || true)"
        [ "$OC_VER" = "1.18.4" ] \
            && pass "opencode pinned at 1.18.4" \
            || fail "opencode --version = '$OC_VER' (expected 1.18.4 per Fix L' Commit 3)"
        # Textual TUI must be ABSENT in OC edition (Fix L' Commit 2).
        in_chroot "$VENV_PY -c 'import terminal' 2>/dev/null" \
            && fail "terminal package still present in OC edition — Fix L' Commit 2 regression" \
            || pass "terminal package removed from OC venv (Fix L' Commit 2)"
        [ -f "${CHROOT}/usr/share/applications/icebreaker-terminal.desktop" ] \
            && fail "old icebreaker-terminal.desktop still present in OC edition — Fix L' Commit 2 regression" \
            || pass "old icebreaker-terminal.desktop removed from OC edition"
        check_file /usr/share/applications/icebreaker-ai-terminal.desktop "OC AI Terminal .desktop missing"
        check_exec /usr/libexec/icebreaker/mcpd-for-oc.sh "mcpd-for-oc.sh wrapper missing (Fix L' Commit 4)"
        # F-96 regression guard: icebreaker-oc-terminal wrapper must ship
        # alongside icebreaker-oc so the .desktop's Exec= (which invokes
        # icebreaker-oc-terminal) resolves. Without it, gnome-terminal
        # opens with a "command not found" that also flashes past user.
        check_exec /usr/bin/icebreaker-oc-terminal "icebreaker-oc-terminal wrapper missing (F-96/F-94 fix)"
        # F-93 regression guard: launcher MUST NOT invoke opencode with
        # --config (unknown flag in opencode 1.18.4, dumps help + exits).
        # Must use OPENCODE_CONFIG env var instead.
        if grep -qE '^\s*exec\s+opencode\s+--config\b' "${CHROOT}/usr/bin/icebreaker-oc"; then
            fail "F-93 regression: /usr/bin/icebreaker-oc uses \`opencode --config\` (unknown flag in 1.18.4; use OPENCODE_CONFIG env var)"
        else
            pass "F-93: launcher does not pass --config to opencode"
        fi
        grep -q 'OPENCODE_CONFIG' "${CHROOT}/usr/bin/icebreaker-oc" \
            && pass "F-93: launcher exports OPENCODE_CONFIG env var" \
            || fail "F-93 regression: launcher missing OPENCODE_CONFIG export"
        check_file /etc/icebreaker/qb_oc.json "qb_oc.json missing (Fix L' Commit 4)"
        in_chroot "python3 -c \"import json;json.load(open('/etc/icebreaker/qb_oc.json'))\"" \
            && pass "qb_oc.json parses as JSON" || fail "qb_oc.json is not valid JSON"
    fi
    check_exec /usr/libexec/icebreaker/ib-wait-sock "ib-wait-sock helper missing (F-9)"
    if in_chroot "command -v desktop-file-validate"; then
        in_chroot "desktop-file-validate /usr/share/applications/icebreaker-*.desktop" \
            && pass "desktop-file-validate" || fail "a .desktop file is invalid"
    fi
fi

# ═══ Level 4: # trigger ═══
if [ "$LEVEL" -ge 4 ]; then
    echo "[L4] # trigger"
    check_file /usr/share/icebreaker/shell/ib_trigger.bash "trigger script not installed"
    check_file /usr/share/icebreaker/shell/ib_run.py "ib_run.py runner not installed"
    in_chroot "bash -n /usr/share/icebreaker/shell/ib_trigger.bash" \
        && pass "ib_trigger.bash syntax OK" || fail "ib_trigger.bash has a bash syntax error"
    in_chroot "/opt/icebreaker/venv/bin/python3 -m py_compile /usr/share/icebreaker/shell/ib_run.py" \
        && pass "ib_run.py compiles" || fail "ib_run.py has a syntax error"
    grep -q "ib_trigger.bash" "${CHROOT}/etc/skel/.bashrc" 2>/dev/null \
        && pass "skel .bashrc sources trigger" || fail "trigger not in /etc/skel/.bashrc"
    grep -q "ib_trigger.bash" "${CHROOT}/home/icebreaker/.bashrc" 2>/dev/null \
        && pass "icebreaker .bashrc sources trigger (F-6)" || fail "F-6: trigger not in /home/icebreaker/.bashrc — skel was copied at useradd time"
fi

# ═══ Level 5: QB backend (edition-branched) ═══
if [ "$LEVEL" -ge 5 ]; then
    echo "[L5] QB backend (edition=${EDITION})"
    if [ "$EDITION" = "current" ]; then
        grep -q 'backend *= *"gemini"' "${CHROOT}/etc/icebreaker/controller.toml" 2>/dev/null \
            && pass "QB backend=gemini configured" || fail "controller.toml missing gemini QB config"
    else
        # OC edition: controller flips QB to the no-op opencode_oc backend (Fix Q + Fix L' Commit 5).
        grep -q 'backend *= *"opencode_oc"' "${CHROOT}/etc/icebreaker/controller.toml" 2>/dev/null \
            && pass "QB backend=opencode_oc configured (Fix L' Commit 5)" \
            || fail "controller.toml missing opencode_oc backend (Fix L' Commit 5)"
        grep -q '^\[qb\.opencode_oc\]' "${CHROOT}/etc/icebreaker/controller.toml" 2>/dev/null \
            && pass "[qb.opencode_oc] section present" \
            || fail "[qb.opencode_oc] section missing from controller.toml (Fix Q)"
    fi
    check_file /etc/icebreaker/locations.env "locations.env missing (API key env file)"
    check_exec /usr/local/bin/ib-setup-key "ib-setup-key not installed"
    PERMS="$(stat -c '%a' "${CHROOT}/etc/icebreaker/locations.env" 2>/dev/null || echo '')"
    # F-22: 0640 root:icebreaker-users — world unreadable (BP-8) AND pbd/qbd
    # can source the env file via group membership.
    [ "$PERMS" = "640" ] \
        && pass "locations.env is 0640 (BP-8 + F-22)" \
        || fail "F-22/BP-8: locations.env perms '$PERMS' != 640 (needs group read for pbd)"
    grep -q "GEMINI_API_KEY" "${CHROOT}/etc/icebreaker/locations.env" 2>/dev/null \
        && pass "key template present in locations.env" || fail "GEMINI_API_KEY template missing from locations.env"
    # BP-8: no REAL key may ever ship in the ISO (template line is commented).
    grep -qE '^GEMINI_API_KEY=' "${CHROOT}/etc/icebreaker/locations.env" 2>/dev/null \
        && fail "BP-8 VIOLATION: uncommented GEMINI_API_KEY baked into the ISO" \
        || pass "no live API key in the image (BP-8)"
fi

# ═══ Level 6: PB + mcpd ═══
if [ "$LEVEL" -ge 6 ]; then
    echo "[L6] PB + mcpd"
    check_exec /usr/libexec/icebreaker/mcpd "mcpd binary missing"
    check_exec /usr/libexec/icebreaker/llama-server "llama-server missing"
    check_exec /usr/libexec/icebreaker/start-pbd "start-pbd missing"
    check_file /etc/systemd/system/icebreaker-pbd.service "pbd unit missing"
    MODEL_OK=0
    for m in "${CHROOT}/var/lib/icebreaker/models/"*.gguf; do
        [ -f "$m" ] && MODEL_OK=1 && break
    done
    [ "$MODEL_OK" = "1" ] && pass "PB model embedded" || fail "no .gguf in /var/lib/icebreaker/models (INV-7)"
    check_file /var/lib/icebreaker/models/checksums.sha256 "model checksums manifest missing (INV-7)"
    # INV-7: verify the EMBEDDED model against the EMBEDDED manifest (the pair
    # that actually ships — defense in depth over the manifest's source check).
    in_chroot "cd /var/lib/icebreaker/models && sha256sum -c checksums.sha256 --quiet" \
        && pass "embedded model checksum verified (INV-7)" \
        || fail "INV-7: embedded model fails checksum against embedded manifest"
    # F-21: llama-server must have zero unresolved shared libs AND must exec.
    in_chroot "ldd /usr/libexec/icebreaker/llama-server | grep -q 'not found'" \
        && fail "F-21: llama-server has unresolved shared libraries in the image" \
        || pass "llama-server shared libs all resolve (F-21)"
    in_chroot "/usr/libexec/icebreaker/llama-server --version" \
        && pass "llama-server executes (--version)" \
        || fail "F-21: llama-server does not execute in the chroot"
    [ -L "${CHROOT}/etc/systemd/system/multi-user.target.wants/icebreaker-pbd.service" ] \
        && pass "pbd unit enabled" || fail "icebreaker-pbd.service not enabled"
    grep -q "^ICEBREAKER_SOCKET_TIMEOUT=0$" "${CHROOT}/etc/icebreaker/locations.env" 2>/dev/null \
        && fail "v2's SOCKET_TIMEOUT=0 bypass still present — v6 must revert it" \
        || pass "wait-for-sockets bypass reverted"
    grep -q "_icebreaker_pb" "${CHROOT}/etc/sysusers.d/icebreaker.conf" 2>/dev/null \
        && pass "_icebreaker_pb in sysusers.d" || fail "_icebreaker_pb missing from sysusers.d (pbd unit will fail)"
    # INV-3 static check: mcpd must not link TCP listeners — verified at runtime by qemu-gate
    in_chroot "strings /usr/libexec/icebreaker/mcpd | grep -q MCPD_FS_TEST_ROOTS" \
        && fail "mcpd built with test-only feature (CLAUDE.md Test-Only Knobs)" || pass "mcpd has no test features"
    # V6.3 positive assertions on the controller unit — pin the F-30/F-31
    # sandbox config so future edits can't silently regress.
    UNIT="${CHROOT}/etc/systemd/system/icebreaker-controller.service"
    grep -q "^Environment=HOME=/home/icebreaker$" "$UNIT" 2>/dev/null \
        && pass "controller unit: Environment=HOME=/home/icebreaker (F-28)" \
        || fail "controller unit missing 'Environment=HOME=/home/icebreaker' (F-28)"
    grep -q "^BindPaths=/home/icebreaker$" "$UNIT" 2>/dev/null \
        && pass "controller unit: BindPaths=/home/icebreaker writable (F-31)" \
        || fail "controller unit missing 'BindPaths=/home/icebreaker' (F-31: fs.write inside home fails EROFS with BindReadOnlyPaths)"
    grep -q "^ProtectHome=tmpfs$" "$UNIT" 2>/dev/null \
        && pass "controller unit: ProtectHome=tmpfs (F-30)" \
        || fail "controller unit missing 'ProtectHome=tmpfs' (F-30: ProtectHome=yes hides /home entirely)"
    # V6.3 Stage 3: wmctrl for best-effort active-window context in the trigger
    check_exec /usr/bin/wmctrl "wmctrl not installed (v6.manifest VERSION_PACKAGES=wmctrl)"
    # V6.3: mcpd binary must carry the config-driven read-roots env var symbol
    in_chroot "strings /usr/libexec/icebreaker/mcpd | grep -q MCPD_FS_READ_ROOTS" \
        && pass "mcpd honours MCPD_FS_READ_ROOTS (F-28 baked in)" \
        || fail "mcpd binary lacks MCPD_FS_READ_ROOTS — rebuild from F-28 source"
    # V6.4 (F-33): mcpd must link fsync — safe_write / canonicalize_write call
    # file.sync_all() which translates to fsync(2). Without SYS_fsync in the
    # seccomp allowlist, the write path dies with SIGSYS mid-response.
    # The real invariant is the allowlist entry (locked by cargo unit test);
    # this strings check is a cheap defense-in-depth signal that the write
    # code path is actually in the shipped binary.
    in_chroot "strings /usr/libexec/icebreaker/mcpd | grep -qE '(^|[^a-z])fsync([^a-z]|$)'" \
        && pass "mcpd links fsync (F-33 write path present)" \
        || fail "mcpd binary lacks fsync symbol — safe_write dropped, or wrong binary shipped (F-33)"
    # G1 / F-55 (Scope G, 2026-07-11): mcpd's seccomp allowlist must include
    # the faccessat / faccessat2 syscalls — arm64 kernels don't implement
    # access(2), so glibc routes access() through faccessat. Without either,
    # `service.logs` invocations SIGSYS-kill journalctl.
    #
    # 2026-07-13: `libc::SYS_faccessat` compiles to a numeric constant, so
    # the string `faccessat` does NOT appear in the stripped release binary.
    # Verify at the SOURCE layer instead — the v2.manifest F-51 marker
    # `F55-arm64-facc:src/mcpd/src/sandbox/seccomp.rs:SYS_faccessat` already
    # covers this, but repeat it here as a smoke-gate signal so a mismatch
    # between shipped mcpd and source tree is caught before ISO tag.
    if grep -q "SYS_faccessat" "${REPO_ROOT:-$(pwd)}/src/mcpd/src/sandbox/seccomp.rs" 2>/dev/null; then
        pass "mcpd source has SYS_faccessat (G1 / F-55 arm64 seccomp fix present)"
    else
        fail "src/mcpd/src/sandbox/seccomp.rs missing SYS_faccessat — F-55 fix reverted"
    fi
    # G5 / F-55 (Scope G, 2026-07-11): the in-guest harvest runner must be
    # present at /usr/local/bin/mcpd-harvest-guest.sh so qemu-gate L6 can
    # exercise real-kernel seccomp inside the booted VM.
    [ -x "${CHROOT}/usr/local/bin/mcpd-harvest-guest.sh" ] \
        && pass "mcpd-harvest-guest.sh installed (G5 in-guest harvest runner)" \
        || fail "mcpd-harvest-guest.sh missing — qemu-gate L6 in-guest harvest cannot run"
    # F-57 / G.5 (Scope G.5, 2026-07-12): the terminal + chatbot autostart
    # .desktop files were deleted because they auto-launched at every login
    # BEFORE the user could configure API keys. This assertion fires if
    # either file is reintroduced under /etc/xdg/autostart/.
    if ls "${CHROOT}"/etc/xdg/autostart/icebreaker-terminal*.desktop 2>/dev/null || \
       ls "${CHROOT}"/etc/xdg/autostart/icebreaker-chatbot*.desktop 2>/dev/null; then
        fail "F-57 regression: terminal or chatbot autostart .desktop reintroduced under /etc/xdg/autostart/"
    else
        pass "no terminal/chatbot autostart .desktop under /etc/xdg/autostart/ (F-57)"
    fi
    # ib-bundle diagnostic collector must be installed as an executable
    # so the user can produce a self-service diagnostic tarball.
    [ -x "${CHROOT}/usr/local/bin/ib-bundle" ] \
        && pass "ib-bundle installed (G.5 diagnostic bundle collector)" \
        || fail "ib-bundle missing — user cannot produce a self-service diag bundle"
    # F-36 / R10 (revised): AVX2 is the minimum CPU. Enforce that llama-server
    # doesn't accidentally start requiring AVX-512 (which Rosetta 2 doesn't
    # support — F-24). ymm/AVX2 references are expected and fine.
    if command -v objdump >/dev/null 2>&1; then
        # grep -c always emits a single number; || : swallows non-zero exit
        # when count is 0 without polluting the captured stdout (F-24 bug).
        ZMM_COUNT=$(objdump -d "${CHROOT}/usr/libexec/icebreaker/llama-server" 2>/dev/null | grep -cE '\bzmm[0-9]+\b' || :)
        ZMM_COUNT="${ZMM_COUNT:-0}"
        if [ "${ZMM_COUNT}" = "0" ]; then
            pass "llama-server has no AVX-512 (F-24: Rosetta 2 compatible)"
        else
            fail "F-24: llama-server contains ${ZMM_COUNT} zmm references — will SIGILL under Rosetta 2. Rebuild without -DGGML_AVX512=ON"
        fi
    fi
fi

# ═══ Level 7: chatbot GUI ═══
if [ "$LEVEL" -ge 7 ]; then
    echo "[L7] Chatbot GUI"
    in_chroot "$VENV_PY -c \"import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')\"" \
        && pass "GTK4 + Adw importable in venv (F-10)" || fail "F-10: gi/Gtk4/Adw import fails — check --system-site-packages + apt gir packages"
    in_chroot "$VENV_PY -c 'import gui'" \
        && pass "import gui" || fail "gui package not importable"
    check_file /usr/share/applications/icebreaker-chatbot.desktop "chatbot .desktop missing"
fi

echo "──────────────────────────────"
if [ "$FAILURES" -gt 0 ]; then
    echo -e "\033[0;31mSMOKE GATE: ${FAILURES} FAILURE(S)\033[0m"
    exit 1
fi
echo -e "\033[0;32mSMOKE GATE: ALL GREEN (level ${LEVEL})\033[0m"
exit 0
