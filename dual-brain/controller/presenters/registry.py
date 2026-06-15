"""Presenter registry + factory.

Mirrors the ``@register_backend`` pattern from ``backends/registry.py``.
Concrete presenters self-register via ``@register_presenter("name")``.
``make_presenter(config, keymap)`` instantiates by ``config.presenter``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..hitl import HitlPresenter
from ..keymap import Keymap


_REGISTRY: dict[str, type[HitlPresenter]] = {}


def register_presenter(
    name: str,
) -> Callable[[type[HitlPresenter]], type[HitlPresenter]]:
    """Decorator: register a concrete ``HitlPresenter`` subclass by name."""
    if not name:
        raise ValueError("presenter name must be a non-empty string")

    def decorator(cls: type[HitlPresenter]) -> type[HitlPresenter]:
        if name in _REGISTRY:
            raise ValueError(
                f"presenter {name!r} already registered to "
                f"{_REGISTRY[name].__name__}; cannot re-register as "
                f"{cls.__name__}"
            )
        if not issubclass(cls, HitlPresenter):
            raise TypeError(
                f"@register_presenter({name!r}) target {cls.__name__} is "
                "not a HitlPresenter subclass"
            )
        _REGISTRY[name] = cls
        return cls

    return decorator


def make_presenter(
    name: str,
    *,
    keymap: Optional[Keymap] = None,
) -> HitlPresenter:
    """Instantiate the presenter named by config."""
    cls = _REGISTRY.get(name)
    if cls is None:
        registered = sorted(_REGISTRY.keys()) or ["(none registered)"]
        raise ValueError(
            f"unknown presenter {name!r}; registered: {registered}. "
            "Has the concrete presenter module been imported?"
        )
    return cls(keymap=keymap)


def registered_presenters() -> tuple[str, ...]:
    """Returns the tuple of currently-registered presenter names."""
    return tuple(sorted(_REGISTRY.keys()))


def _reset_registry_for_tests() -> None:
    """Test-only: clear the registry. NEVER call from production code."""
    _REGISTRY.clear()
