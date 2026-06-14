"""Configurable keymap for the HITL approval gate.

Maps single-keypress characters to ``Action`` values. Ships with sensible
defaults (numeric + mnemonic + universal bindings); users override via
``[keymap]`` in ``controller.toml``.

Validation rules (enforced at config load, never at prompt time):

* Each binding is exactly one printable, non-control char.
* No key may be bound to two different actions.
* ``Esc`` (``\\x1b``) is hard-reserved to DENY and cannot be remapped.
* ``?`` cannot be unbound from HELP.
* DENY must retain at least one user binding (in addition to Esc).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Mapping, Sequence


@unique
class Action(Enum):
    APPROVE = "approve"
    DENY    = "deny"
    MODIFY  = "modify"
    EXPLAIN = "explain"
    TRUST   = "trust"
    HELP    = "help"


_ESC = "\x1b"

DEFAULTS: dict[Action, tuple[str, ...]] = {
    Action.APPROVE: ("1", "a", "y"),
    Action.DENY:    ("2", "d", "n"),
    Action.MODIFY:  ("3", "m"),
    Action.EXPLAIN: ("4", "e"),
    Action.TRUST:   ("5", "t"),
    Action.HELP:    ("?",),
}

_LABELS: dict[Action, str] = {
    Action.APPROVE: "Approve",
    Action.DENY:    "Deny",
    Action.MODIFY:  "Modify",
    Action.EXPLAIN: "Explain",
    Action.TRUST:   "Trust",
    Action.HELP:    "Help",
}


@dataclass(frozen=True)
class Keymap:
    """Immutable, validated key→action mapping."""

    bindings: dict[Action, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULTS))

    def lookup(self, key: str) -> Action | None:
        if key == _ESC:
            return Action.DENY
        for action, keys in self.bindings.items():
            if key in keys:
                return action
        return None

    def legend(self, *, include_trust: bool = True) -> str:
        parts: list[str] = []
        for action in (Action.APPROVE, Action.DENY, Action.MODIFY, Action.EXPLAIN):
            keys = self.bindings.get(action, ())
            if keys:
                display = "/".join(keys)
                parts.append(f"[{display}] {_LABELS[action]}")
        if include_trust:
            trust_keys = self.bindings.get(Action.TRUST, ())
            if trust_keys:
                display = "/".join(trust_keys)
                parts.append(f"[{display}] {_LABELS[Action.TRUST]}")
        help_keys = self.bindings.get(Action.HELP, ())
        if help_keys:
            display = "/".join(help_keys)
            parts.append(f"[{display}] {_LABELS[Action.HELP]}")
        return "   ".join(parts)

    def actions_for_display(self) -> list[tuple[Action, tuple[str, ...]]]:
        result = []
        for action in Action:
            keys = self.bindings.get(action, ())
            if keys:
                result.append((action, keys))
        return result


def _validate_key(key: str, action: Action, source: str) -> None:
    """Raise ``KeymapValidationError`` if ``key`` is invalid."""
    if len(key) != 1:
        raise KeymapValidationError(
            f"{source}: binding for {action.value!r} must be exactly one character, "
            f"got {key!r} (length {len(key)})"
        )
    cp = ord(key)
    if cp <= 0x20 or cp == 0x7F:
        raise KeymapValidationError(
            f"{source}: binding for {action.value!r} contains control character "
            f"U+{cp:04X} — only printable characters are allowed"
        )
    if key == _ESC:
        raise KeymapValidationError(
            f"{source}: Esc (\\x1b) is hard-reserved for DENY and cannot be bound to "
            f"{action.value!r}"
        )


class KeymapValidationError(Exception):
    """Raised when the keymap configuration is invalid."""


def load_keymap(raw_section: Mapping[str, Sequence[str]] | None) -> Keymap:
    """Build a ``Keymap`` from the ``[keymap]`` TOML section.

    If *raw_section* is ``None`` or empty, returns the default keymap.
    """
    if not raw_section:
        return Keymap()

    merged: dict[Action, tuple[str, ...]] = dict(DEFAULTS)

    for action_name, keys in raw_section.items():
        try:
            action = Action(action_name)
        except ValueError:
            raise KeymapValidationError(
                f"[keymap]: unknown action {action_name!r}. "
                f"Valid actions: {', '.join(a.value for a in Action)}"
            ) from None

        if not isinstance(keys, (list, tuple)):
            raise KeymapValidationError(
                f"[keymap]: bindings for {action_name!r} must be a list of strings, "
                f"got {type(keys).__name__}"
            )

        normed: list[str] = []
        for k in keys:
            if not isinstance(k, str):
                raise KeymapValidationError(
                    f"[keymap]: each binding must be a string, got {type(k).__name__} "
                    f"in {action_name!r}"
                )
            _validate_key(k, action, "[keymap]")
            normed.append(k)

        if not normed:
            raise KeymapValidationError(
                f"[keymap]: action {action_name!r} must have at least one binding"
            )
        merged[action] = tuple(normed)

    _validate_no_collisions(merged)
    _validate_invariants(merged)
    return Keymap(bindings=merged)


def _validate_no_collisions(bindings: dict[Action, tuple[str, ...]]) -> None:
    seen: dict[str, Action] = {}
    for action, keys in bindings.items():
        for key in keys:
            k_lower = key.lower()
            k_upper = key.upper()
            for variant in {key, k_lower, k_upper}:
                if variant in seen and seen[variant] != action:
                    raise KeymapValidationError(
                        f"[keymap]: key {key!r} collides with {seen[variant].value!r} "
                        f"(already bound) — each key must map to exactly one action"
                    )
            seen[key] = action


def _validate_invariants(bindings: dict[Action, tuple[str, ...]]) -> None:
    deny_keys = bindings.get(Action.DENY, ())
    if not deny_keys:
        raise KeymapValidationError(
            "[keymap]: DENY must retain at least one user binding "
            "(in addition to the hard-reserved Esc key)"
        )

    help_keys = bindings.get(Action.HELP, ())
    if not help_keys or "?" not in help_keys:
        raise KeymapValidationError(
            "[keymap]: '?' must remain bound to HELP — it is the universal "
            "help affordance and cannot be unbound"
        )
