"""python -m gui — launch the Icebreaker desktop GUI.

Usage:
    python -m gui [--chatbot | --settings | --wizard | --audit] [--sock PATH] [--light]
"""

from __future__ import annotations

import argparse
import sys

from .app import IcebreakerApp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gui",
        description="Icebreaker Desktop GUI (GTK4 + LibAdwaita)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chatbot", action="store_true", help="Open chatbot window (default).")
    mode.add_argument("--settings", action="store_true", help="Open settings panel.")
    mode.add_argument("--wizard", action="store_true", help="Open first-boot wizard.")
    mode.add_argument("--audit", action="store_true", help="Open audit log viewer.")
    parser.add_argument(
        "--sock",
        metavar="PATH",
        default="",
        help="AF_UNIX socket path for the controller daemon.",
    )
    parser.add_argument(
        "--light",
        action="store_true",
        help="Use the light color scheme (default: dark).",
    )
    args = parser.parse_args(argv)

    window_mode = "chatbot"
    if args.settings:
        window_mode = "settings"
    elif args.wizard:
        window_mode = "wizard"
    elif args.audit:
        window_mode = "audit"

    app = IcebreakerApp(sock_path=args.sock, dark=not args.light, window_mode=window_mode)
    return app.run(None) or 0


if __name__ == "__main__":
    sys.exit(main())
