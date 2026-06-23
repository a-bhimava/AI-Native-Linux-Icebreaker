"""python -m gui — launch the Icebreaker desktop GUI.

Usage:
    python -m gui [--sock PATH] [--light]
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

    app = IcebreakerApp(sock_path=args.sock, dark=not args.light)
    return app.run(None) or 0


if __name__ == "__main__":
    sys.exit(main())
