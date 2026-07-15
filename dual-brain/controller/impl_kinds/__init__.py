"""v6.9 Scope O Layer 2 impl.kind primitives.

Each impl.kind module exports a single ``dispatch(manifest, params, ctx)``
function that receives:

  manifest: the parsed, validated manifest dict.
  params:   the intent's params dict (already validated against
            manifest.param_schema by ManifestRegistry.dispatch).
  ctx:      a DispatchContext dataclass carrying the collaborators the
            impl needs (session, audit_log, mcpd_client) without leaking
            the whole Controller. Kind-specific impls read only what
            they need.

Returns an ImplResult with .stdout (str; goes into audit + becomes
$STEP_N_STDOUT for plan mode) and .metadata (dict; extra fields the
audit line carries, e.g. old_cwd/new_cwd for nav.cd).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DispatchContext:
    """Narrow façade the impl.kinds see. Deliberately excludes the
    Controller itself and any dispatch machinery so impls can't reach
    back into main.py and re-enter the pipeline."""
    session: Any                                  # SessionState
    audit_log: Any = None                         # AuditLog | None
    mcpd_client: Any = None                       # McpdClient | None (mcpd-side kinds only)


@dataclass(frozen=True)
class ImplResult:
    stdout: str
    metadata: dict[str, Any] = field(default_factory=dict)
