"""Terminal presenter — re-exports from ``hitl.py`` with registry wiring.

The actual ``TerminalPresenter`` implementation lives in ``hitl.py`` to
avoid a circular import (``hitl.py`` defines both the ABC and the
default concrete implementation). This module registers it and provides
the re-export for the presenter package.
"""

from __future__ import annotations

from ..hitl import TerminalPresenter
from .registry import register_presenter

register_presenter("terminal")(TerminalPresenter)

__all__ = ["TerminalPresenter"]
