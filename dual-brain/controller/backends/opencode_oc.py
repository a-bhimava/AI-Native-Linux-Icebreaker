"""``opencode_oc`` backend — placeholder for the v6.13_OC OpenCode edition.

In the OC edition, opencode is the actual Quarantined Brain — spawned
externally by the user via ``icebreaker-oc``. The Python daemon never
invokes an LLM directly. But every ``ControllerConfig`` still needs a
resolvable ``qb`` entry (via :func:`~controller.backends.make_backend`),
so this module ships a no-op backend that satisfies the type contract
and fails loudly if anything ever calls into it.

Any code path that lands here in OC mode is a bug — probably a leftover
current-edition branch that should have been gated on
``cfg.qb.name != 'opencode_oc'``. The loud failure surfaces the bug
at the first turn attempt instead of silently returning empty responses.
"""

from __future__ import annotations

from typing import Any

from .base import BrainBackend, RequestEnvelope
from .registry import register_backend


@register_backend("opencode_oc")
class NoOpBrainBackend(BrainBackend):
    """No-op QB for the OC edition. Fails loudly if ``.complete()`` is
    ever called — opencode is the real QB and lives outside the Python
    daemon."""

    __slots__ = ()
    backend_name = "opencode_oc"

    def _call_provider(
        self, envelope: RequestEnvelope,
    ) -> tuple[str, int, int]:
        raise RuntimeError(
            "opencode_oc backend has no in-process LLM. If you see this, "
            "the Controller is trying to invoke QB from a code path that "
            "should have been gated on cfg.qb.name != 'opencode_oc'. "
            "opencode is the actual QB in the OC edition — it runs "
            "externally as the user-facing TUI. See "
            "docs/v6.x_OC/IMPLEMENTATION_PLAN.md § Fix Q."
        )
