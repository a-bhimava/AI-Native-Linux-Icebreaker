"""GUI Agent — AT-SPI + D-Bus GUI automation for Icebreaker.

Separate sandboxed process (ADR-11). Communicates with the Controller
daemon over AF_UNIX JSON-RPC. Linux-only (requires AT-SPI, D-Bus,
Landlock kernel 5.13+).
"""

__version__ = "0.1.0"
