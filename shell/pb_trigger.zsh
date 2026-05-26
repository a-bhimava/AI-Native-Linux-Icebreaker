#!/usr/bin/env zsh
# pb_trigger.zsh — Natural language → shell command via Privileged Brain
#
# Usage: type ^<natural language> and press Enter
#   ^list all docker containers using more than 500MB
#   ^show which process is eating memory
#   ^find all .log files older than 7 days
#
# Source this from ~/.zshrc:
#   source ~/path/to/shell/pb_trigger.zsh

PB_CACHE_DB="${HOME}/.pb_cache.db"
PB_AUDIT_LOG="${HOME}/.pb_audit.jsonl"
PB_CACHE_SCRIPT="${0:A:h}/pb_cache.py"
PB_AUDIT_SCRIPT="${0:A:h}/pb_audit.py"
PB_MODEL_PRIMARY="privileged-brain"
PB_MODEL_FALLBACK="qwen2.5-coder:1.5b"
PB_OLLAMA_URL="http://localhost:11434/api/generate"

# Patterns that are always blocked regardless of model output
_PB_DANGEROUS_PATTERNS='(rm -rf /[^a-zA-Z]|rm -rf /$|mkfs\.|dd if=/dev/zero|dd if=/dev/random|> /dev/sd|iptables -F$|iptables --flush$|DROP TABLE|DROP DATABASE|chmod -R 777 /|fork bomb|\(\)\s*\{.*\}.*&)'

function _pb_query_model() {
    local query="$1"
    local model="$2"
    local prompt_json

    prompt_json=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$query")

    curl -s --max-time 15 -X POST "$PB_OLLAMA_URL" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${model}\",\"prompt\":${prompt_json},\"stream\":false,\"options\":{\"temperature\":0.1}}" \
        2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    resp = data.get('response', '').strip()
    # Strip markdown code fences if model wrapped the output
    if resp.startswith('\`\`\`'):
        lines = resp.split('\n')
        resp = '\n'.join(l for l in lines if not l.startswith('\`\`\`')).strip()
    print(resp)
except:
    pass
" 2>/dev/null
}

function _pb_check_cache() {
    local query="$1"
    [[ -f "$PB_CACHE_SCRIPT" ]] || return 1
    python3 "$PB_CACHE_SCRIPT" get "$query" 2>/dev/null
}

function _pb_save_cache() {
    local query="$1" cmd="$2"
    [[ -f "$PB_CACHE_SCRIPT" ]] && python3 "$PB_CACHE_SCRIPT" set "$query" "$cmd" 2>/dev/null
}

function _pb_audit() {
    local nl="$1" cmd="$2" blocked="$3" cwd="$4"
    [[ -f "$PB_AUDIT_SCRIPT" ]] && python3 "$PB_AUDIT_SCRIPT" log "$nl" "$cmd" "$blocked" "$cwd" 2>/dev/null &
}

function _pb_nl2cmd() {
    local buf="$BUFFER"

    # Only intercept if buffer starts with ^
    if [[ "$buf" != \^* ]]; then
        zle .accept-line
        return
    fi

    local query="${buf:1}"
    query="${query## }"  # strip leading space

    if [[ -z "$query" ]]; then
        BUFFER=""
        zle redisplay
        return
    fi

    # Show spinner while querying
    BUFFER="⏳ ${query}"
    zle redisplay

    local cmd=""

    # 1. Check cache first (instant)
    cmd=$(_pb_check_cache "$query")

    # 2. Try fine-tuned model
    if [[ -z "$cmd" ]]; then
        cmd=$(_pb_query_model "$query" "$PB_MODEL_PRIMARY")
    fi

    # 3. Fallback to base model if fine-tuned not available yet
    if [[ -z "$cmd" ]]; then
        cmd=$(_pb_query_model "Output only a single bash command, no explanation, for: ${query}" "$PB_MODEL_FALLBACK")
    fi

    # Strip any remaining whitespace / newlines
    cmd="${cmd%%$'\n'*}"
    cmd="${cmd%% }"

    # Safety check — block dangerous patterns
    if [[ -n "$cmd" ]] && echo "$cmd" | grep -qE "$_PB_DANGEROUS_PATTERNS"; then
        _pb_audit "$query" "$cmd" "true" "$PWD"
        BUFFER="# BLOCKED — dangerous command detected. Review manually: $cmd"
        zle redisplay
        return
    fi

    # Place result in readline buffer
    if [[ -z "$cmd" ]]; then
        BUFFER="# No command generated. Try rephrasing your query."
    else
        _pb_save_cache "$query" "$cmd"
        _pb_audit "$query" "$cmd" "false" "$PWD"
        BUFFER="$cmd"
    fi

    zle redisplay
}

zle -N _pb_nl2cmd
bindkey '^M' _pb_nl2cmd   # Enter
bindkey '^J' _pb_nl2cmd   # Ctrl+J (alternate Enter on some terminals)

# Show a brief confirmation that the plugin loaded
function _pb_status() {
    local primary_up=false fallback_up=false
    curl -s --max-time 2 http://localhost:11434/api/tags &>/dev/null && {
        ollama list 2>/dev/null | grep -q "privileged-brain" && primary_up=true
        ollama list 2>/dev/null | grep -q "qwen2.5-coder" && fallback_up=true
    }
    echo "pb_trigger loaded  |  primary: ${primary_up}  |  fallback: ${fallback_up}"
    echo "Usage: ^<your intent>  then press Enter"
}

_pb_status
