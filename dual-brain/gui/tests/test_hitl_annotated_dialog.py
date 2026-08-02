"""Tests for gui.hitl.annotated_dialog — V.5b AnnotatedScreenshotHitlPresenter.

Headless. Follows the repo's established mock-`gi`-in-sys.modules
pattern (mirrors gui/tests/test_hitl_dialog.py:21-22) so tests never
need a real X display or PyGObject install.

Coverage:
- Registration: `annotated_screenshot` name registered without
  colliding with the base `libadwaita` name.
- `_add_extra_content` no-op when preview_image_path is None.
- `_add_extra_content` no-op when preview_image_path fails presenter-
  layer re-sanitization (defense in depth on top of V.5a).
- `_add_extra_content` renders Gtk.Picture with correct path and
  appends the group to vbox when preview_image_path is valid.
- Overriding the hook does NOT break parent's other behavior
  (subclass still identifies as LibAdwaitaHitlPresenter).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("gi", MagicMock())
sys.modules.setdefault("gi.repository", MagicMock())

from controller.hitl import HitlDisplayData
from controller.presenters.registry import _REGISTRY
from controller.risk_classifier import Tier

from gui.hitl.annotated_dialog import (
    _MAX_PREVIEW_HEIGHT,
    AnnotatedScreenshotHitlPresenter,
)
from gui.hitl.dialog import LibAdwaitaHitlPresenter


# ── Fixtures ────────────────────────────────────────────────────────


def _display_data(preview_path=None):
    return HitlDisplayData(
        action="click",
        target="Slack — Send button",
        tier=Tier.HIGH,
        risk_level="tier_3",
        reversible=False,
        backend="opencode_oc",
        reason="grounded_click",
        blocked_pattern=None,
        cow_summary=None,
        preview_image_path=preview_path,
    )


def _bare_presenter():
    """Build an AnnotatedScreenshotHitlPresenter WITHOUT running its
    __init__ (which would try to import real gi). Injects a fake
    Gtk/Adw namespace via attribute assignment — matches the pattern
    the base test file uses for GtkPresenter."""
    inst = object.__new__(AnnotatedScreenshotHitlPresenter)
    inst._Gtk = MagicMock()
    inst._Adw = MagicMock()
    inst._GLib = MagicMock()
    inst._Gtk.ContentFit = SimpleNamespace(CONTAIN="CONTAIN")
    inst._Gtk.AccessibleProperty = SimpleNamespace(LABEL="LABEL")
    inst._Gtk.Orientation = SimpleNamespace(VERTICAL="VERTICAL",
                                             HORIZONTAL="HORIZONTAL")
    return inst


# ── Registration ─────────────────────────────────────────────────────


def test_annotated_screenshot_registered_by_name():
    """Import side-effect registers via decorator. Ensure the name is
    present in the registry after import."""
    assert "annotated_screenshot" in _REGISTRY


def test_registration_does_not_clobber_libadwaita():
    """Adding the new presenter must not remove or replace the
    base 'libadwaita' entry."""
    assert "libadwaita" in _REGISTRY
    assert _REGISTRY["libadwaita"] is not _REGISTRY["annotated_screenshot"]


def test_subclass_still_registers_as_libadwaita_subclass():
    assert issubclass(AnnotatedScreenshotHitlPresenter, LibAdwaitaHitlPresenter)


# ── _add_extra_content behavior ─────────────────────────────────────


def test_no_preview_path_is_noop():
    """When preview_image_path is None, hook does nothing —
    parent modal fires unchanged."""
    inst = _bare_presenter()
    vbox = MagicMock()
    data = _display_data(preview_path=None)
    result = inst._add_extra_content(vbox, data)
    assert result is None
    vbox.append.assert_not_called()
    # No Gtk widgets constructed.
    inst._Gtk.Picture.new_for_filename.assert_not_called()


def test_invalid_preview_path_is_noop():
    """When preview_image_path passes V.5a's dataclass sanitizer
    (rare — bypass scenarios) but fails presenter-layer re-check,
    hook silently skips."""
    inst = _bare_presenter()
    vbox = MagicMock()
    # Bypass V.5a by using object.__setattr__ on the frozen dataclass
    # — simulates a tampering-post-init scenario.
    data = _display_data(preview_path=None)
    object.__setattr__(data, "preview_image_path", "/etc/passwd")
    result = inst._add_extra_content(vbox, data)
    assert result is None
    vbox.append.assert_not_called()
    inst._Gtk.Picture.new_for_filename.assert_not_called()


def test_valid_preview_path_renders_picture():
    """Happy path: valid preview path → Gtk.Picture created, appended
    to vbox via a PreferencesGroup."""
    inst = _bare_presenter()
    vbox = MagicMock()
    valid_path = "/tmp/icebreaker-gui/preview-abc123.png"
    data = _display_data(preview_path=valid_path)
    inst._add_extra_content(vbox, data)

    # Picture built with the sanitized path.
    inst._Gtk.Picture.new_for_filename.assert_called_once_with(valid_path)

    # Rendered picture had CONTAIN fit + height cap.
    pic_mock = inst._Gtk.Picture.new_for_filename.return_value
    pic_mock.set_content_fit.assert_called_once_with("CONTAIN")
    pic_mock.set_size_request.assert_called_once_with(-1, _MAX_PREVIEW_HEIGHT)

    # PreferencesGroup was appended to the vbox (once).
    vbox.append.assert_called_once()


def test_valid_preview_path_wraps_in_scrolled_window():
    """Oversized PNGs need scrolling so the modal doesn't blow out
    on wide screens."""
    inst = _bare_presenter()
    vbox = MagicMock()
    data = _display_data(preview_path="/tmp/icebreaker-gui/preview-x.png")
    inst._add_extra_content(vbox, data)

    inst._Gtk.ScrolledWindow.assert_called_once()
    scroll_mock = inst._Gtk.ScrolledWindow.return_value
    scroll_mock.set_max_content_height.assert_called_once_with(_MAX_PREVIEW_HEIGHT)


def test_valid_preview_path_sets_accessibility_label():
    """Orca announces the picture — a11y label MUST be set."""
    inst = _bare_presenter()
    vbox = MagicMock()
    data = _display_data(preview_path="/tmp/icebreaker-gui/preview-x.png")
    inst._add_extra_content(vbox, data)

    pic_mock = inst._Gtk.Picture.new_for_filename.return_value
    pic_mock.update_property.assert_called_once()
    args, _ = pic_mock.update_property.call_args
    # Args are ([Gtk.AccessibleProperty.LABEL], [label_str]).
    label_string = args[1][0]
    assert "click" in label_string  # action
    assert "Slack" in label_string  # target


def test_render_exception_swallowed():
    """If Gtk widget construction blows up for any reason, hook logs
    and returns — never breaks the parent modal."""
    inst = _bare_presenter()
    vbox = MagicMock()
    inst._Gtk.Picture.new_for_filename.side_effect = RuntimeError("PIL boom")
    data = _display_data(preview_path="/tmp/icebreaker-gui/preview-x.png")
    # Must not raise.
    inst._add_extra_content(vbox, data)
    # The vbox was never modified because the exception was caught
    # before .append() ran.
    vbox.append.assert_not_called()


def test_missing_gtk_bindings_is_noop():
    """If self._Gtk is not set (e.g. instance built via object.__new__
    without any Gtk mock at all), hook returns without crashing."""
    inst = object.__new__(AnnotatedScreenshotHitlPresenter)
    # Intentionally do NOT set _Gtk / _Adw.
    vbox = MagicMock()
    data = _display_data(preview_path="/tmp/icebreaker-gui/preview-x.png")
    inst._add_extra_content(vbox, data)
    vbox.append.assert_not_called()


# ── Constants ────────────────────────────────────────────────────────


def test_max_preview_height_is_reasonable():
    """Sanity check on the height cap — must be small enough that a
    2400x1500 Retina screenshot fits on a laptop screen with the
    rest of the modal, big enough to actually read element captions."""
    assert 200 <= _MAX_PREVIEW_HEIGHT <= 800


# ── Interaction with V.5a sanitizer ──────────────────────────────────


def test_dataclass_sanitized_valid_path_reaches_presenter():
    """End-to-end from V.5a to V.5b: a valid path constructed via
    HitlDisplayData survives dataclass __post_init__ AND makes it into
    Gtk.Picture.new_for_filename with the exact same string."""
    valid = "/tmp/icebreaker-gui/preview-e2e.png"
    data = _display_data(preview_path=valid)
    assert data.preview_image_path == valid  # V.5a preserved it

    inst = _bare_presenter()
    vbox = MagicMock()
    inst._add_extra_content(vbox, data)
    inst._Gtk.Picture.new_for_filename.assert_called_once_with(valid)


def test_dataclass_dropped_bad_path_never_reaches_presenter():
    """If V.5a dropped the path to None, presenter sees None and no-ops."""
    data = _display_data(preview_path="/etc/passwd")  # V.5a drops to None
    assert data.preview_image_path is None

    inst = _bare_presenter()
    vbox = MagicMock()
    inst._add_extra_content(vbox, data)
    vbox.append.assert_not_called()
    inst._Gtk.Picture.new_for_filename.assert_not_called()
