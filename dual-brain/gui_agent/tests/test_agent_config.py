"""v6.10 P6 / F-73 — GuiConfig plumbing anti-hide guard.

Prior to Track P6 the Controller constructed ``GuiAgent(scratch_dir=...)``
and passed nothing else. Three TOML knobs — ``gui.prefer_app_api``,
``gui.screenshot_retention``, ``gui.a11y_timeout_ms`` — were reachable
only by name in ``config.py``; the Agent constructor never saw them.
Setting ``gui.screenshot_retention = 10`` in ``/etc/icebreaker/config.toml``
did nothing.

This test pins the fix: passing a ``GuiConfig`` with non-default fields
must actually reach the Agent's internal state. If a future refactor
reverts to the ``scratch_dir=...``-only form, the assertions fail
loudly with the F-73 reference.

Sister anti-hide guards:
- test_no_silent_swallow.py (F-53)
- test_tool_catalogue_yaml.py (F-68 / F-71)
- test_verifier_prompt_scope.py (F-65)
- test_cancelled_reason.py (F-67)
- test_friendly_tool_error.py (F-64)
"""
from __future__ import annotations

from unittest.mock import patch

from controller.config import GuiConfig
from gui_agent.agent import GuiAgent


def test_gui_agent_reads_screenshot_retention_from_config(tmp_path):
    """``gui.screenshot_retention`` in TOML must reach the
    ScreenshotManager's eviction loop. Prior to P6 the constant
    ``_MAX_RETAINED`` (50) was the only cap; user config was ignored."""
    cfg = GuiConfig(screenshot_dir=str(tmp_path), screenshot_retention=7)
    with patch("gui_agent.agent.AtSpiClient"):
        agent = GuiAgent(config=cfg)
    assert agent._screenshot_retention == 7, (
        "F-73 regression: GuiConfig.screenshot_retention did not reach "
        "GuiAgent internal state. Prior fix threaded the whole config; "
        "someone must have reverted to scratch_dir=... only. Restore "
        "the config=... path in GuiAgent.__init__."
    )
    assert agent._screenshots._retention == 7, (
        "F-73 regression: retention set on GuiAgent but NOT on the "
        "ScreenshotManager it constructs. The eviction loop still uses "
        "the hard-coded _MAX_RETAINED. Restore the "
        "``ScreenshotManager(..., retention=self._screenshot_retention)`` "
        "call in __init__."
    )


def test_gui_agent_reads_a11y_timeout_from_config(tmp_path):
    """``gui.a11y_timeout_ms`` in TOML must land on the Agent. Value is
    reserved for AT-SPI call-level timeout wiring (Phase 7); presence
    on the object today is the load-bearing part — it proves the
    config path exists so downstream wiring is a one-line change."""
    cfg = GuiConfig(screenshot_dir=str(tmp_path), a11y_timeout_ms=1234)
    with patch("gui_agent.agent.AtSpiClient"):
        agent = GuiAgent(config=cfg)
    assert agent._a11y_timeout_ms == 1234, (
        "F-73 regression: GuiConfig.a11y_timeout_ms did not reach "
        "GuiAgent. This is the config seam Phase 7 will wire into "
        "AT-SPI call timeouts; if the plumbing itself regresses we "
        "have to redo it first."
    )


def test_gui_agent_reads_prefer_app_api_from_config(tmp_path):
    """``gui.prefer_app_api`` in TOML must override the default. Prior
    to P6, the Controller call sites always used the ``prefer_app_api``
    keyword default (True), no matter what config said."""
    cfg = GuiConfig(screenshot_dir=str(tmp_path), prefer_app_api=False)
    with patch("gui_agent.agent.AtSpiClient"):
        agent = GuiAgent(config=cfg)
    assert agent._prefer_app_api is False, (
        "F-73 regression: GuiConfig.prefer_app_api = False did not "
        "flip the Agent's internal flag. Users cannot opt into the "
        "raw AT-SPI path even after setting the knob in TOML."
    )


def test_gui_agent_legacy_scratch_dir_form_still_works(tmp_path):
    """Backward compat: callers that don't have a GuiConfig handy
    (e.g. unit tests, ad-hoc scripts) can still pass just
    ``scratch_dir=...`` and get sensible defaults."""
    with patch("gui_agent.agent.AtSpiClient"):
        agent = GuiAgent(scratch_dir=str(tmp_path))
    assert agent._screenshot_retention == 50, (
        "Legacy default retention regressed. Backward-compat callers "
        "should get the historical 50-file cap."
    )
    assert agent._a11y_timeout_ms == 5000, (
        "Legacy default a11y timeout regressed."
    )
    assert agent._prefer_app_api is True, (
        "Legacy default prefer_app_api regressed."
    )
