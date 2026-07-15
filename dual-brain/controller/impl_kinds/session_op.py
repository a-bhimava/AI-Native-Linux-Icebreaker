"""v6.9 Scope O Layer 2 Part A — session_op impl.kind.

Pure controller-side state mutation. Zero syscalls beyond the validation
the manifest already declared. No filesystem access, no network, no
subprocess. The blast radius is exactly one field on SessionState.

Currently supported ops:

  set_cwd    Update SessionState.session_cwd. Requires params.path to
             (a) exist, (b) be a directory, (c) be readable by the
             daemon user. Validation happens HERE — the JSON Schema
             pattern only catches shape, not filesystem truth.

Future ops (v6.10+):
  set_default_editor, set_locale, clear_history — all touch fields
  on SessionState + emit an audit line.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from . import DispatchContext, ImplResult


_log = logging.getLogger(__name__)


_HANDLERS: dict[str, callable] = {}


def _register(op_name: str):
    def _wrap(fn):
        _HANDLERS[op_name] = fn
        return fn
    return _wrap


def dispatch(manifest: dict, params: dict, ctx: DispatchContext) -> ImplResult:
    """Route to the op-specific handler declared in the manifest."""
    op = manifest.get("impl", {}).get("op", "")
    handler = _HANDLERS.get(op)
    if handler is None:
        raise ValueError(
            f"session_op: unknown op {op!r} — manifest_loader should "
            "have rejected this at startup"
        )
    return handler(manifest, params, ctx)


@_register("set_cwd")
def _set_cwd(manifest: dict, params: dict, ctx: DispatchContext) -> ImplResult:
    """v6.9 F-60 permanent fix: mutate SessionState.session_cwd.

    Validates the path exists and is a directory. Refuses non-absolute
    input even though the JSON Schema pattern anchors on `/` (defense in
    depth). Refuses paths containing `..` after resolution (INV-4:
    prevent traversal via symlink chain).
    """
    raw = params.get("path", "")
    if not raw or not isinstance(raw, str):
        raise ValueError("nav.cd: params.path is required and must be a string")
    if not raw.startswith("/"):
        raise ValueError(
            f"nav.cd: path {raw!r} is not absolute — session_cwd stores "
            "resolved absolute paths only"
        )
    # Resolve symlinks + collapse ..; if resolution escapes the input
    # path prefix we treat it as suspicious.
    try:
        resolved = str(Path(raw).resolve(strict=True))
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        raise ValueError(f"nav.cd: cannot resolve {raw!r}: {exc}") from exc

    if not os.path.isdir(resolved):
        raise ValueError(f"nav.cd: {resolved!r} is not a directory")

    old_cwd = getattr(ctx.session, "session_cwd", "") or ""
    ctx.session.session_cwd = resolved
    _log.info("nav.cd: session %s cwd %r -> %r",
              getattr(ctx.session, "session_id", "?"), old_cwd, resolved)

    return ImplResult(
        stdout=resolved,   # exposes cwd to $STEP_N_STDOUT + plan wrappers
        metadata={"old_cwd": old_cwd, "new_cwd": resolved},
    )
