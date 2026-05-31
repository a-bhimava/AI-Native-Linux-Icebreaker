#!/usr/bin/env zsh
# pb_trigger.zsh — Privileged Brain shell trigger (zsh)
# Identical logic to pb_trigger.bash — add to your .zshrc with:
#   source /path/to/privileged-brain/shell/pb_trigger.zsh
# Then use: pb "show disk usage"

# ── Configuration ──────────────────────────────────────────────────────────────
: "${PB_MODEL_PATH:="$HOME/models/privileged-brain-awq.gguf"}"
PB_SCRIPT_DIR="${0:A:h}"
: "${PB_GRAMMAR:="$PB_SCRIPT_DIR/../inference/grammar/bash_cot.gbnf"}"
: "${PB_PORT:=8765}"
: "${PB_THREADS:=4}"
: "${PB_CTX:=512}"

# ── Dangerous command pattern-filter ──────────────────────────────────────────
_pb_danger_patterns=(
    "rm -rf /"
    "rm -rf --no-preserve-root"
    "dd if=/dev/zero of=/dev"
    "dd if=/dev/urandom of=/dev"
    "> /dev/sda"
    "> /dev/nvme"
    "shred /dev/"
    "mkfs /dev/"
    "curl.*[|].*bash"
    "curl.*[|].*sh"
    "wget.*[|].*bash"
    "wget.*[|].*sh"
    "eval.*curl"
    "eval.*wget"
    "chmod -R 777 /"
    "chmod 777 /etc"
    "echo.*sudoers.*NOPASSWD"
)

pb() {
    local dry_run=false
    local nl=""

    for arg in "$@"; do
        case "$arg" in
            --dry-run) dry_run=true ;;
            --help|-h)
                echo "Usage: pb [--dry-run] <natural language description>"
                return 0
                ;;
            *) nl="$nl $arg" ;;
        esac
    done
    nl="${nl# }"

    if [[ -z "$nl" ]]; then
        echo "Usage: pb [--dry-run] <natural language description>"
        return 1
    fi

    # If the resident server is up, no other deps required
    if curl -sf "http://127.0.0.1:${PB_PORT}/health" &>/dev/null; then
        : # server is running, all good
    else
        if ! command -v llama-cli &>/dev/null && ! command -v llama-run &>/dev/null; then
            print -u2 "\033[0;31mError:\033[0m pb-serve is not running and llama-cli was not found."
            print -u2 "Start the inference server: pb-serve start"
            return 1
        fi
        if [[ ! -f "$PB_MODEL_PATH" ]]; then
            print -u2 "\033[0;31mError:\033[0m Model not found at: $PB_MODEL_PATH"
            return 1
        fi
    fi

    local system_prompt="You are the Privileged Brain — a system execution engine for an AI-native OS. \
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

    print -u2 "\033[0;36mThinking...\033[0m"

    local raw_output
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
        raw_output=$(curl -sf "http://127.0.0.1:${PB_PORT}/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -d "$json_payload" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])")
    else
        # ── Cold-start fallback: spawn llama-cli (~5s) ────────────────────────
        print -u2 "\033[0;33mNote: pb-serve not running — cold-start (~5s). Run: pb-serve start\033[0m"
        local llama_bin
        llama_bin=$(command -v llama-cli 2>/dev/null || command -v llama-run 2>/dev/null)
        local grammar_args=()
        [[ -f "$PB_GRAMMAR" ]] && grammar_args=("--grammar-file" "$PB_GRAMMAR")
        raw_output=$(
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
        )
    fi

    # Extract COMMAND line
    local command=""
    while IFS= read -r line; do
        if [[ "$line" == COMMAND:* ]]; then
            command="${line#COMMAND: }"
            break
        fi
    done <<< "$raw_output"
    [[ -z "$command" ]] && command="$raw_output"

    # Log REASONING
    local reasoning
    reasoning=$(print "$raw_output" | grep "^REASONING:" | head -1 | sed 's/^REASONING: //')
    [[ -n "$reasoning" ]] && print -u2 "\033[0;36mReasoning:\033[0m $reasoning"

    # Semantic consistency check: REASONING flags danger but model didn't REFUSE → override
    if [[ "${(U)command}" != REFUSE:* ]] && [[ "${(U)command}" != CLARIFY:* ]]; then
        if print "$reasoning" | grep -qiE \
           "permanent|destroy|irreversible|unrecoverable|wipe.*drive|root access|bypass.*password|malicious|cannot be undone"; then
            print -u2 "\033[0;31mWarning: REASONING flags danger but model did not refuse — forcing REFUSE\033[0m"
            command="REFUSE: model contradiction (REASONING flagged danger without refusing)"
        fi
    fi

    # Pattern-filter fallback
    for pattern in "${_pb_danger_patterns[@]}"; do
        if print "$command" | grep -qiE "$pattern"; then
            command="REFUSE: command matches dangerous pattern: $pattern"
            break
        fi
    done

    # REFUSE
    if [[ "${(U)command}" == REFUSE:* ]]; then
        print -u2 "\033[0;31mREFUSED:\033[0m ${command#*: }"
        return 0
    fi

    # CLARIFY
    if [[ "${(U)command}" == CLARIFY:* ]]; then
        print -u2 "\033[0;33mCLARIFY:\033[0m ${command#*: }"
        return 0
    fi

    # Shellcheck
    if command -v shellcheck &>/dev/null; then
        local tmp_sh
        tmp_sh=$(mktemp /tmp/pb_check_XXXX.sh)
        print "#!/bin/bash\n$command" > "$tmp_sh"
        if ! shellcheck --shell=bash --severity=warning "$tmp_sh" &>/dev/null; then
            print -u2 "\033[0;33mWarning: shellcheck found issues\033[0m"
            shellcheck --shell=bash --severity=warning "$tmp_sh" 2>&1 | tail -5 >&2
        fi
        rm -f "$tmp_sh"
    fi

    # Display
    print
    print "\033[0;32m  $command\033[0m"
    print

    if [[ "$dry_run" == true ]]; then
        print -u2 "\033[0;36m(dry-run — not executed)\033[0m"
        return 0
    fi

    print -nu2 "Execute? [Enter to run / Ctrl+C to cancel] "
    read -r

    eval "$command"
}
