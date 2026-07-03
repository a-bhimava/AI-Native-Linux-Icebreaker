#!/usr/bin/env python3
"""ib_run.py — send one NL query to the Icebreaker Controller daemon.

Called by ib_trigger.bash (the `#` trigger) and executed directly by the
QEMU gate so the tested path and the shipped path are the same file.

Usage: ib_run.py "<query>" [socket_path]
Exit:  0 = daemon returned a result; 1 = error (message on stderr).
"""
import sys


def _c(code: str, text: str) -> str:
    if not sys.stderr.isatty() and not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ib_run.py '<query>' [socket]", file=sys.stderr)
        return 1
    query = sys.argv[1]
    sock_path = sys.argv[2] if len(sys.argv) > 2 else "/run/icebreaker/controller.sock"

    try:
        from controller.client import DaemonClient
        client = DaemonClient(sock_path)
        client.connect()
        try:
            resp = client.run_turn(query)
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
