"""Phase 6 Scope B B2 — verifier voting cross-field validator.

Testable rules for `_revalidate_votes`:

  * require > votes                  → warning visible (can never accept)
  * require == votes AND !parallel   → warning visible (any 'no' short-circuits)
  * require == votes AND parallel    → OK (needs unanimous, but attainable)
  * require < votes                  → OK (attainable threshold)
  * require == 0                     → OK (majority mode — always attainable)

The Apply button reflects the validator state — disabled while broken,
enabled when the config is attainable. BP-4 gate integrity: a
guaranteed-broken config must never make it to disk.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


class _PageStub:
    """Stand-in for Adw.PreferencesPage so `class BehaviorPage(Adw.…)`
    subclasses successfully at import time."""
    def __init__(self, *args, **kwargs) -> None:
        pass
    def set_name(self, *args, **kwargs) -> None:
        pass
    def add(self, *args, **kwargs) -> None:
        pass


class _RowStub:
    def __init__(self, *args, **kwargs) -> None:
        pass
    def add_suffix(self, *args, **kwargs) -> None:
        pass
    def add_row(self, *args, **kwargs) -> None:
        pass
    def set_child(self, *args, **kwargs) -> None:
        pass
    def add_css_class(self, *args, **kwargs) -> None:
        pass
    def set_visible(self, visible: bool) -> None:
        self._is_visible = visible


_adw_mock = MagicMock()
_adw_mock.PreferencesPage = _PageStub
_adw_mock.PreferencesGroup = _RowStub
_adw_mock.ActionRow = _RowStub
_adw_mock.EntryRow = _RowStub
_adw_mock.ExpanderRow = _RowStub

_gi_mock = MagicMock()
_gi_mock.repository = MagicMock()
_gi_mock.repository.Adw = _adw_mock
_gi_mock.repository.Gtk = MagicMock()

sys.modules.setdefault("gi", _gi_mock)
sys.modules.setdefault("gi.repository", _gi_mock.repository)


from gui.control.pages import behavior_page as bp


def _make_page():
    """Build a bare BehaviorPage instance with only the state the
    validator touches."""
    page = bp.BehaviorPage.__new__(bp.BehaviorPage)
    page._pending_votes = 1
    page._pending_require = 0
    page._pending_parallel = True
    page._pending_qb_max_retries = 3
    # A row that tracks its own visibility so we can assert on it.
    validator_row = _RowStub()
    validator_row._is_visible = False
    page._vote_validator_row = validator_row
    # Apply button — MagicMock so we can assert set_sensitive calls.
    page._apply_button = MagicMock()
    return page


# ── The rules ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("votes", "require", "parallel", "expected_broken"),
    [
        # Attainable configs.
        (1, 0, True, False),   # single vote, majority
        (3, 0, True, False),   # 3 votes, majority
        (3, 2, True, False),   # 3 votes, need 2 — attainable
        (3, 3, True, False),   # 3 votes parallel unanimous — attainable
        # Broken configs.
        (3, 5, True, True),    # need more than we cast
        (3, 3, False, True),   # sequential unanimous — 1st `no` kills it
        (1, 1, False, True),   # trivial rejection case
    ],
)
def test_validator_flags_broken_configs(
    votes: int, require: int, parallel: bool, expected_broken: bool,
) -> None:
    page = _make_page()
    page._pending_votes = votes
    page._pending_require = require
    page._pending_parallel = parallel

    bp.BehaviorPage._revalidate_votes(page)

    assert page._vote_validator_row._is_visible is expected_broken
    assert getattr(page, "_vote_config_broken", False) is expected_broken
    # Apply button sensitivity mirrors "not broken".
    page._apply_button.set_sensitive.assert_called_with(not expected_broken)


def test_default_config_passes() -> None:
    """The shipping defaults (votes=1, require=0, parallel=true) must
    always be treated as OK by the validator."""
    page = _make_page()
    bp.BehaviorPage._revalidate_votes(page)
    assert page._vote_validator_row._is_visible is False
    assert page._vote_config_broken is False


def test_validator_no_op_without_widgets_wired() -> None:
    """If someone calls _revalidate_votes before the widgets exist
    (e.g. mid-init) it must not crash — plan requires the validator
    to fire once at group build AND once after Actions build."""
    page = bp.BehaviorPage.__new__(bp.BehaviorPage)
    page._pending_votes = None
    page._pending_require = None
    page._pending_parallel = None
    page._pending_qb_max_retries = None
    page._vote_validator_row = None
    page._apply_button = None
    # Must not raise.
    bp.BehaviorPage._revalidate_votes(page)
