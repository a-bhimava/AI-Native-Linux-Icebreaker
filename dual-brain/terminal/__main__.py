"""Allow ``python -m terminal`` to launch the AI Terminal."""

import argparse
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="Icebreaker AI Terminal")
    parser.add_argument(
        "--sock", metavar="PATH",
        help="Connect to daemon at this UNIX socket path",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="Path to controller.toml (used to resolve socket path)",
    )
    parser.add_argument(
        "--connect-timeout", type=float, default=20.0, metavar="SECONDS",
        help="How long to retry the daemon connection before starting "
             "in degraded mode (default: 20)",
    )
    args = parser.parse_args()

    daemon_client = None
    startup_warning = None
    sock_path = args.sock

    if not sock_path and args.config:
        from controller.config import load
        cfg = load(args.config)
        sock_path = str(cfg.daemon.socket_path)

    if sock_path:
        # F-7: the daemon may still be starting when the TUI launches
        # (autostart races systemd). Retry for a bounded window, and on
        # failure LAUNCH ANYWAY with a visible warning — never silently
        # degrade (R6).
        from .daemon_client import TextualDaemonClient
        deadline = time.monotonic() + max(args.connect_timeout, 0.0)
        last_err = None
        while True:
            client = TextualDaemonClient(sock_path)
            try:
                client.connect()
                daemon_client = client
                break
            except Exception as exc:
                last_err = exc
                if time.monotonic() >= deadline:
                    startup_warning = (
                        f"Daemon unreachable at {sock_path}: "
                        f"{type(last_err).__name__}: {last_err} — "
                        "NL mode disabled. Check: "
                        "systemctl status icebreaker-controller"
                    )
                    print(f"WARN: {startup_warning}", file=sys.stderr)
                    break
                time.sleep(1.0)

    from .app import run
    run(daemon_client=daemon_client, startup_warning=startup_warning)


main()
