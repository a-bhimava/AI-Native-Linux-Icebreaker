"""Presenter package — pluggable HITL approval UIs.

Import concrete presenters here for self-registration via
``@register_presenter(name)``. ``make_presenter()`` dispatches by
config name.
"""

from .registry import make_presenter, register_presenter, registered_presenters
from .terminal import TerminalPresenter
from . import screen_reader as _sr  # noqa: F401 — self-registration
from . import gtk as _gtk  # noqa: F401 — self-registration (PyGObject checked at instantiation)

__all__ = [
    "TerminalPresenter",
    "make_presenter",
    "register_presenter",
    "registered_presenters",
]
