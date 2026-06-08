"""Outbound payload auditor — runtime defense layer for exit gate G3.

Scans every outbound LLM request payload immediately before SDK
transmission. Raises ``BrainSecurityError`` on any tool/function/
grounding key at any depth. Catches the case where a future SDK
version silently adds a tool field, even when subclass code is correct.

The base class instantiates one ``OutboundPayloadAuditor`` per backend
instance, exposes it as ``self._auditor``, and contractually requires
``_call_provider`` implementations to call ``self._auditor.intercept(
payload)`` immediately before SDK transmission. The base class
``complete()`` method verifies the contract was honored via a
call-count probe — subclasses that skip the auditor raise
``BrainSecurityError``.
"""

from __future__ import annotations

from typing import Final

_FORBIDDEN_KEYS: Final = frozenset(
    {
        "tools",
        "tool_choice",
        "tool_use",
        "tool_calls",
        "functions",
        "function_call",
        "grounding",
        "google_search_retrieval",
        "google_search",
        "file_search",
        "code_interpreter",
        "retrieval",
    }
)


class OutboundPayloadAuditor:
    """Per-backend payload scanner. One instance per ``BrainBackend``.

    ``intercept(payload)`` returns ``None`` on success and raises
    ``BrainSecurityError`` on hit. ``call_count`` is used by the base
    class to verify subclasses actually invoke the auditor.
    """

    __slots__ = ("call_count",)

    def __init__(self) -> None:
        self.call_count = 0

    def intercept(self, payload: object) -> None:
        self.call_count += 1
        self._scan(payload, path="")

    def _scan(self, node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _FORBIDDEN_KEYS:
                    # Inline import: avoids auditor → base import cycle.
                    from .base import BrainSecurityError

                    raise BrainSecurityError(
                        f"G3 violation: forbidden key {key!r} in outbound "
                        f"payload at {path or '/'}"
                    )
                self._scan(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                self._scan(value, f"{path}[{index}]")
