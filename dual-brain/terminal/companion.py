"""Right panel — companion panel with dual-mode CoT / interpretation (ADR-19).

Dual-mode:
  - **CoT mode** (during execution): pipeline step cards with
    pending/active/done/failed visual states.
  - **Interpretation mode** (after completion): outcome summary with
    tier badge, result output, and actionable suggestions.
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

_TIER_LABELS = {
    0: ("Tier 0", "Auto-approved", "#5e8787"),
    1: ("Tier 1", "Low risk", "#5e8787"),
    2: ("Tier 2", "Needs approval", "#e78952"),
    3: ("Tier 3", "High risk", "#f87171"),
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
        """Switch to interpretation mode after turn completion."""
        self._mode = "interpretation"
        header = self.query_one("#companion-header", Label)
        header.update("Interpretation")

        container = self.query_one("#cot-container", VerticalScroll)

        output = result.get("output", "")
        tier = result.get("tier", 0)
        outcome = result.get("outcome", "")
        success = result.get("success", False)

        tier_label, tier_desc, tier_color = _TIER_LABELS.get(
            tier, ("Tier ?", "Unknown", "#888888")
        )
        badge_text = Text()
        badge_text.append(f" {tier_label} ", style=f"bold on {tier_color}")
        badge_text.append(f" {tier_desc}", style="#888888")
        badge_card = Static(badge_text, classes="cot-card s-pending")
        container.mount(badge_card)

        glyph = "✓" if success else "✗"
        style = "#5e8787" if success else "#f87171"
        summary_card = Static(
            Text(f"{glyph} {outcome}", style=f"bold {style}"),
            classes="cot-card s-done" if success else "cot-card s-failed",
        )
        container.mount(summary_card)

        if output:
            output_card = Static(
                Text(output, style="#c0c0c0"),
                classes="cot-card s-done",
            )
            container.mount(output_card)

        suggestions = result.get("suggestions", [])
        if suggestions:
            parts = Text()
            parts.append("Suggestions:\n", style="bold #e78952")
            for s in suggestions:
                parts.append(f"  → {s}\n", style="#c0c0c0")
            suggestion_card = Static(parts, classes="cot-card s-pending")
            container.mount(suggestion_card)

        container.scroll_end(animate=False)

    def handle_gui(self, params: dict) -> None:
        """Render a GUI automation event card."""
        self._mode = "cot"
        container = self.query_one("#cot-container", VerticalScroll)

        phase = params.get("phase", "")
        action = params.get("action", "")
        app_name = params.get("app_name", "")
        window = params.get("window_title", "")
        element_role = params.get("element_role", "")
        element_name = params.get("element_name", "")
        predicted = params.get("predicted_outcome", "")
        error = params.get("error", "")
        before_hash = params.get("screenshot_before_hash", "")
        after_hash = params.get("screenshot_after_hash", "")

        _PHASE_GLYPHS = {"preview": "○", "executing": "◉", "complete": "✓"}
        _PHASE_STYLES = {"preview": "s-pending", "executing": "s-active", "complete": "s-done"}
        glyph = _PHASE_GLYPHS.get(phase, "?")
        css_class = _PHASE_STYLES.get(phase, "s-pending")

        heading = f"{glyph} GUI: {action}"
        if app_name:
            heading += f" [{app_name}]"

        parts = []
        if window:
            parts.append(f"Window: {window}")
        if element_role and element_name:
            parts.append(f"Element: {element_role} '{element_name}'")
        if predicted:
            parts.append(f"Predicted: {predicted}")
        if before_hash:
            parts.append(f"Before: {before_hash[:12]}...")
        if after_hash:
            parts.append(f"After: {after_hash[:12]}...")
        if error:
            parts.append(f"Error: {error}")

        body_text = "\n".join(parts)

        content = Text()
        content.append(heading + "\n", style="bold #e78952")
        if body_text:
            content.append(body_text, style="#c0c0c0")

        card = Static(content, classes=f"cot-card {css_class}")
        container.mount(card)
        container.scroll_end(animate=False)

    def handle_rpa(self, params: dict) -> None:
        """Render an RPA Bridge automation event card."""
        self._mode = "cot"
        container = self.query_one("#cot-container", VerticalScroll)

        phase = params.get("phase", "")
        workflow = params.get("workflow_name", "")
        kw_idx = params.get("keyword_index", 0)
        kw_total = params.get("keyword_total", 0)
        current_kw = params.get("current_keyword", "")
        kw_status = params.get("keyword_status", "")
        timeout_ms = params.get("timeout_remaining_ms", 0)
        screenshot_hash = params.get("screenshot_hash", "")
        qb_on_track = params.get("qb_on_track", True)
        qb_concern = params.get("qb_concern", "")
        error = params.get("error", "")

        _PHASE_GLYPHS = {
            "preview": "○", "executing": "◉", "step": "▸",
            "paused": "⏸", "complete": "✓",
        }
        _PHASE_STYLES = {
            "preview": "s-pending", "executing": "s-active",
            "step": "s-active", "paused": "s-pending", "complete": "s-done",
        }
        glyph = _PHASE_GLYPHS.get(phase, "?")
        css_class = _PHASE_STYLES.get(phase, "s-pending")

        heading = f"{glyph} RPA: {workflow}"
        if kw_total:
            heading += f" [{kw_idx}/{kw_total}]"

        parts = []
        if current_kw:
            status_str = f" → {kw_status}" if kw_status else ""
            parts.append(f"Keyword: {current_kw}{status_str}")
        if timeout_ms > 0:
            parts.append(f"Timeout: {timeout_ms / 1000:.0f}s remaining")
        if screenshot_hash:
            parts.append(f"Screenshot: {screenshot_hash[:12]}...")
        if qb_on_track:
            parts.append("QB: ✓ On track")
        elif qb_concern:
            parts.append(f"QB: ⚠ {qb_concern}")
        if error:
            parts.append(f"Error: {error}")

        body_text = "\n".join(parts)

        content = Text()
        content.append(heading + "\n", style="bold #e78952")
        if body_text:
            content.append(body_text, style="#c0c0c0")

        card = Static(content, classes=f"cot-card {css_class}")
        container.mount(card)
        container.scroll_end(animate=False)

    def clear(self) -> None:
        """Reset for a new turn."""
        self._cards.clear()
        self._mode = "idle"
        container = self.query_one("#cot-container", VerticalScroll)
        container.remove_children()
        header = self.query_one("#companion-header", Label)
        header.update("Chain of Thought")
