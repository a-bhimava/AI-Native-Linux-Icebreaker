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
# sends the text to the Controller daemon (QB → PB → mcpd), and prints the result.
# Lines NOT starting with "#" are executed normally by bash.

_IB_VENV_PYTHON="${ICEBREAKER_VENV_PYTHON:-/opt/icebreaker/venv/bin/python3}"
_IB_SOCK="${ICEBREAKER_CONTROLLER_SOCK:-/run/icebreaker/controller.sock}"

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

    "${_IB_VENV_PYTHON}" - "$query" "$_IB_SOCK" <<'PYEOF'
import sys

query = sys.argv[1]
sock_path = sys.argv[2]

try:
    from controller.client import DaemonClient
    client = DaemonClient(sock_path)
    client.connect()
    resp = client.run_turn(query)
    client.close()
    if "result" in resp:
        out = resp["result"].get("output", "")
        if out:
            print(out)
        else:
            print("(done)")
    else:
        msg = resp.get("error", {}).get("message", "unknown error")
        print(f"\033[1;31m[error]\033[0m {msg}", file=sys.stderr)
except FileNotFoundError:
    print(
        f"\033[1;33m[icebreaker]\033[0m Daemon socket not found: {sock_path}",
        file=sys.stderr,
    )
    print(
        "\033[1;33m[icebreaker]\033[0m Start the daemon: systemctl start icebreaker-controller",
        file=sys.stderr,
    )
except Exception as exc:
    print(f"\033[1;31m[icebreaker]\033[0m {exc}", file=sys.stderr)
PYEOF
}

bind -x '"\C-m": _ib_hash_trigger'   # Enter
bind -x '"\C-j": _ib_hash_trigger'   # Ctrl+J (also Enter in some terminals)
