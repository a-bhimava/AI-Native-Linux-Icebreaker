#!/bin/bash
# mcpd-for-oc.sh — Icebreaker mcpd wrapper for opencode's MCP client.
#
# opencode's `mcp.icebreaker.command` field takes a bare command array
# but doesn't support env-var loading. This wrapper loads mcpd's runtime
# env from /etc/icebreaker/env.d/mcpd.conf (MCPD_FS_READ_ROOTS,
# MCPD_AUDIT_LOG, etc.) then execs the mcpd binary.
#
# Called by opencode as a stdio subprocess. The wrapper must be
# transparent — no output on stdout/stderr other than mcpd's own.

set -euo pipefail

MCPD_ENV_FILE="${MCPD_ENV_FILE:-/etc/icebreaker/env.d/mcpd.conf}"
MCPD_BIN="${MCPD_BIN:-/opt/icebreaker/venv/lib/python3.12/site-packages/controller/bin/mcpd}"

if [ ! -x "${MCPD_BIN}" ]; then
    # fallback locations checked in order — the venv-embedded mcpd is
    # the canonical install site, but /usr/libexec is used by some
    # older layouts.
    for cand in \
        /usr/libexec/icebreaker/mcpd \
        /usr/bin/mcpd \
        /opt/icebreaker/mcpd; do
        if [ -x "$cand" ]; then MCPD_BIN="$cand"; break; fi
    done
fi

if [ ! -x "${MCPD_BIN}" ]; then
    echo "mcpd-for-oc: cannot find mcpd binary (checked venv + /usr/libexec + /usr/bin + /opt)" >&2
    exit 127
fi

if [ -f "${MCPD_ENV_FILE}" ]; then
    set -a
    # shellcheck source=/dev/null
    . "${MCPD_ENV_FILE}"
    set +a
fi

# Sensible defaults if the env file is missing — MCPD_FS_READ_ROOTS
# empty means mcpd's default $HOME + /tmp allowlist applies.
: "${MCPD_AUDIT_LOG:=/var/log/mcpd/audit.log}"

exec "${MCPD_BIN}"
