#!/bin/bash
# gui-mcp-for-oc.sh — iceui MCP server wrapper for opencode.
#
# F-101 (2026-07-27): opencode's `mcp.iceui.command` field takes a bare
# command array but doesn't source env files. This wrapper mirrors
# mcpd-for-oc.sh — loads env from /etc/icebreaker/locations.env (F-95
# canonical path), applies the GEMINI_API_KEY → GOOGLE_GENERATIVE_AI_API_KEY
# alias (F-97), then execs the Python MCP server module.
#
# Called by opencode as a stdio subprocess. Must be transparent — no
# output on stdout/stderr other than the MCP server's own JSON-RPC.

set -euo pipefail

ICEUI_ENV_FILE="${ICEUI_ENV_FILE:-/etc/icebreaker/locations.env}"
VENV_PYTHON="${VENV_PYTHON:-/opt/icebreaker/venv/bin/python3}"

if [ -f "${ICEUI_ENV_FILE}" ] && [ -r "${ICEUI_ENV_FILE}" ]; then
    set -a
    # shellcheck source=/dev/null
    . "${ICEUI_ENV_FILE}"
    set +a
fi

# F-97 alias: opencode's Google provider reads GOOGLE_GENERATIVE_AI_API_KEY.
# ib-setup-key writes GEMINI_API_KEY. Bridge in-process.
if [ -n "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]; then
    export GOOGLE_GENERATIVE_AI_API_KEY="${GEMINI_API_KEY}"
fi

# Fallback if the canonical venv path isn't present (dev env, container
# variations). Try /usr/bin/python3 as the last resort.
if [ ! -x "${VENV_PYTHON}" ]; then
    if command -v python3 >/dev/null 2>&1; then
        VENV_PYTHON="$(command -v python3)"
    else
        echo "gui-mcp-for-oc: no python3 interpreter found" >&2
        exit 127
    fi
fi

exec "${VENV_PYTHON}" -m controller.mcp_gui_server
