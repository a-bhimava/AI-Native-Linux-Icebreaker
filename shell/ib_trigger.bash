#!/usr/bin/env bash
# ib_trigger.bash — # trigger: type "# <intent>" in any bash shell to run it
#                   through the Icebreaker Controller daemon.
#
# Source from ~/.bashrc:
#   source /usr/share/icebreaker/shell/ib_trigger.bash
#
# Usage:
#   # show disk usage          →  routed through the AI pipeline
#   # list running services    →  routed through the AI pipeline
#
# Design (F-19): NO readline key bindings. Binding \C-j/\C-m to a bind -x
# handler destroyed accept-line and broke ALL command execution; and bind -x
# handlers inside macros don't reliably see READLINE_LINE. Instead we exploit
# the fact that a '#'-prefixed interactive line is a bash comment (a no-op
# that still lands in history): a PROMPT_COMMAND hook inspects the newest
# history entry before each prompt and routes '#' lines to the daemon.
# Enter is untouched — plain commands cannot break, structurally.

# Interactive shells only.
[[ $- == *i* ]] || return 0 2>/dev/null || exit 0

_IB_VENV_PYTHON="${ICEBREAKER_VENV_PYTHON:-/opt/icebreaker/venv/bin/python3}"
_IB_SOCK="${ICEBREAKER_CONTROLLER_SOCK:-/run/icebreaker/controller.sock}"
_IB_RUN="${ICEBREAKER_IB_RUN:-/usr/share/icebreaker/shell/ib_run.py}"

# Seed with the current newest history number so sourcing this file never
# fires on a pre-existing '#' entry.
_IB_LAST_HISTNUM="$(HISTTIMEFORMAT= builtin history 1 | awk '{print $1}')"

_ib_prompt_hook() {
    local entry num line
    entry="$(HISTTIMEFORMAT= builtin history 1)" || return 0
    [[ -n "$entry" ]] || return 0
    num="${entry%%[!\ ]*}"                 # leading spaces
    num="$(awk '{print $1}' <<<"$entry")"
    line="$(sed 's/^ *[0-9]\{1,\} *//' <<<"$entry")"

    # Only fire once per NEW history entry (empty Enter adds no entry).
    [[ "$num" == "$_IB_LAST_HISTNUM" ]] && return 0
    _IB_LAST_HISTNUM="$num"

    [[ "$line" == '#'* ]] || return 0
    local query="${line:1}"
    query="${query## }"
    [[ -n "$query" ]] || return 0

    printf '\033[1;36m[icebreaker]\033[0m %s\n' "$query"

    # V6B Stage 2: collect shell context and export it for ib_run.py to
    # forward in the run_turn RPC. Lets QB resolve "here", "this folder",
    # "the file I was editing" against the actual environment.
    local ib_recent ib_win
    ib_recent="$(HISTTIMEFORMAT= builtin history 6 2>/dev/null | head -5 | sed 's/^ *[0-9]\{1,\} *//')"
    # V6B Stage 3: best-effort active window title via xdotool. Only makes
    # sense with an X display (bare TTY logins skip it silently).
    if [ -n "${DISPLAY:-}" ] && command -v xdotool >/dev/null 2>&1; then
        ib_win="$(xdotool getactivewindow getwindowname 2>/dev/null || true)"
    else
        ib_win=""
    fi
    IB_CWD="$PWD" \
    IB_RECENT="$ib_recent" \
    IB_ACTIVE_WINDOW="$ib_win" \
        "${_IB_VENV_PYTHON}" "${_IB_RUN}" "$query" "$_IB_SOCK"
}

# Prepend to PROMPT_COMMAND (runs before each prompt is drawn).
case ";${PROMPT_COMMAND:-};" in
    *";_ib_prompt_hook;"*) ;;   # already installed — idempotent
    *) PROMPT_COMMAND="_ib_prompt_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
