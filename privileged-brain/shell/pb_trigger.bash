#!/usr/bin/env bash
# pb_trigger.bash — Privileged Brain shell trigger (bash)
#
# Translates a natural language description into a bash command using the
# locally-running Privileged Brain model, validates it, and (optionally) executes it.
#
# Dependencies:
#   - pb-serve (recommended) or llama-cli / llama-run (fallback)
#   - shellcheck (optional but recommended — brew install shellcheck / apt install shellcheck)
#   - The Privileged Brain GGUF model at $PB_MODEL_PATH
#
# Usage:
#   pb "show disk usage"              # generate and confirm
#   pb "restart nginx"                # same
#   pb --dry-run "restart nginx"      # generate only, do not execute
#
# Fast setup (add to ~/.bashrc):
#   pb-serve start                    # starts resident inference daemon (~300ms/query)
#   source /path/to/pb_trigger.bash   # loads pb() function
#
# Environment variables:
#   PB_MODEL_PATH   Path to the GGUF model file (default: ~/models/privileged-brain-awq.gguf)
#   PB_GRAMMAR      Path to the GBNF grammar file (default: auto-detected)
#   PB_PORT         Port for pb-serve resident server (default: 8765)
#   PB_THREADS      Number of CPU threads for cold-start fallback (default: 4)
#   PB_CTX          Context size tokens (default: 512)

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
PB_MODEL_PATH="${PB_MODEL_PATH:-"$HOME/models/privileged-brain-awq.gguf"}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PB_GRAMMAR="${PB_GRAMMAR:-"$SCRIPT_DIR/../inference/grammar/bash_cot.gbnf"}"
PB_PORT="${PB_PORT:-8765}"
PB_THREADS="${PB_THREADS:-4}"
PB_CTX="${PB_CTX:-512}"

# ── Dangerous command patterns — refuse even if model doesn't ─────────────────
_DANGER_PATTERNS=(
    "rm -rf /"
    "rm -rf --no-preserve-root"
    "dd if=/dev/zero of=/dev"
    "dd if=/dev/urandom of=/dev"
    "> /dev/sda"
    "> /dev/nvme"
    "shred /dev/"
    "mkfs /dev/"
    "curl.*|.*bash"
    "curl.*|.*sh"
    "wget.*|.*bash"
    "wget.*|.*sh"
    "eval.*curl"
    "eval.*wget"
    "chmod -R 777 /"
    "chmod 777 /etc"
    "echo.*sudoers.*NOPASSWD"
)

# ── Colours ────────────────────────────────────────────────────────────────────
_RED='\033[0;31m'
_YLW='\033[0;33m'
_GRN='\033[0;32m'
_CYN='\033[0;36m'
_RST='\033[0m'

# ── Usage ──────────────────────────────────────────────────────────────────────
_usage() {
    echo "Usage: pb [--dry-run] <natural language description>"
    echo "       pb --help"
    exit 1
}

# ── Check dependencies ─────────────────────────────────────────────────────────
_check_deps() {
    # If the resident server is up, no other deps required
    if curl -sf "http://127.0.0.1:${PB_PORT}/health" &>/dev/null; then
        return 0
    fi
    # Server not running — need llama-cli for cold-start fallback
    if ! command -v llama-cli &>/dev/null && ! command -v llama-run &>/dev/null; then
        echo -e "${_RED}Error:${_RST} pb-serve is not running and llama-cli was not found."
        echo "Start the inference server: pb-serve start"
        echo "Or install llama.cpp: https://github.com/ggml-org/llama.cpp"
        exit 1
    fi
    if [[ ! -f "$PB_MODEL_PATH" ]]; then
        echo -e "${_RED}Error:${_RST} Model not found at: $PB_MODEL_PATH"
        echo "Set PB_MODEL_PATH to the location of your privileged-brain GGUF file."
        exit 1
    fi
}

# ── Run inference ──────────────────────────────────────────────────────────────
_run_inference() {
    local nl="$1"
    local system_prompt
    system_prompt="You are the Privileged Brain — a system execution engine for an AI-native OS. \
You receive natural language descriptions of system administration tasks and output \
a structured two-line response:

REASONING: <one sentence — what this command does and why it is safe to run>
COMMAND: <the bare bash command>

Rules you must always follow:
1. REASONING must be exactly one line — your internal safety check before executing.
2. COMMAND must be exactly one line — the bare shell command, no markdown, no fences.
3. Prefer minimal-scope, reversible commands.
4. Never read or process external data (emails, documents, URLs).
5. If a request is ambiguous or dangerous: COMMAND: REFUSE: <one-line reason>
6. If a request is too vague to safely execute: COMMAND: CLARIFY: <one specific question>"

    # ── Fast path: resident pb-serve server (~300ms) ───────────────────────────
    if curl -sf "http://127.0.0.1:${PB_PORT}/health" &>/dev/null; then
        local json_payload
        json_payload=$(PB_SYS="$system_prompt" PB_NL="$nl" python3 -c "
import os, json
print(json.dumps({
    'messages': [
        {'role': 'system', 'content': os.environ['PB_SYS']},
        {'role': 'user',   'content': os.environ['PB_NL']},
    ],
    'max_tokens': 160,
    'temperature': 0.0,
}))
")
        curl -sf "http://127.0.0.1:${PB_PORT}/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -d "$json_payload" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
        return
    fi

    # ── Cold-start fallback: spawn llama-cli (~5s) ────────────────────────────
    echo -e "${_YLW}Note: pb-serve not running — cold-start (~5s). Run: pb-serve start${_RST}" >&2
    local llama_bin
    llama_bin=$(command -v llama-cli 2>/dev/null || command -v llama-run 2>/dev/null)

    local grammar_args=()
    if [[ -f "$PB_GRAMMAR" ]]; then
        grammar_args=("--grammar-file" "$PB_GRAMMAR")
    fi

    "$llama_bin" \
        --model "$PB_MODEL_PATH" \
        --threads "$PB_THREADS" \
        --ctx-size "$PB_CTX" \
        --temp 0.0 \
        --n-predict 160 \
        --no-display-prompt \
        --silent-prompt \
        "${grammar_args[@]}" \
        --system-prompt "$system_prompt" \
        --prompt "$nl" \
        2>/dev/null
}

# ── Extract COMMAND line from CoT output ───────────────────────────────────────
_extract_command() {
    local raw="$1"
    # Find the line that starts with "COMMAND: " and extract what follows
    while IFS= read -r line; do
        if [[ "$line" == COMMAND:* ]]; then
            echo "${line#COMMAND: }"
            return
        fi
    done <<< "$raw"
    # Fallback: no COMMAND line found, return raw output
    echo "$raw"
}

# ── Pattern-filter: override model if command matches a known-dangerous pattern ─
_pattern_check() {
    local cmd="$1"
    for pattern in "${_DANGER_PATTERNS[@]}"; do
        if echo "$cmd" | grep -qiE "$pattern"; then
            echo "REFUSE: command matches dangerous pattern: $pattern"
            return
        fi
    done
    echo "$cmd"
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
    local dry_run=false
    local nl=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) dry_run=true; shift ;;
            --help|-h) _usage ;;
            *) nl="$nl $1"; shift ;;
        esac
    done

    nl="${nl# }"  # trim leading space
    [[ -z "$nl" ]] && _usage

    _check_deps

    echo -e "${_CYN}Thinking...${_RST}" >&2

    # Generate
    local raw_output
    raw_output=$(_run_inference "$nl")

    # Extract COMMAND line
    local command
    command=$(_extract_command "$raw_output")

    # Log REASONING to stderr for audit visibility
    local reasoning
    reasoning=$(echo "$raw_output" | grep "^REASONING:" | head -1 | sed 's/^REASONING: //')
    [[ -n "$reasoning" ]] && echo -e "${_CYN}Reasoning:${_RST} $reasoning" >&2

    # Semantic consistency check: REASONING flags danger but model didn't REFUSE → override
    if [[ "${command^^}" != REFUSE:* ]] && [[ "${command^^}" != CLARIFY:* ]]; then
        if echo "$reasoning" | grep -qiE \
           "permanent|destroy|irreversible|unrecoverable|wipe.*drive|root access|bypass.*password|malicious|cannot be undone"; then
            echo -e "${_RED}Warning: REASONING flags danger but model did not refuse — forcing REFUSE${_RST}" >&2
            command="REFUSE: model contradiction (REASONING flagged danger without refusing)"
        fi
    fi

    # Apply pattern-filter fallback
    command=$(_pattern_check "$command")

    # Handle REFUSE:
    if [[ "${command^^}" == REFUSE:* ]]; then
        local reason="${command#*: }"
        echo -e "${_RED}REFUSED:${_RST} $reason" >&2
        exit 0
    fi

    # Handle CLARIFY:
    if [[ "${command^^}" == CLARIFY:* ]]; then
        local question="${command#*: }"
        echo -e "${_YLW}CLARIFY:${_RST} $question" >&2
        exit 0
    fi

    # Optional shellcheck validation
    if command -v shellcheck &>/dev/null; then
        local sc_ok sc_issues tmp_sh
        tmp_sh=$(mktemp /tmp/pb_check_XXXX.sh)
        echo "#!/bin/bash" > "$tmp_sh"
        echo "$command" >> "$tmp_sh"
        if shellcheck --shell=bash --severity=warning "$tmp_sh" &>/dev/null; then
            sc_ok=true
        else
            sc_ok=false
            sc_issues=$(shellcheck --shell=bash --severity=warning "$tmp_sh" 2>&1 | tail -5)
        fi
        rm -f "$tmp_sh"
        if [[ "$sc_ok" == false ]]; then
            echo -e "${_YLW}Warning: shellcheck found issues:${_RST}" >&2
            echo "$sc_issues" >&2
        fi
    fi

    # Display the command
    echo
    echo -e "  ${_GRN}${command}${_RST}"
    echo

    if [[ "$dry_run" == true ]]; then
        echo -e "${_CYN}(dry-run — not executed)${_RST}" >&2
        exit 0
    fi

    # Confirm before execution
    echo -n "Execute? [Enter to run / Ctrl+C to cancel] " >&2
    read -r

    eval "$command"
}

# Allow sourcing without executing
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
