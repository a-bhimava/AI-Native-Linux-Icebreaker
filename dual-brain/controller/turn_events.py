"""TurnEvent protocol — streaming events for the Controller pipeline.

The ``run_turn_streaming()`` generator yields ``TurnEvent`` objects so the
REPL can show pipeline progress and stream QB summarization tokens. The
existing synchronous ``run_turn()`` is unchanged (BP-2 backward compat).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .main import TurnResult


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


TurnEvent = Union[ProgressEvent, TokenEvent, ResultEvent, ErrorEvent]
