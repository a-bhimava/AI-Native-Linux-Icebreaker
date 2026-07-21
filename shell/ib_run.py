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

    # v6.9 Bug A (2026-07-17): the plain shell client can auto-render
    # Tier-0 answers and audit Tier ≥ 1 HITL denials, but has no way to
    # RESPOND to an HITL prompt (no interactive presenter). Cap at 45s —
    # long enough for a slow Tier-0 execution or a daemon-side HITL
    # timeout (33s default) with slack. On timeout, surface a targeted
    # message pointing at the AI Terminal instead of dumping a raw
    # TimeoutError. Real F-61 fix (interactive HITL through this shell
    # path) tracked as Task #152.
    _SHELL_TURN_TIMEOUT = 45.0

    try:
        from controller.client import DaemonClient
        client = DaemonClient(sock_path)
        client.connect()
        try:
            resp = client.run_turn(
                query, context=context or None, timeout=_SHELL_TURN_TIMEOUT,
            )
        finally:
            client.close()
        if "result" in resp:
            out = resp["result"].get("output", "")
            print(out if out else "(done)")
            # v6.12 Fix G (F-87 2026-07-21): if nav.cd (or any future
            # session-mutating tool) set a new session_cwd that differs
            # from the calling shell's $PWD, signal ib_trigger.bash to
            # cd there. Uses a temp-file dropbox because bash can't read
            # a Python subprocess's return value directly — the trigger
            # exports IB_CD_SIGNAL=/tmp/ib_cd.XXXXXX before invoking us,
            # and after we return reads + evals its contents. Never
            # raises: signal-file write failure just means no auto-cd,
            # which degrades to today's behavior.
            _signal = os.environ.get("IB_CD_SIGNAL", "")
            _new_scwd = str(resp["result"].get("session_cwd", "") or "")
            _cur_scwd = str(context.get("cwd", "") or "")
            if _signal and _new_scwd and _new_scwd != _cur_scwd:
                try:
                    with open(_signal, "w") as _f:
                        _f.write(_new_scwd)
                except OSError:
                    pass  # best-effort; ib_trigger validates before cd
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
    except TimeoutError:
        print(
            _c("1;33", "[icebreaker]")
            + f" No response within {_SHELL_TURN_TIMEOUT:.0f}s.",
            file=sys.stderr,
        )
        print(
            _c("1;33", "[icebreaker]")
            + " Likely cause: this Tier ≥ 1 intent needs interactive"
            + " HITL approval, and the plain shell can't render the prompt.",
            file=sys.stderr,
        )
        print(
            _c("1;33", "[icebreaker]")
            + " Use the Icebreaker AI Terminal (Activities → Terminal)"
            + " for Tier ≥ 1 intents. Read-only intents work fine here.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(_c("1;31", "[icebreaker]") + f" {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
