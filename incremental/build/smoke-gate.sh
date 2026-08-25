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
PROFILE="${PROFILE:-desktop}"
case "$PROFILE" in
    desktop|xfce-frosted) ;;
    *) echo "FATAL: PROFILE must be desktop or xfce-frosted, got: $PROFILE" >&2; exit 2 ;;
esac

FAILURES=0
pass() { echo -e "  \033[0;32m[PASS]\033[0m $*"; }
fail() { echo -e "  \033[0;31m[FAIL]\033[0m $*"; FAILURES=$((FAILURES+1)); }
check_file() { [ -f "${CHROOT}$1" ] && pass "$1" || fail "$1 missing: $2"; }
check_exec() { [ -x "${CHROOT}$1" ] && pass "$1 (executable)" || fail "$1 missing/not executable: $2"; }
in_chroot()  { chroot "$CHROOT" bash -c "$1" >/dev/null 2>&1; }

VENV_PY="/opt/icebreaker/venv/bin/python3"

echo "── Smoke gate: level ${LEVEL}, edition=${EDITION}, profile=${PROFILE} ──"

# ═══ Level 0: bootable profile base ═══
echo "[L0] Base system"
ls "${CHROOT}/boot/vmlinuz-"* >/dev/null 2>&1 && pass "kernel present" || fail "no vmlinuz in /boot"
ls "${CHROOT}/boot/initrd.img-"* >/dev/null 2>&1 && pass "initrd present" || fail "no initrd in /boot"
if [ "$PROFILE" = "desktop" ]; then
    check_file /etc/gdm3/custom.conf "GDM autologin not configured"
    grep -q "AutomaticLogin=ubuntu" "${CHROOT}/etc/gdm3/custom.conf" 2>/dev/null \
        && pass "GDM live-session autologin=ubuntu (locked account)" || fail "locked live-session autologin missing from gdm3/custom.conf"
else
    check_file /etc/lightdm/lightdm.conf.d/50-icebreaker-autologin.conf "LightDM autologin not configured"
    grep -q "^user-session=xfce$" "${CHROOT}/etc/lightdm/lightdm.conf.d/50-icebreaker-autologin.conf" 2>/dev/null \
        && pass "LightDM XFCE autologin configured" || fail "XFCE session missing from LightDM config"
    for file in xfce4-desktop.xml xfce4-panel.xml xsettings.xml xfwm4.xml xfce4-keyboard-shortcuts.xml; do
        check_file "/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/${file}" "Frosted XFCE config missing"
    done
    check_exec /usr/libexec/icebreaker/appearance-apply "Frosted session helper missing"
    check_file /etc/xdg/icebreaker/picom-frosted.conf "opt-in Picom config missing"
    check_file /usr/share/themes/MacTahoe-Dark/index.theme "MacTahoe theme not extracted"
    check_file /usr/share/backgrounds/icebreaker-frosted-graphite-wallpaper.jpeg "Frosted wallpaper missing"
    in_chroot "dpkg-query -W -f='\${db:Status-Status}' plank-reloaded | grep -qx installed" \
        && pass "Plank Reloaded installed" || fail "Plank Reloaded missing or unconfigured after dependency resolution"
fi
[ -L "${CHROOT}/etc/systemd/system/display-manager.service" ] \
    && pass "display-manager enabled" || fail "display manager not enabled (no display-manager.service symlink)"
# The live account is intentionally locked and never becomes an installed
# account.  The installer creates the user's real Ubuntu login and the
# finalizer adds that account to icebreaker-users after installation.
in_chroot "getent group icebreaker-users" \
    && pass "icebreaker-users group exists" || fail "icebreaker-users group missing"
in_chroot "id ubuntu | grep -q icebreaker-users" \
    && pass "locked live account is in icebreaker-users" || fail "ubuntu live account missing icebreaker-users"
in_chroot "test \"\$(passwd -S ubuntu | awk '{print \$2}')\" = L" \
    && pass "live account password is locked" || fail "ubuntu live account is not locked"
check_exec /usr/libexec/icebreaker/icebreaker-installer-first "installer-first helper missing"
check_file /etc/xdg/autostart/icebreaker-installer-first.desktop "installer-first autostart missing"
grep -q '^Exec=/usr/libexec/icebreaker/icebreaker-installer-first$' \
    "${CHROOT}/etc/xdg/autostart/icebreaker-installer-first.desktop" \
    && pass "installer-first autostart invokes installer helper" \
    || fail "installer-first autostart does not invoke installer helper"
check_file /etc/xdg/autostart/icebreaker-onboarding.desktop "post-install onboarding autostart missing"
if grep -q '^OnlyShowIn=' "${CHROOT}/etc/xdg/autostart/icebreaker-onboarding.desktop"; then
    fail "post-install onboarding is desktop-specific; it must run on XFCE and GNOME"
else
    pass "post-install onboarding is desktop-neutral"
fi
check_exec /usr/local/bin/ice "explicit offline command helper missing"
check_file /etc/icebreaker-version "version marker not written"
if [ "$(cat "${CHROOT}/etc/icebreaker-build-profile" 2>/dev/null || true)" = "$PROFILE" ]; then
    pass "build profile marker = ${PROFILE}"
else
    fail "build profile marker missing or incorrect"
fi

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
    check_file /etc/icebreaker/credentials.env "root-only credentials template missing"
    in_chroot "test \$(stat -c '%a:%U:%G' /etc/icebreaker/credentials.env) = 600:root:root" \
        && pass "credentials.env root-only" || fail "credentials.env must be 0600 root:root"
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
        # F-101 (2026-07-27): iceui MCP server wrapper + udev rule.
        check_exec /usr/libexec/icebreaker/gui-mcp-for-oc.sh "gui-mcp-for-oc.sh wrapper missing (F-101)"
        [ -f "${CHROOT}/etc/udev/rules.d/10-uinput.rules" ] \
            && pass "F-101: uinput udev rule installed" \
            || fail "F-101: /etc/udev/rules.d/10-uinput.rules missing — RPA input synthesis will fail"
        # iceui MCP server self-test — verifies handshake + tool count.
        # v6.14 shipped 12; v6.15 Fix V.4 added 12 → 24; v6.17 M7.6a-1a
        # added submit_intent → 25.
        # Cannot use in_chroot() here because it discards stdout+stderr,
        # so the pipe to grep would always read empty and fail (F-101.2).
        # Run chroot directly so the self-test output (which goes to
        # stderr via file=sys.stderr) reaches grep.
        if chroot "$CHROOT" bash -c "$VENV_PY -m controller.mcp_gui_server --self-test 2>&1" \
                | grep -q "25 tools"; then
            pass "F-111: iceui MCP server --self-test OK (25 tools incl. submit_intent)"
        else
            fail "F-111: iceui MCP server self-test failed — expected '25 tools' (24 gui/rpa + submit_intent per v6.17 M7.6a-1a). Count mismatch = drift"
        fi

        # ── F-103 (Fix V.6c): vision-actuation backend + config knobs ──
        check_exec /usr/bin/xdotool "F-103: xdotool missing — Fix V input_synth cannot spawn mouse+kb events (packages-desktop.txt regression)"

        # F-105 (Fix V.3c): trust store defaults JSONL must be shipped
        # in the ISO so the trust-store auto-approves the safe read-only
        # tool set (gui.hover, gui.parse_screen, etc.) instead of every
        # single grounded action prompting HITL.
        [ -f "${CHROOT}/etc/icebreaker/gui_trust.d/defaults.jsonl" ] \
            || [ -f "${CHROOT}/etc/icebreaker/gui_trust.d/00-defaults.jsonl" ] \
            && pass "F-105: gui_trust.d/defaults present" \
            || fail "F-105: gui_trust.d/defaults.jsonl (or 00-defaults.jsonl) missing — Fix V trust store starts empty, every grounded action would prompt HITL"

        # F-105 (Fix V.3c): the ib-trust CLI shipped for users to
        # inspect + manage grants via `ib-trust list / add / revoke`.
        check_exec /usr/local/bin/ib-trust "F-105: /usr/local/bin/ib-trust missing — users have no way to manage the trust store"

        # F-103 (Fix V.1): vision.py self-test — smoke that VisionGrounder
        # can be imported + does a canned mock parse. Doesn't hit the
        # network — only verifies the module wiring.
        if chroot "$CHROOT" bash -c "$VENV_PY -m gui_agent.vision --self-test 2>&1" \
                | grep -q "vision OK"; then
            pass "F-103: gui_agent.vision --self-test OK (VisionGrounder importable + parseable)"
        else
            fail "F-103: gui_agent.vision --self-test failed — VisionGrounder wiring broken"
        fi

        # F-107 (Fix V.6a): pre-existing Landlock bit-value bug fix
        # regression guard. If a refactor reverts the constants, this
        # smoke gate + the unit test test_sandbox_landlock_bits.py
        # both fail. `1` here is LANDLOCK_ACCESS_FS_EXECUTE per kernel
        # UAPI include/uapi/linux/landlock.h.
        #
        # V.6e (2026-08-02) — F-101.2-style fix for THIS assertion:
        # prior implementation used `python … 2>&1 | grep -qv "…"` which
        # inverted the semantics on empty stdout (grep -qv on empty
        # returns exit 1 → fail branch fires on green builds). Now use
        # the Python exit code directly: `assert` inside the -c script
        # raises SystemExit(1) on failure, 0 on success. `if chroot …`
        # branches correctly on that.
        if chroot "$CHROOT" bash -c "$VENV_PY -c 'from gui_agent.sandbox import _LANDLOCK_ACCESS_FS_EXECUTE; assert _LANDLOCK_ACCESS_FS_EXECUTE == 1, _LANDLOCK_ACCESS_FS_EXECUTE' >/dev/null 2>&1"; then
            pass "F-107: gui_agent.sandbox Landlock bit values match kernel UAPI"
        else
            fail "F-107: gui_agent.sandbox Landlock bit-value regression — _LANDLOCK_ACCESS_FS_EXECUTE must equal 1<<0 per kernel"
        fi
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
        # F-98 regression guard: opencode 1.18.4 requires nested-object
        # permission shape `{"mcp": {"<pattern>": "<action>"}}`. The flat
        # shape `{"<pattern>": "<action>"}` from gen-oc-config.sh v1 was
        # silently ignored → MCP tool calls failed silently in the GUI TUI.
        in_chroot "python3 -c \"
import json, sys
d = json.load(open('/etc/icebreaker/qb_oc.json'))
p = d.get('permission')
assert isinstance(p, dict), f'F-98: permission must be object, got {type(p).__name__}'
mcp = p.get('mcp')
assert isinstance(mcp, dict) and mcp, f'F-98: permission.mcp must be non-empty object, got {type(mcp).__name__}'
ib = [k for k in mcp if k.startswith('icebreaker_')]
assert ib, 'F-98: no icebreaker_* patterns in permission.mcp — gen-oc-config.sh output shape regression'
# F-101 regression guard: iceui MCP server's gui.* + rpa.* tools must
# also appear in the permission map so opencode actually invokes them.
iceui = [k for k in mcp if k.startswith('iceui_')]
assert iceui, 'F-101: no iceui_* patterns in permission.mcp — gen-oc-config.sh iceui harvest broken'
assert len(iceui) >= 12, f'F-101: expected at least 12 iceui_* entries, got {len(iceui)}'
for k, v in mcp.items():
    assert v in ('allow', 'ask', 'deny'), f'F-98: bad action {v!r} for {k}'
# F-100: opencode native edit + bash tools must be denied so model
# routes all writes/shell through icebreaker_* MCP tools (mcpd sandbox
# + tier gate + INV-8 audit). Without this the model silently bypasses
# every Icebreaker security invariant.
assert p.get('edit') == 'deny', f'F-100 regression: permission.edit must be deny, got {p.get(\\\"edit\\\")!r}'
assert p.get('bash') == 'deny', f'F-100 regression: permission.bash must be deny, got {p.get(\\\"bash\\\")!r}'
sys.exit(0)
\"" \
            && pass "F-98 + F-100: qb_oc.json permission shape + native-tool deny" \
            || fail "F-98/F-100 regression: qb_oc.json permission shape or native-tool policy wrong"

        # v6.17 M7.6a-1h (F-111): opencode submit_intent system prompt
        # file must ship at the runtime path gen-oc-config.sh's
        # instructions[] field references. Without it, opencode reads
        # an empty instructions file and Gemini has no submit_intent
        # guidance — turns fail at the permission gate.
        check_file /etc/icebreaker/opencode_prompt_submit_intent.txt \
            "F-111 M7.6a-1g: opencode submit_intent system prompt missing"
        # Prompt content sanity — file exists but empty would be a
        # silent failure of the runtime experience. Guard against a
        # zero-byte install failure or accidental empty overwrite.
        _prompt_size=$(stat -c '%s' \
            "${CHROOT}/etc/icebreaker/opencode_prompt_submit_intent.txt" \
            2>/dev/null || echo 0)
        [ "$_prompt_size" -ge 500 ] \
            && pass "F-111 M7.6a-1g: submit_intent prompt file present (${_prompt_size} bytes)" \
            || fail "F-111 M7.6a-1g: submit_intent prompt file too small (${_prompt_size} bytes) — expected >500 bytes"

        # v6.17 M7.6a-1h (F-111): if qb_oc.json has the `instructions`
        # field (M7.6a-1g wired it in submit_intent_only mode — the
        # default for v6.17+ OC builds), verify:
        #   1. instructions points at the shipped prompt file
        #   2. permission.mcp locks EVERY tool to 'deny' except
        #      iceui_submit_intent which is 'allow'
        # Legacy_direct builds (rollback path) don't have instructions
        # → we skip the lockdown check for them (existing F-98/F-100
        # checks above cover the legacy permission shape).
        in_chroot "python3 -c \"
import json, sys
d = json.load(open('/etc/icebreaker/qb_oc.json'))
instr = d.get('instructions')
if instr is None:
    # legacy_direct mode — no assertion (already validated above)
    print('  [INFO] qb_oc.json has no instructions field (legacy_direct mode)', file=sys.stderr)
    sys.exit(0)
# submit_intent_only mode — verify lockdown
assert isinstance(instr, list), f'F-111 M7.6a-1g: instructions must be list, got {type(instr).__name__}'
assert '/etc/icebreaker/opencode_prompt_submit_intent.txt' in instr, (
    f'F-111 M7.6a-1g: instructions missing shipped prompt path: {instr}'
)
p = d.get('permission', {})
mcp = p.get('mcp', {})
assert mcp.get('iceui_submit_intent') == 'allow', (
    f'F-111 M7.6a-1e: submit_intent_only requires iceui_submit_intent=allow, got {mcp.get(\\\"iceui_submit_intent\\\")!r}'
)
for k, v in mcp.items():
    if k == 'iceui_submit_intent':
        continue
    assert v == 'deny', (
        f'F-111 M7.6a-1e: submit_intent_only requires every non-submit_intent tool to be deny, {k}={v!r}'
    )
sys.exit(0)
\"" \
            && pass "F-111 M7.6a-1e/g: qb_oc.json OC_MODE lockdown consistent (instructions + permission)" \
            || fail "F-111 M7.6a-1e/g regression: qb_oc.json OC_MODE lockdown broken"
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
    ! [ -e "${CHROOT}/home/icebreaker" ] \
        && pass "no baked icebreaker login/home" || fail "release image still contains /home/icebreaker"
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
    check_file /etc/icebreaker/locations.env "locations.env missing (non-secret environment contract)"
    check_exec /usr/local/bin/ib-setup-key "ib-setup-key not installed"
    check_exec /usr/libexec/icebreaker/icebreaker-onboarding "first-login onboarding helper missing"
    check_file /etc/xdg/autostart/icebreaker-onboarding.desktop "first-login onboarding autostart missing"
    check_file /usr/share/applications/icebreaker-install.desktop "Ubuntu installer launcher missing"
    PERMS="$(stat -c '%a' "${CHROOT}/etc/icebreaker/locations.env" 2>/dev/null || echo '')"
    # locations.env is intentionally public non-secret service configuration.
    # API keys may exist only in credentials.env (0600 root:root), which L2
    # checks independently.
    [ "$PERMS" = "644" ] \
        && pass "locations.env is public non-secret configuration (0644)" \
        || fail "locations.env perms '$PERMS' != 644"
    grep -qE '^[A-Z_]*API_KEY=' "${CHROOT}/etc/icebreaker/locations.env" 2>/dev/null \
        && fail "BP-8 VIOLATION: API key variable in public locations.env" \
        || pass "locations.env contains no API key variables (BP-8)"
    grep -qE '^[A-Z_]*API_KEY=' "${CHROOT}/etc/icebreaker/credentials.env" 2>/dev/null \
        && fail "BP-8 VIOLATION: live API key baked into credentials.env" \
        || pass "no live API key in the image (BP-8)"
fi

# ═══ Level 6: PB + mcpd ═══
if [ "$LEVEL" -ge 6 ]; then
    echo "[L6] PB + mcpd"
    check_exec /usr/libexec/icebreaker/mcpd "mcpd binary missing"
    check_exec /usr/libexec/icebreaker/llama-server "llama-server missing"
    check_exec /usr/libexec/icebreaker/start-pbd "start-pbd missing"
    check_file /etc/systemd/system/icebreaker-pbd.service "pbd unit missing"
    # v6.16 M7.0.2g (F-110): PB grammar file MUST be present at the
    # shipping path start-pbd reads --grammar-file from. Missing =
    # audit row C-4 open + whitepaper §6 non-compliant. Build-time
    # gate (belt-and-braces alongside start-pbd's runtime visible-warn).
    check_file /var/lib/icebreaker/grammars/mcp_tool_call.gbnf \
        "F-110: PB grammar missing at /var/lib/icebreaker/grammars/ — start-pbd will drop --grammar-file, PB emits schema-invalid JSON at ~5%, audit row C-4 stays open"
    # Sanity: grammar file must be non-empty text (guards against a
    # zero-byte install failure or accidental binary swap).
    _grammar_size=$(stat -c '%s' "${CHROOT}/var/lib/icebreaker/grammars/mcp_tool_call.gbnf" 2>/dev/null || echo 0)
    [ "$_grammar_size" -ge 100 ] \
        && pass "F-110: PB grammar file present (${_grammar_size} bytes)" \
        || fail "F-110: PB grammar file too small (${_grammar_size} bytes) — expected >100 bytes"
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
    grep -q "^BindPaths=/home$" "$UNIT" 2>/dev/null \
        && pass "controller unit: dynamically scoped writable /home bind" \
        || fail "controller unit missing 'BindPaths=/home' for authenticated per-user mcpd"
    grep -q "^RestrictSUIDSGID=no$" "$UNIT" 2>/dev/null \
        && pass "controller unit: permits authenticated mcpd uid/gid transition" \
        || fail "controller unit blocks the required per-user mcpd uid/gid transition"
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
    # F-109 (M7.0.1c, v6.16): mcpd registers the cow.commit RPC and lists
    # it in the tools/list catalogue. Without this, the two-phase COW
    # commit flow degrades to silent tickets that never execute — the
    # exact state the whitepaper §5 promised to prevent (audit row C-1).
    # Strings check because release binaries are stripped; the method
    # name is embedded as a literal in server.rs::is_known_method.
    in_chroot "strings /usr/libexec/icebreaker/mcpd | grep -q 'cow.commit'" \
        && pass "F-109: mcpd carries cow.commit method (M7.0.1c/v6.16)" \
        || fail "F-109: mcpd binary lacks cow.commit — M7.0.1c dispatch code missing, two-phase COW broken"
    # F-109 (M7.0.1a, v6.16): apt-get -s is the simulator backend for
    # package.* COW previews. Sanity check that apt-get exists in the
    # chroot; the actual -s output shape is exercised by cargo tests.
    in_chroot "command -v apt-get" >/dev/null 2>&1 \
        && pass "F-109: apt-get present for package.* COW simulator" \
        || fail "F-109: apt-get missing — package.* COW previews will visible-warn"
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
