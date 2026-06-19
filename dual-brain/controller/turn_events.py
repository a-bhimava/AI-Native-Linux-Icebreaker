"""TurnEvent protocol — streaming events for the Controller pipeline.

The ``run_turn_streaming()`` generator yields ``TurnEvent`` objects so the
REPL can show pipeline progress and stream QB summarization tokens. The
existing synchronous ``run_turn()`` is unchanged (BP-2 backward compat).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

from .main import TurnResult

_COT_STEP_STATES = frozenset({"pending", "active", "done", "failed"})


@dataclass(frozen=True)
class ProgressEvent:
    """Emitted before each pipeline step begins."""

    step_name: str
    step_label: str
    step_index: int
    total_steps: int
    elapsed_ms: float


@dataclass(frozen=True)
class TokenEvent:
    """Emitted during QB summarization streaming (Step 11)."""

    token: str
    accumulated: str
    final: bool


@dataclass(frozen=True)
class ResultEvent:
    """Emitted once at pipeline completion."""

    result: TurnResult


@dataclass(frozen=True)
class ErrorEvent:
    """Emitted on error or cancellation."""

    error_type: str
    message: str
    cancelled_at_step: str = ""


@dataclass(frozen=True)
class InfoEvent:
    """Emitted for informational messages (explain, modify, trust feedback)."""

    message: str


@dataclass(frozen=True)
class CotEvent:
    """Chain-of-Thought event for the companion panel (ADR-16 / ADR-19).

    Emitted at each pipeline step with structured data derived from the
    step output.  ``step_state`` drives visual card rendering:
    pending → active → done / failed.
    """

    step_index: int
    step_name: str
    step_state: str
    heading: str
    body: str
    data: dict[str, Any]
    timestamp_ms: float

    def __post_init__(self) -> None:
        if self.step_state not in _COT_STEP_STATES:
            raise ValueError(
                f"step_state must be one of {sorted(_COT_STEP_STATES)}, "
                f"got {self.step_state!r}"
            )


_GUI_PHASES = frozenset({"preview", "executing", "complete"})


@dataclass(frozen=True)
class GuiEvent:
    """GUI automation event for the companion panel.

    Emitted during GUI tool dispatch: preview (before-screenshot),
    executing (action in progress), complete (after-screenshot).
    """

    phase: str
    action: str
    window_title: str
    app_name: str = ""
    element_role: str = ""
    element_name: str = ""
    screenshot_before_hash: str = ""
    screenshot_after_hash: str = ""
    predicted_outcome: str = ""
    error: str = ""
    timestamp_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.phase not in _GUI_PHASES:
            raise ValueError(
                f"phase must be one of {sorted(_GUI_PHASES)}, "
                f"got {self.phase!r}"
            )


TurnEvent = Union[
    ProgressEvent, TokenEvent, ResultEvent, ErrorEvent, InfoEvent,
    CotEvent, GuiEvent,
]
