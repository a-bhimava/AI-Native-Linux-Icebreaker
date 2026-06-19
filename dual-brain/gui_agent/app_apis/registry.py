"""App API registry + factory.

Mirrors the ``@register_presenter`` pattern from
``controller/presenters/registry.py``. Concrete app APIs self-register
via ``@register_app_api("name")``. ``get_app_api(window_title)``
returns the first registered API whose ``supports()`` matches, or
``None`` if no match (caller falls back to GenericAtSpiApi).
"""

from __future__ import annotations

from typing import Callable, Optional

from .base import AppApi


_REGISTRY: dict[str, type[AppApi]] = {}


def register_app_api(
    name: str,
) -> Callable[[type[AppApi]], type[AppApi]]:
    """Decorator: register a concrete ``AppApi`` subclass by name."""
    if not name:
        raise ValueError("app API name must be a non-empty string")

    def decorator(cls: type[AppApi]) -> type[AppApi]:
        if name in _REGISTRY:
            raise ValueError(
                f"app API {name!r} already registered to "
                f"{_REGISTRY[name].__name__}; cannot re-register as "
                f"{cls.__name__}"
            )
        if not issubclass(cls, AppApi):
            raise TypeError(
                f"@register_app_api({name!r}) target {cls.__name__} is "
                "not an AppApi subclass"
            )
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_app_api(window_title: str) -> Optional[AppApi]:
    """Return an instantiated AppApi for the given window title, or None."""
    for _name, cls in _REGISTRY.items():
        instance = cls()
        if instance.supports(window_title):
            return instance
    return None


def list_app_apis() -> tuple[str, ...]:
    """Return the tuple of currently-registered app API names."""
    return tuple(sorted(_REGISTRY.keys()))


def _reset_registry_for_tests() -> None:
    """Test-only: clear the registry. NEVER call from production code."""
    _REGISTRY.clear()
