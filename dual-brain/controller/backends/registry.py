"""Backend registry + factory.

Concrete backends self-register via ``@register_backend("local")``.
``make_backend(config)`` instantiates by ``config.qb.name``. This
decouples ``M2.12`` orchestration from importing each concrete backend
module directly — the registry is populated as a side effect of
importing the concrete modules.

Mirrors the module-singleton + module-fn delegate pattern in
``intent_store.py:126, 129-142``.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import BrainBackend, BrainConfigError


_REGISTRY: dict[str, type[BrainBackend]] = {}


def register_backend(
    name: str,
) -> Callable[[type[BrainBackend]], type[BrainBackend]]:
    """Decorator: register a concrete ``BrainBackend`` subclass by name.

    Raises ``BrainConfigError`` if a name is registered twice.
    """
    if not name:
        raise BrainConfigError("backend name must be a non-empty string")

    def decorator(cls: type[BrainBackend]) -> type[BrainBackend]:
        if name in _REGISTRY:
            raise BrainConfigError(
                f"backend {name!r} already registered to "
                f"{_REGISTRY[name].__name__}; cannot re-register as "
                f"{cls.__name__}"
            )
        if not issubclass(cls, BrainBackend):
            raise BrainConfigError(
                f"@register_backend({name!r}) target {cls.__name__} is "
                "not a BrainBackend subclass"
            )
        _REGISTRY[name] = cls
        cls.backend_name = name
        return cls

    return decorator


def make_backend(config: Any) -> BrainBackend:
    """Instantiate the backend named by ``config.qb.name``.

    ``config.qb`` must be a ``BackendConfig`` (see ``controller.config``).
    Raises ``BrainConfigError`` if the name is not registered. The
    concrete backend's module must be imported before ``make_backend``
    is called (registration is a side effect of import).
    """
    qb_name = config.qb.name
    cls = _REGISTRY.get(qb_name)
    if cls is None:
        registered = sorted(_REGISTRY.keys()) or ["(none registered)"]
        raise BrainConfigError(
            f"unknown backend {qb_name!r}; registered: {registered}. "
            "Has the concrete backend module been imported?"
        )
    return cls(config.qb)


def registered_backends() -> tuple[str, ...]:
    """Returns the tuple of currently-registered backend names. For tests + introspection."""
    return tuple(sorted(_REGISTRY.keys()))


def _reset_registry_for_tests() -> None:
    """Test-only: clear the registry. NEVER call from production code."""
    _REGISTRY.clear()
