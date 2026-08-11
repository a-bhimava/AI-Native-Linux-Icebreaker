#!/usr/bin/env bash
# gen-oc-config.sh — generate /etc/icebreaker/qb_oc.json at ISO build time.
#
# Boots the just-built mcpd binary, calls tools/list over stdio JSON-RPC,
# builds a permission map (Tier <= 1 => 'allow', Tier >= 2 => 'ask'),
# splices it into the shipped qb_oc.json.template, writes the final
# qb_oc.json into the target chroot.
#
# Same "harvest against the just-built binary" pattern as mcpd-harvest.sh
# (F-33 precedent). Ensures the shipped config reflects the ACTUAL tool
# set of the mcpd binary that lands in the ISO — no drift possible.
#
# v6.17 M7.6a-1e: OC_MODE env var gates which tools opencode/Gemini can
# actually call. Two modes:
#   OC_MODE=legacy_direct     — default; keeps v6.13_OC..v6.16 behavior
#                                (24+N tools each set to allow|ask based
#                                on tier).  Rollback knob.
#   OC_MODE=submit_intent_only — v6.17 M7.6a-1 shipping mode; ONLY
#                                iceui_submit_intent is 'allow'. Every
#                                other MCP tool (mcpd + iceui gui/rpa) is
#                                'deny'. Forces Gemini to route through
#                                Controller.run_turn_from_intent, which
#                                gives OC users the v6.16 M7.0.1 + M7.0.2
#                                safety features (COW dry-run at HITL, PB
#                                grammar mandate, bounded retry with
#                                QB-consult rescue).
#
# Usage:
#   MCPD_BIN=/path/to/mcpd \
#   TEMPLATE=/path/to/qb_oc.json.template \
#   OUTPUT=/target/chroot/etc/icebreaker/qb_oc.json \
#   [OC_MODE=submit_intent_only|legacy_direct] \
#   bash incremental/build/gen-oc-config.sh
#
# Exit codes:
#   0 — qb_oc.json written successfully
#   1 — missing input file or mcpd handshake failed
#   2 — template splice / JSON assembly failed

set -euo pipefail

MCPD_BIN="${MCPD_BIN:?MCPD_BIN required (path to built mcpd binary)}"
TEMPLATE="${TEMPLATE:?TEMPLATE required (path to qb_oc.json.template)}"
OUTPUT="${OUTPUT:?OUTPUT required (path to write final qb_oc.json)}"

[ -x "${MCPD_BIN}" ] || { echo "gen-oc-config: MCPD_BIN not executable: ${MCPD_BIN}" >&2; exit 1; }
[ -f "${TEMPLATE}" ] || { echo "gen-oc-config: TEMPLATE not found: ${TEMPLATE}" >&2; exit 1; }

# ── 1. call mcpd tools/list, capture the tools array to a temp file ────
TMP_TOOLS="$(mktemp -t mcpd-tools.XXXXXX.json)"
trap 'rm -f "${TMP_TOOLS}"' EXIT

printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | "${MCPD_BIN}" 2>/dev/null | head -1 > "${TMP_TOOLS}"

if [ ! -s "${TMP_TOOLS}" ]; then
    echo "gen-oc-config: mcpd tools/list returned no output" >&2
    exit 1
fi

# ── 2. python driver — reads tools JSON + template, writes qb_oc.json ──
# v6.17 M7.6a-1e: OC_MODE (env var) selects tool-visibility policy.
# Absent → legacy_direct (v6.16 shipping behavior).
# submit_intent_only → lock every tool except iceui_submit_intent to
# 'deny'. Enables the M7.6a-1 flow (opencode → submit_intent →
# Controller → PB → mcpd) as the only path through.
OC_MODE="${OC_MODE:-legacy_direct}"
if [ "${OC_MODE}" != "legacy_direct" ] && [ "${OC_MODE}" != "submit_intent_only" ]; then
    echo "gen-oc-config: WARN unknown OC_MODE='${OC_MODE}', defaulting to legacy_direct" >&2
    OC_MODE="legacy_direct"
fi
echo "gen-oc-config: OC_MODE=${OC_MODE}" >&2

TOOLS_FILE="${TMP_TOOLS}" \
TEMPLATE_FILE="${TEMPLATE}" \
OUTPUT_FILE="${OUTPUT}" \
OC_MODE="${OC_MODE}" \
python3 <<'PY'
import json, os, sys

tools_file = os.environ['TOOLS_FILE']
template_file = os.environ['TEMPLATE_FILE']
output_file = os.environ['OUTPUT_FILE']

with open(tools_file) as f:
    tools_resp = json.load(f)

if 'result' not in tools_resp or 'tools' not in tools_resp['result']:
    print('gen-oc-config: mcpd tools/list response missing result.tools', file=sys.stderr)
    sys.exit(1)

tools = tools_resp['result']['tools']
if not isinstance(tools, list):
    print('gen-oc-config: result.tools is not an array', file=sys.stderr)
    sys.exit(1)

print(f'gen-oc-config: mcpd tools/list returned {len(tools)} tools', file=sys.stderr)

# opencode namespaces MCP tools as '<server>_<tool>'. Our server name in
# the template is 'icebreaker', so mcpd's 'fs.write' becomes
# 'icebreaker_fs.write' in the permission map.
#
# Tier rules:
#   - Tier >= 2  → always 'ask' (destructive / system-wide effect).
#   - Tier <= 1  → 'allow' by default, but the ALWAYS_ASK set below
#     forces 'ask' for tools whose runtime tier escalates by path
#     (mcpd's COW gate uses the runtime path to decide, not the
#     descriptor tier). Without this, opencode would happily let the
#     model call `fs.write /etc/hostname` with no user prompt, and
#     mcpd's COW ticket response wouldn't surface as a permission
#     request — the user would just see a confusing "requires approval"
#     tool result. Conservative defaults: any tool that WRITES anything
#     always asks, even when descriptor tier is 0/1.
ALWAYS_ASK = {
    "fs.write",       # tier can escalate to 2+ for out-of-home paths
    "fs.delete",      # tier can escalate similarly
    "process.inspect", # reads /proc/<pid> — usually fine but can leak
}

# F-98 (2026-07-27): opencode 1.18.4 takes `permission` as a nested
# object shape: `{"<kind>": {"<pattern>": "<action>"}}` — where <kind>
# is one of `mcp`, `bash`, `edit`, `question`, `plan_enter`, `plan_exit`
# (per opencode strings dump). The flat object shape `{"<tool>":
# "<action>"}` we shipped in v6.13_OC was SILENTLY IGNORED — MCP tool
# calls silently denied in the GUI TUI. Live-verified via v6.13_OC
# arm64 UTM: bare `icebreaker_system.uptime` prompt returned empty +
# $0.00 + 0 tokens. Live-verified fix: nested shape returns real tool
# output ("The system has been up for 5 hours and 7 minutes.").
#
# MCP tool names live under the `mcp` kind, keyed by their
# `<server>_<tool>` name (opencode's namespace).
mcp_perms = {}
for t in tools:
    name = t['name']
    tier = int(t.get('tier', 0))
    pattern = f"icebreaker_{name}"
    action = "ask" if (tier >= 2 or name in ALWAYS_ASK) else "allow"
    mcp_perms[pattern] = action

# F-101 (2026-07-27): the iceui MCP server (dual-brain/controller/
# mcp_gui_server.py) exposes gui.* + rpa.* tools that mcpd doesn't
# know about. Import the same schemas that server uses to build the
# permission entries — keeps the two components' tool sets in sync
# by construction rather than by convention. Runs at build time on
# the operator (Mac) or on the GCP VM — either has the Python
# controller package importable in the current sys.path (via the
# dual-brain venv the build already uses to run this script).
#
# Default actions:
#   gui.ping / gui.get_window_list / rpa.ping / rpa.list_workflows
#     → allow (read-only, no PII leak, no input synthesis)
#   gui.screenshot / gui.find_element / gui.get_element_tree /
#     gui.click / gui.type / gui.select / rpa.execute_workflow /
#     rpa.find_by_image → ask (may leak screen content OR synthesize
#     keyboard/mouse events via /dev/uinput)
try:
    import sys
    # Extend path so we can import gui_agent + rpa_bridge from the
    # dual-brain repo (build script may not have set PYTHONPATH).
    _repo_root = os.environ.get('REPO_ROOT') or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(template_file))))
    _dual_brain = os.path.join(_repo_root, 'dual-brain')
    if os.path.isdir(_dual_brain) and _dual_brain not in sys.path:
        sys.path.insert(0, _dual_brain)
    from gui_agent.protocol import _PARAM_SCHEMAS as _GUI_SCHEMAS
    from rpa_bridge.protocol import _PARAM_SCHEMAS as _RPA_SCHEMAS
except ImportError as _exc:
    print(
        f'gen-oc-config: WARNING — cannot import gui_agent/rpa_bridge '
        f'({_exc}); iceui permission entries will be empty and Fix M '
        f'tool calls will be denied at runtime.',
        file=sys.stderr,
    )
    _GUI_SCHEMAS = {}
    _RPA_SCHEMAS = {}

_ICEUI_ALLOW = {'gui.ping', 'gui.get_window_list', 'rpa.ping', 'rpa.list_workflows'}
for _iceui_tool in sorted(_GUI_SCHEMAS.keys()) + sorted(_RPA_SCHEMAS.keys()):
    _pattern = f'iceui_{_iceui_tool}'
    mcp_perms[_pattern] = 'allow' if _iceui_tool in _ICEUI_ALLOW else 'ask'
print(
    f'gen-oc-config: added {len(_GUI_SCHEMAS)+len(_RPA_SCHEMAS)} iceui_ '
    f'permission entries (F-101)',
    file=sys.stderr,
)

# F-100 (2026-07-27): deny opencode's native write/edit/bash tools so
# the model is forced to use icebreaker_* MCP tools for any real system
# action. Without this, opencode's built-in `write` and `bash` bypass
# every Icebreaker invariant (INV-6 COW dry-run, INV-8 audit hash chain,
# mcpd's Landlock+seccomp sandbox) — model would write files with plain
# fs syscalls under user perms + no audit trail, defeating the entire
# security posture Icebreaker's OC edition inherited from the current
# edition's dual-brain design.
#
# Live-verified on v6.13_OC arm64 guest: with `edit: deny`, Gemini
# fell back to `icebreaker_fs_write` MCP tool for a "create file X
# with content Y" prompt; mcpd's audit log recorded the tool call;
# oc_audit_bridge enriched an INV-8 row; response noted "This
# operation required your approval." (tier-2 gate acknowledged).
#
# Trade-off: `bash: deny` prevents opencode from running arbitrary
# shell commands ('run ls', 'check git status'). Users who need
# shell access should use gnome-terminal (which they already have
# open running icebreaker-oc). The AI Terminal's ROLE in the OC
# edition is NL → mcpd tool calls, not general-purpose shell.
# Variance note: Gemini sometimes gives up silently when a natural
# tool choice is denied. Future tuning may re-open bash as `ask`
# if the strict policy proves too restrictive in real use.
# v6.17 M7.6a-1e: OC_MODE=submit_intent_only lockdown.
# When set, every existing MCP permission entry gets 'deny' + we add
# ONE 'allow' for iceui_submit_intent. This forces Gemini to route
# every real system op through Controller.run_turn_from_intent —
# where v6.16 M7.0.1 COW dry-run at HITL + M7.0.2 PB grammar mandate
# + bounded retry with QB-consult rescue actually fire.
#
# The 'deny' entries also protect against a Gemini system-prompt
# override attempt: even if the model tries a raw fs.write, opencode's
# permission gate rejects at the transport layer before mcpd sees it.
#
# submit_intent itself lives at 'iceui_submit_intent' per Fix M
# (mcp_gui_server.py, v6.17 M7.6a-1a). If the iceui server isn't
# importable at build time (see _GUI_SCHEMAS fallback above), this
# still emits the allow entry so runtime dispatch works — the iceui
# server itself carries the submit_intent tool definition.
oc_mode = os.environ.get('OC_MODE', 'legacy_direct')
if oc_mode == 'submit_intent_only':
    _pre_count = len(mcp_perms)
    for _k in list(mcp_perms.keys()):
        mcp_perms[_k] = 'deny'
    mcp_perms['iceui_submit_intent'] = 'allow'
    print(
        f'gen-oc-config: OC_MODE=submit_intent_only — locked '
        f'{_pre_count} tools to deny, iceui_submit_intent to allow',
        file=sys.stderr,
    )
else:
    print(
        f'gen-oc-config: OC_MODE=legacy_direct — {len(mcp_perms)} tools '
        f'reachable per per-tool tier policy',
        file=sys.stderr,
    )

permissions = {"mcp": mcp_perms, "edit": "deny", "bash": "deny"}

# v6.17 M7.6a-1g: when submit_intent_only mode is active, wire the
# opencode instructions field to point at the shipped system-prompt
# file. This teaches Gemini to translate user requests into the
# intent object shape submit_intent expects. Without this prompt,
# Gemini will attempt other tools and hit the 'deny' walls with no
# useful recovery signal.
#
# Instructions path is a runtime path (where the file lives on the
# booted ISO, NOT where the file lives in the source tree). The build
# system installs the source file to this exact path via
# cx-distro/build.sh / v-manifests.
INSTRUCTIONS_RUNTIME_PATH = "/etc/icebreaker/opencode_prompt_submit_intent.txt"
if oc_mode == 'submit_intent_only':
    instructions = [INSTRUCTIONS_RUNTIME_PATH]
    print(
        f'gen-oc-config: OC_MODE=submit_intent_only — wiring instructions '
        f'-> {INSTRUCTIONS_RUNTIME_PATH}',
        file=sys.stderr,
    )
else:
    instructions = None  # unchanged behavior for legacy_direct

# Read template + strip comment fields (opencode's parser is strict JSON).
with open(template_file) as f:
    template = json.load(f)

def strip_comments(obj):
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith('//')}
    if isinstance(obj, list):
        return [strip_comments(x) for x in obj]
    return obj

template = strip_comments(template)
template['permission'] = permissions
# v6.17 M7.6a-1g: opencode `instructions` array — file paths whose
# contents opencode injects into the model system prompt. Only set
# when OC_MODE=submit_intent_only so legacy_direct configs stay
# byte-for-byte identical to v6.16.
if instructions is not None:
    template['instructions'] = instructions

with open(output_file + '.tmp', 'w') as f:
    json.dump(template, f, indent=2)
    f.write('\n')

os.rename(output_file + '.tmp', output_file)
print(f'gen-oc-config: wrote {len(permissions)} tool permission entries', file=sys.stderr)
print(f'gen-oc-config: OK — {output_file}', file=sys.stderr)
PY
