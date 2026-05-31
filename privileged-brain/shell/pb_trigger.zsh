#!/usr/bin/env zsh
# pb_trigger.zsh — Privileged Brain shell trigger (zsh)
# Identical logic to pb_trigger.bash — add to your .zshrc with:
#   source /path/to/privileged-brain/shell/pb_trigger.zsh
# Then use: pb "show disk usage"

# ── Configuration ──────────────────────────────────────────────────────────────
: "${PB_MODEL_PATH:="$HOME/models/privileged-brain-awq.gguf"}"
PB_SCRIPT_DIR="${0:A:h}"
: "${PB_GRAMMAR:="$PB_SCRIPT_DIR/../inference/grammar/bash_cot.gbnf"}"
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

    if ! command -v llama-cli &>/dev/null && ! command -v llama-run &>/dev/null; then
        print -u2 "\033[0;31mError:\033[0m llama-cli not found. Install llama.cpp first."
        return 1
    fi
    if [[ ! -f "$PB_MODEL_PATH" ]]; then
        print -u2 "\033[0;31mError:\033[0m Model not found at: $PB_MODEL_PATH"
        return 1
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

    local llama_bin
    llama_bin=$(command -v llama-cli 2>/dev/null || command -v llama-run 2>/dev/null)

    local grammar_args=()
    [[ -f "$PB_GRAMMAR" ]] && grammar_args=("--grammar-file" "$PB_GRAMMAR")

    print -u2 "\033[0;36mThinking...\033[0m"

    local raw_output
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
