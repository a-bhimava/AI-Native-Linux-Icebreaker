"""Right panel — companion panel with dual-mode CoT / interpretation (ADR-19).

PR #23 implements the CoT card rendering side. The interpretation view
(bar charts, summaries, suggestions) is wired in PR #24 when the input
router connects NL input to the daemon and turn results flow back.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Label, Static

_STATE_GLYPHS = {
    "pending": "○",
    "active":  "◉",
    "done":    "✓",
    "failed":  "✗",
}


class CotCard(Static):
    """A single chain-of-thought step card."""

    DEFAULT_CSS = ""

    def __init__(
        self,
        step_index: int,
        step_name: str,
        step_state: str,
        heading: str,
        body: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._step_index = step_index
        self._step_name = step_name
        self._heading_text = heading
        self._body_text = body
        self._state = step_state

    def compose(self) -> ComposeResult:
        glyph = _STATE_GLYPHS.get(self._state, "?")
        yield Label(
            f"{glyph} {self._heading_text}",
            classes="card-heading",
        )
        if self._body_text:
            yield Label(self._body_text, classes="card-body")

    def on_mount(self) -> None:
        self.add_class("cot-card")
        self.add_class(f"s-{self._state}")

    def update_state(self, state: str, body: str = "") -> None:
        """Update the card's visual state and optional body text."""
        self.remove_class(f"s-{self._state}")
        self._state = state
        self.add_class(f"s-{self._state}")

        glyph = _STATE_GLYPHS.get(state, "?")
        heading_label = self.query_one(".card-heading", Label)
        heading_label.update(f"{glyph} {self._heading_text}")

        if body:
            self._body_text = body
            body_labels = self.query(".card-body")
            if body_labels:
                body_labels.first().update(body)
            else:
                self.mount(Label(body, classes="card-body"))


class CompanionPanel(Static):
    """Right panel: CoT step cards during execution, interpretation after."""

    DEFAULT_CSS = ""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cards: dict[str, CotCard] = {}
        self._mode = "idle"  # idle | cot | interpretation

    def compose(self) -> ComposeResult:
        yield Label("Chain of Thought", id="companion-header")
        yield VerticalScroll(id="cot-container")

    def handle_cot(
        self,
        step_index: int,
        step_name: str,
        step_state: str,
        heading: str,
        body: str = "",
        data: dict | None = None,
    ) -> None:
        """Process an incoming CotEvent — create or update a step card."""
        self._mode = "cot"
        header = self.query_one("#companion-header", Label)
        header.update("Chain of Thought")

        container = self.query_one("#cot-container", VerticalScroll)

        if step_name in self._cards:
            self._cards[step_name].update_state(step_state, body)
        else:
            card = CotCard(
                step_index=step_index,
                step_name=step_name,
                step_state=step_state,
                heading=heading,
                body=body,
            )
            self._cards[step_name] = card
            container.mount(card)

        container.scroll_end(animate=False)

    def handle_result(self, result: dict) -> None:
        """Switch to interpretation mode after turn completion.

        Full interpretation rendering (bar charts, summaries, suggestions)
        is PR #24 scope. This PR shows a simple result summary.
        """
        self._mode = "interpretation"
        header = self.query_one("#companion-header", Label)
        header.update("Interpretation")

        container = self.query_one("#cot-container", VerticalScroll)

        output = result.get("output", "")
        tier = result.get("tier", 0)
        outcome = result.get("outcome", "")
        success = result.get("success", False)

        style = "#5e8787" if success else "#f87171"
        summary_card = Static(
            Text(f"{'✓' if success else '✗'} {outcome}", style=f"bold {style}"),
            classes="cot-card s-done" if success else "cot-card s-failed",
        )
        container.mount(summary_card)

        if output:
            output_card = Static(
                Text(output, style="#c0c0c0"),
                classes="cot-card s-done",
            )
            container.mount(output_card)

        container.scroll_end(animate=False)

    def clear(self) -> None:
        """Reset for a new turn."""
        self._cards.clear()
        self._mode = "idle"
        container = self.query_one("#cot-container", VerticalScroll)
        container.remove_children()
        header = self.query_one("#companion-header", Label)
        header.update("Chain of Thought")
