#!/usr/bin/env python3
"""ib_run.py — send one NL query to the Icebreaker Controller daemon.

Called by ib_trigger.bash (the `#` trigger) and executed directly by the
QEMU gate so the tested path and the shipped path are the same file.

V6B Stage 2: collect shell context (cwd, recent commands, user, hostname)
from the caller's environment and forward it in the run_turn RPC so the
Quarantined Brain can resolve ambiguous references like "here", "this
folder", "the file I was editing".

Usage: ib_run.py "<query>" [socket_path]
Exit:  0 = daemon returned a result; 1 = error (message on stderr).
"""
import os
import socket as _socket
import sys


def _c(code: str, text: str) -> str:
    if not sys.stderr.isatty() and not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _collect_context() -> dict:
    """Best-effort shell state — never raises, always returns a dict.

    Fields the daemon (session.ShellContext.from_params) will accept:
      cwd, user, hostname, recent_commands, active_window

    Sources (in order of preference):
      cwd:     ib_trigger.bash exports IB_CWD before invoking; else os.getcwd()
      user:    $USER / $LOGNAME
      hostname: socket.gethostname() (cheap, no subprocess)
      recent_commands: ib_trigger.bash exports IB_RECENT (newline-joined)
      active_window: env IB_ACTIVE_WINDOW (Stage 3 will populate via wmctrl)
    """
    ctx: dict = {}
    try:
        ctx["cwd"] = os.environ.get("IB_CWD") or os.getcwd()
    except Exception:
        pass
    ctx["user"] = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    try:
        ctx["hostname"] = _socket.gethostname()
    except Exception:
        ctx["hostname"] = ""
    recent = os.environ.get("IB_RECENT", "")
    if recent:
        # newline-separated, oldest first, up to 5
        rc = [ln.strip() for ln in recent.splitlines() if ln.strip()]
        ctx["recent_commands"] = rc[-5:]
    win = os.environ.get("IB_ACTIVE_WINDOW", "").strip()
    if win:
        ctx["active_window"] = win
    return ctx


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ib_run.py '<query>' [socket]", file=sys.stderr)
        return 1
    query = sys.argv[1]
    sock_path = sys.argv[2] if len(sys.argv) > 2 else "/run/icebreaker/controller.sock"
    context = _collect_context()

    try:
        from controller.client import DaemonClient
        client = DaemonClient(sock_path)
        client.connect()
        try:
            resp = client.run_turn(query, context=context or None)
        finally:
            client.close()
        if "result" in resp:
            out = resp["result"].get("output", "")
            print(out if out else "(done)")
            return 0
        msg = resp.get("error", {}).get("message", "unknown error")
        print(_c("1;31", "[error]") + f" {msg}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        print(_c("1;33", "[icebreaker]") + f" Daemon not reachable at {sock_path}: {exc}",
              file=sys.stderr)
        print(_c("1;33", "[icebreaker]") + " Check: systemctl status icebreaker-controller",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(_c("1;31", "[icebreaker]") + f" {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
