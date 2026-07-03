#!/usr/bin/env bash
# ib_trigger.bash — # trigger: type "# <intent>" in any bash shell to run it
#                   through the Icebreaker Controller daemon.
#
# Source from ~/.bashrc:
#   source /usr/share/icebreaker/shell/ib_trigger.bash
#
# Usage:
#   # show disk usage          →  runs df -h via the AI pipeline
#   # list running services    →  runs systemctl list-units --state=running
#
# The trigger intercepts Enter when the line starts with "#", strips the prefix,
# sends the text to the Controller daemon (QB → PB → mcpd) via ib_run.py, and
# prints the result. Lines NOT starting with "#" are executed normally by bash.

# Interactive shells only — bind is meaningless (and noisy) otherwise.
[[ $- == *i* ]] || return 0 2>/dev/null || exit 0

_IB_VENV_PYTHON="${ICEBREAKER_VENV_PYTHON:-/opt/icebreaker/venv/bin/python3}"
_IB_SOCK="${ICEBREAKER_CONTROLLER_SOCK:-/run/icebreaker/controller.sock}"
_IB_RUN="${ICEBREAKER_IB_RUN:-/usr/share/icebreaker/shell/ib_run.py}"

_ib_hash_trigger() {
    local buf="${READLINE_LINE}"

    # Only intercept lines that start with "#"
    [[ "$buf" != '#'* ]] && return

    local query="${buf:1}"
    query="${query## }"   # strip leading space

    # Empty "#" — just clear the line
    if [[ -z "$query" ]]; then
        READLINE_LINE=""
        READLINE_POINT=0
        return
    fi

    READLINE_LINE=""
    READLINE_POINT=0
    echo ""
    printf '\033[1;36m[icebreaker]\033[0m %s\n' "$query"

    "${_IB_VENV_PYTHON}" "${_IB_RUN}" "$query" "$_IB_SOCK"
}

# Bind Ctrl-J to the trigger function
bind -x '"\C-j": _ib_hash_trigger'

# Bind Enter (Ctrl-M) to execute Ctrl-J, then accept the line (Ctrl-M)
bind '"\C-m": "\C-j\n"'
