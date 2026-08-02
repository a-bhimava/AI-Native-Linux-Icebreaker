"""Presenter package — pluggable HITL approval UIs.

Import concrete presenters here for self-registration via
``@register_presenter(name)``. ``make_presenter()`` dispatches by
config name.
"""

from .registry import make_presenter, register_presenter, registered_presenters
from .terminal import TerminalPresenter
from . import screen_reader as _sr  # noqa: F401 — self-registration
from . import gtk as _gtk  # noqa: F401 — self-registration (PyGObject checked at instantiation)

# Fix V.5b (v6.15): register the annotated_screenshot presenter so
# `[hitl] presenter = "annotated_screenshot"` in controller.toml can
# find it. Import inside try/except so a broken `gui` package (missing
# widgets, corrupt PyGObject, etc.) doesn't break every other presenter.
# Module-level imports here are safe — gi is only touched at __init__
# time inside the presenter class, not at import time.
try:
    from gui.hitl import dialog as _libadwaita_dialog  # noqa: F401 — registration
    from gui.hitl import annotated_dialog as _annotated_dialog  # noqa: F401
except ImportError:
    pass

__all__ = [
    "TerminalPresenter",
    "make_presenter",
    "register_presenter",
    "registered_presenters",
]
