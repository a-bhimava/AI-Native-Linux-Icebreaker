"""Credential leak defenses for BrainBackend.

Three primitives:

* ``sanitize_exception(exc) -> str`` — point-of-use scrubber. Concrete
  backends wrap every SDK call in ``raise BrainProviderError(
  sanitize_exception(e)) from None`` so credential-bearing exception
  text never reaches the orchestrator.
* ``SecretRef(env_var_name)`` — wrapper that holds only the env var
  *name*. ``.reveal()`` reads ``os.environ`` on demand; ``__repr__`` and
  ``__str__`` mask the value. Used in ``BackendConfig.api_key`` so the
  raw string never sits in a long-lived attribute.
* ``KeyRedactionFilter`` + ``install_root_redaction_filter()`` —
  catch-all root-logger filter. Installed once by ``config.load()``;
  scrubs every ``LogRecord`` before any handler sees it.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final

SECRET_PATTERNS: Final = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-\.]{30,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
)


def _redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def sanitize_exception(exc: BaseException) -> str:
    """Stringify ``exc`` and redact every credential-shaped substring.

    Use at the SDK boundary as ``raise BrainProviderError(
    sanitize_exception(e)) from None``. ``from None`` is critical:
    without it, Python's chained-traceback machinery would re-expose the
    original SDK exception (which may carry the ``Authorization`` header)
    via ``__context__``.
    """
    return _redact(f"{type(exc).__name__}: {exc}")


class SecretRef:
    """Env-var-backed credential. Holds the env var *name* only.

    ``.reveal()`` reads ``os.environ`` on every call so the value never
    becomes a long-lived attribute. ``__repr__``/``__str__`` mask the
    value, so accidental f-string interpolation cannot leak it.
    """

    __slots__ = ("env_var_name",)

    def __init__(self, env_var_name: str) -> None:
        if not env_var_name:
            raise ValueError("env_var_name must be a non-empty string")
        self.env_var_name = env_var_name

    def reveal(self) -> str:
        # Inline import: avoids a sanitize → base import cycle.
        from .base import BrainConfigError

        value = os.environ.get(self.env_var_name)
        if not value:
            raise BrainConfigError(
                f"required env var not set: {self.env_var_name}"
            )
        return value

    def __repr__(self) -> str:
        return f"SecretRef(env={self.env_var_name}, value=********)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, SecretRef)
            and self.env_var_name == other.env_var_name
        )

    def __hash__(self) -> int:
        return hash((SecretRef, self.env_var_name))


class KeyRedactionFilter(logging.Filter):
    """Root-logger ``logging.Filter`` that redacts API-key-shaped strings.

    Scans ``LogRecord.msg`` and ``LogRecord.args`` before any handler
    formats the record. Catches the case where third-party code or a
    future bug does ``logging.error(f"failed with {api_key}")``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    _redact(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


def install_root_redaction_filter() -> None:
    """Idempotently attach a single ``KeyRedactionFilter`` to the root logger.

    Called once from ``config.load()``. Safe to call repeatedly.
    """
    root = logging.getLogger()
    if not any(isinstance(f, KeyRedactionFilter) for f in root.filters):
        root.addFilter(KeyRedactionFilter())
