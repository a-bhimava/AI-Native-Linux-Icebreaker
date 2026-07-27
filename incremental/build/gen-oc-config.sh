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
# Usage:
#   MCPD_BIN=/path/to/mcpd \
#   TEMPLATE=/path/to/qb_oc.json.template \
#   OUTPUT=/target/chroot/etc/icebreaker/qb_oc.json \
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
TOOLS_FILE="${TMP_TOOLS}" \
TEMPLATE_FILE="${TEMPLATE}" \
OUTPUT_FILE="${OUTPUT}" \
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
permissions = {"mcp": mcp_perms, "edit": "deny", "bash": "deny"}

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

with open(output_file + '.tmp', 'w') as f:
    json.dump(template, f, indent=2)
    f.write('\n')

os.rename(output_file + '.tmp', output_file)
print(f'gen-oc-config: wrote {len(permissions)} tool permission entries', file=sys.stderr)
print(f'gen-oc-config: OK — {output_file}', file=sys.stderr)
PY
