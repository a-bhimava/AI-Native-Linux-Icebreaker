#!/usr/bin/env bash
# pb_trigger.bash — Natural language → shell command via Privileged Brain
#
# Bash version using readline's bind -x mechanism.
# Source from ~/.bashrc:
#   source ~/path/to/shell/pb_trigger.bash

PB_CACHE_DB="${HOME}/.pb_cache.db"
PB_AUDIT_LOG="${HOME}/.pb_audit.jsonl"
PB_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PB_CACHE_SCRIPT="${PB_SCRIPT_DIR}/pb_cache.py"
PB_AUDIT_SCRIPT="${PB_SCRIPT_DIR}/pb_audit.py"
PB_MODEL_PRIMARY="privileged-brain"
PB_MODEL_FALLBACK="qwen2.5-coder:1.5b"
PB_OLLAMA_URL="http://localhost:11434/api/generate"
_PB_DANGEROUS_PATTERNS='(rm -rf /[^a-zA-Z]|rm -rf /$|mkfs\.|dd if=/dev/zero|> /dev/sd|iptables -F$|chmod -R 777 /)'

_pb_query_model() {
    local query="$1" model="$2"
    local prompt_json
    prompt_json=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$query")
    curl -s --max-time 15 -X POST "$PB_OLLAMA_URL" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${model}\",\"prompt\":${prompt_json},\"stream\":false,\"options\":{\"temperature\":0.1}}" \
        2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    resp = data.get('response','').strip()
    if resp.startswith('\`\`\`'):
        lines = resp.split('\n')
        resp = '\n'.join(l for l in lines if not l.startswith('\`\`\`')).strip()
    # Strip CoT REASONING prefix — extract the COMMAND line if present
    if resp.startswith('REASONING:'):
        lines = resp.splitlines()
        for line in lines:
            if line.startswith('COMMAND:'):
                resp = line[len('COMMAND:'):].strip()
                break
        else:
            resp = ''
    print(resp.split('\n')[0])
except: pass
" 2>/dev/null
}

_pb_nl2cmd_bash() {
    local buf="${READLINE_LINE}"
    [[ "$buf" != \^* ]] && return

    local query="${buf:1}"
    query="${query## }"
    [[ -z "$query" ]] && { READLINE_LINE=""; return; }

    local cmd=""

    # Cache check
    [[ -f "$PB_CACHE_SCRIPT" ]] && cmd=$(python3 "$PB_CACHE_SCRIPT" get "$query" 2>/dev/null)

    # Primary model
    [[ -z "$cmd" ]] && cmd=$(_pb_query_model "$query" "$PB_MODEL_PRIMARY")

    # Fallback model
    [[ -z "$cmd" ]] && cmd=$(_pb_query_model "Output only a bash command for: ${query}" "$PB_MODEL_FALLBACK")

    cmd="${cmd%%$'\n'*}"

    if echo "$cmd" | grep -qE "$_PB_DANGEROUS_PATTERNS"; then
        READLINE_LINE="# BLOCKED: $cmd"
        READLINE_POINT=${#READLINE_LINE}
        return
    fi

    if [[ -z "$cmd" ]]; then
        READLINE_LINE="# No command generated."
    else
        [[ -f "$PB_CACHE_SCRIPT" ]] && python3 "$PB_CACHE_SCRIPT" set "$query" "$cmd" 2>/dev/null
        [[ -f "$PB_AUDIT_SCRIPT" ]] && python3 "$PB_AUDIT_SCRIPT" log "$query" "$cmd" "false" "$PWD" 2>/dev/null
        READLINE_LINE="$cmd"
    fi
    READLINE_POINT=${#READLINE_LINE}
}

bind -x '"\C-m": _pb_nl2cmd_bash'   # Enter
bind -x '"\C-j": _pb_nl2cmd_bash'   # Ctrl+J

echo "pb_trigger (bash) loaded | Usage: ^<your intent> then Enter"
