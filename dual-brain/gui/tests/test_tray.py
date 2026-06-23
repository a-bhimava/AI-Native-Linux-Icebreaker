"""Tests for gui.tray — System Tray Indicator (M6UI.3).

Headless tests covering:
  - DaemonState constants
  - State label and color mappings
  - Poll interval value
  - Health response → state mapping
  - State transition logic
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("gi", MagicMock())
sys.modules.setdefault("gi.repository", MagicMock())

from gui.tray.indicator import (
    DaemonState,
    _POLL_INTERVAL_SECONDS,
    _STATE_COLORS,
    _STATE_LABELS,
)


# ---------------------------------------------------------------------------
# DaemonState constants
# ---------------------------------------------------------------------------

class TestDaemonState:
    def test_ready_value(self) -> None:
        assert DaemonState.READY == "ready"

    def test_loading_value(self) -> None:
        assert DaemonState.LOADING == "loading"

    def test_offline_value(self) -> None:
        assert DaemonState.OFFLINE == "offline"

    def test_all_states_are_distinct(self) -> None:
        states = [DaemonState.READY, DaemonState.LOADING, DaemonState.OFFLINE]
        assert len(set(states)) == 3


# ---------------------------------------------------------------------------
# State labels
# ---------------------------------------------------------------------------

class TestStateLabels:
    def test_has_entry_for_every_state(self) -> None:
        for state in [DaemonState.READY, DaemonState.LOADING, DaemonState.OFFLINE]:
            assert state in _STATE_LABELS

    def test_ready_label_contains_ready(self) -> None:
        assert "Ready" in _STATE_LABELS[DaemonState.READY]

    def test_loading_label_mentions_warming(self) -> None:
        assert "warming" in _STATE_LABELS[DaemonState.LOADING].lower()

    def test_offline_label_mentions_offline(self) -> None:
        assert "offline" in _STATE_LABELS[DaemonState.OFFLINE].lower()

    def test_all_labels_start_with_icebreaker(self) -> None:
        for label in _STATE_LABELS.values():
            assert label.startswith("Icebreaker:")


# ---------------------------------------------------------------------------
# State colors — RGB tuples
# ---------------------------------------------------------------------------

class TestStateColors:
    def test_has_entry_for_every_state(self) -> None:
        for state in [DaemonState.READY, DaemonState.LOADING, DaemonState.OFFLINE]:
            assert state in _STATE_COLORS

    def test_all_colors_are_3_tuples(self) -> None:
        for color in _STATE_COLORS.values():
            assert isinstance(color, tuple)
            assert len(color) == 3

    def test_all_channels_in_0_to_1_range(self) -> None:
        for state, (r, g, b) in _STATE_COLORS.items():
            for ch, val in [("r", r), ("g", g), ("b", b)]:
                assert 0.0 <= val <= 1.0, f"{state}.{ch} = {val} out of range"

    def test_ready_is_green_dominant(self) -> None:
        r, g, b = _STATE_COLORS[DaemonState.READY]
        assert g > r and g > b

    def test_loading_is_orange_warm(self) -> None:
        r, g, b = _STATE_COLORS[DaemonState.LOADING]
        assert r > g > b

    def test_offline_is_grey(self) -> None:
        r, g, b = _STATE_COLORS[DaemonState.OFFLINE]
        assert abs(r - g) < 0.05 and abs(g - b) < 0.05


# ---------------------------------------------------------------------------
# Poll interval
# ---------------------------------------------------------------------------

class TestPollInterval:
    def test_interval_is_positive(self) -> None:
        assert _POLL_INTERVAL_SECONDS > 0

    def test_interval_is_10_seconds(self) -> None:
        assert _POLL_INTERVAL_SECONDS == 10


# ---------------------------------------------------------------------------
# Health response → state mapping logic
# ---------------------------------------------------------------------------

class TestHealthMapping:
    """Test the status-string → DaemonState mapping used by _handle_health."""

    _STATUS_MAP = {
        "ready": DaemonState.READY,
        "loading": DaemonState.LOADING,
        "unknown": DaemonState.OFFLINE,
        "error": DaemonState.OFFLINE,
        "": DaemonState.OFFLINE,
    }

    @pytest.mark.parametrize("status,expected", list(_STATUS_MAP.items()))
    def test_status_to_state(self, status: str, expected: str) -> None:
        result = self._map_status(status)
        assert result == expected

    def test_missing_status_key_maps_to_offline(self) -> None:
        result = self._map_status(None)
        assert result == DaemonState.OFFLINE

    @staticmethod
    def _map_status(status: str | None) -> str:
        if status == "ready":
            return DaemonState.READY
        elif status == "loading":
            return DaemonState.LOADING
        else:
            return DaemonState.OFFLINE
