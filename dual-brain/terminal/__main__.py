"""Allow ``python -m terminal`` to launch the AI Terminal."""

import argparse


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
    args = parser.parse_args()

    daemon_client = None
    sock_path = args.sock

    if not sock_path and args.config:
        from controller.config import load
        cfg = load(args.config)
        sock_path = str(cfg.daemon.socket_path)

    if sock_path:
        from .daemon_client import TextualDaemonClient
        daemon_client = TextualDaemonClient(sock_path)
        try:
            daemon_client.connect()
        except Exception:
            daemon_client = None

    from .app import run
    run(daemon_client=daemon_client)


main()
