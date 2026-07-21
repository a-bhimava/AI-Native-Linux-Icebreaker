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
    local ib_recent
    ib_recent="$(HISTTIMEFORMAT= builtin history 6 2>/dev/null | head -5 | sed 's/^ *[0-9]\{1,\} *//')"
    # V6.3 Stage 3: best-effort focused window title. wmctrl -l lists windows;
    # the first row is topmost/focused under a stacking WM. Empty on failure.
    local ib_window
    ib_window="$(command -v wmctrl >/dev/null 2>&1 && wmctrl -l 2>/dev/null | head -1 | awk '{$1=$2=$3=""; sub(/^ +/, ""); print}' || true)"
    # v6.12 Fix G (F-87 2026-07-21): create a per-turn signal file. If
    # the daemon returns a session_cwd that differs from $PWD (nav.cd
    # fired), ib_run.py writes the new path here; we validate and cd.
    # This closes the design gap live UTM Stage E surfaced: nav.cd used
    # to update Icebreaker's session_cwd but never bash's $PWD, so
    # `# take me to Downloads` followed by plain `ls` still showed home.
    local ib_cd_signal
    ib_cd_signal="$(mktemp /tmp/ib_cd.XXXXXX 2>/dev/null)" || ib_cd_signal=""

    IB_CWD="$PWD" \
    IB_RECENT="$ib_recent" \
    IB_ACTIVE_WINDOW="$ib_window" \
    IB_CD_SIGNAL="$ib_cd_signal" \
        "${_IB_VENV_PYTHON}" "${_IB_RUN}" "$query" "$_IB_SOCK"

    # Auto-cd on nav.cd success. Blocklist (not allowlist) validation on
    # top of nav.cd's own daemon-side path validation: reject only truly
    # dangerous characters — shell metachars, NUL, control bytes. Allows
    # spaces, unicode, parens, and every legitimate filename character
    # so `# take me to My Documents` actually works. On rejection print
    # a stderr diagnostic so the user knows why the cd didn't happen
    # (D-3 ct-scan finding: silent rejection is worse than no auto-cd).
    if [ -n "$ib_cd_signal" ] && [ -s "$ib_cd_signal" ]; then
        local ib_cd_target
        ib_cd_target="$(head -c 4096 "$ib_cd_signal")"
        # Reject: must start with /, must not contain any of ; & | ` $ < > * ? ! " ' \ or newline/tab/NUL
        if [[ "$ib_cd_target" == /* ]] \
                && ! [[ "$ib_cd_target" == *[$';&|`$<>*?!"'"'"'\\'$'\n\t\0']* ]]; then
            if [ -d "$ib_cd_target" ]; then
                cd "$ib_cd_target" 2>/dev/null || \
                    printf '\033[1;33m[icebreaker]\033[0m cd %q failed (permission?)\n' "$ib_cd_target" >&2
            else
                printf '\033[1;33m[icebreaker]\033[0m cd target %q is not a directory\n' "$ib_cd_target" >&2
            fi
        else
            printf '\033[1;33m[icebreaker]\033[0m cd target %q rejected (dangerous characters)\n' "$ib_cd_target" >&2
        fi
    fi
    [ -n "$ib_cd_signal" ] && rm -f "$ib_cd_signal"
}

# Prepend to PROMPT_COMMAND (runs before each prompt is drawn).
case ";${PROMPT_COMMAND:-};" in
    *";_ib_prompt_hook;"*) ;;   # already installed — idempotent
    *) PROMPT_COMMAND="_ib_prompt_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
