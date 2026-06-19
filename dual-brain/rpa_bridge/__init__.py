"""RPA Bridge — Robot Framework UI automation for Icebreaker.

Separate sandboxed process (ADR-13, ADR-17). Communicates with the
Controller daemon over AF_UNIX JSON-RPC. Linux-only (requires
Landlock kernel 5.13+, /dev/uinput group access).

RPA is always Tier 3 — every operation requires HITL approval.
"""

__version__ = "0.1.0"
