"""AnnotatedScreenshotHitlPresenter — V.5b of Fix V (v6.15).

Subclass of ``LibAdwaitaHitlPresenter`` that renders an inline annotated
screenshot preview (from ``gui_agent.annotate.render_annotated``) above
the standard Operation Details section when the ``HitlDisplayData``
carries a ``preview_image_path``.

Why a subclass, not a rewrite:
- Parent already implements the visual `Gtk.ProgressBar` lockout,
  disabled-Approve-during-lockout, full keymap wiring, tier-colored
  Adw.HeaderBar badge, and COW diff pane. Duplicating that would be
  ~250 lines of copy-paste + drift risk.
- Parent exposes a tiny `_add_extra_content(vbox, data)` no-op hook
  (dialog.py after the details_group append). This subclass overrides
  only that hook — one method, one focused test surface.

UX design decisions (from GTK4 research probe 2026-08-02):
- **Gtk.Picture**, NOT Gtk.Image. Gtk.Image is icon-sized; Gtk.Picture
  scales natural-size content with `content-fit=CONTAIN` and HiDPI
  transparency via `scale-factor`.
- Height capped at 400px via `set_size_request(-1, 400)`; wider than
  the modal → wrapped in `Gtk.ScrolledWindow` so it doesn't blow out
  the dialog geometry on wide screens.
- `Gtk.Frame` around the picture matches the visual weight of the
  other cards in the parent (COW diff, RPA steps).
- Accessibility: `AccessibleProperty.LABEL` set to a summary string
  so Orca announces "Screenshot preview" instead of nothing.

Path-injection defense in depth (BP-9):
- V.5a already sanitized `preview_image_path` at HitlDisplayData
  construction time (must be under /tmp/icebreaker-gui/ + .png).
- V.5b re-validates HERE at the presenter's load call site as a
  second layer — a subclass that somehow bypassed V.5a's dataclass
  post_init still can't display /etc/passwd.
- If validation fails at presenter time, we silently skip the Picture
  — the base modal still fires with its normal text-only content.

Registration: `@register_presenter("annotated_screenshot")`. Selected
via `[hitl] presenter = "annotated_screenshot"` in `controller.toml`.
The existing `"libadwaita"` name still works — this is additive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from controller.hitl import HitlDisplayData, _sanitize_preview_path
from controller.presenters.registry import register_presenter

from .dialog import LibAdwaitaHitlPresenter

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)


_MAX_PREVIEW_HEIGHT = 400   # px; picture scales down within the modal


@register_presenter("annotated_screenshot")
class AnnotatedScreenshotHitlPresenter(LibAdwaitaHitlPresenter):
    """LibAdwaita HITL presenter that renders an annotated-screenshot
    preview above the standard Operation Details section when the
    HitlDisplayData carries a preview_image_path.

    Inherits ALL other behavior from LibAdwaitaHitlPresenter:
    visual lockout, tier badge, COW diff pane, RPA step list,
    Approve/Deny/Modify/Explain buttons, keymap, screen-reader hooks.
    """

    def _add_extra_content(self, vbox: object, data: HitlDisplayData) -> None:
        """Inject a Gtk.Picture with the annotated PNG when the data has
        a valid preview_image_path. Silently no-op otherwise so the modal
        still fires normally for prompts without a preview.
        """
        raw_path = getattr(data, "preview_image_path", None)
        if not raw_path:
            return

        # Defense in depth: re-sanitize at the load site. V.5a's dataclass
        # already screens on construction — this catches any post-init
        # tampering or bypassed subclass usage.
        safe_path = _sanitize_preview_path(raw_path)
        if safe_path is None:
            _log.warning(
                "annotated_screenshot: rejected preview_image_path %r "
                "(failed re-validation at presenter layer)",
                raw_path,
            )
            return

        try:
            Gtk = self._Gtk
            Adw = self._Adw
        except AttributeError:
            _log.warning("annotated_screenshot: GTK bindings unavailable")
            return

        try:
            # Group wrapper matches the visual card style of the other
            # sections in the parent dialog.
            preview_group = Adw.PreferencesGroup(title="Preview")

            picture = Gtk.Picture.new_for_filename(safe_path)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            picture.set_size_request(-1, _MAX_PREVIEW_HEIGHT)
            picture.set_can_shrink(True)

            # Screen reader — Orca announces this when focus reaches
            # the picture. Base data.action / data.target already sanitize.
            try:
                picture.update_property(
                    [Gtk.AccessibleProperty.LABEL],
                    [f"Preview screenshot for {data.action or 'action'} "
                     f"on {data.target or 'target'}"],
                )
            except Exception:  # noqa: BLE001 — a11y properties are best-effort
                pass

            # Wrap in a scroll region so an oversized PNG doesn't blow
            # out the modal geometry on wide screens.
            scroll = Gtk.ScrolledWindow()
            scroll.set_min_content_height(_MAX_PREVIEW_HEIGHT)
            scroll.set_max_content_height(_MAX_PREVIEW_HEIGHT)
            scroll.set_hexpand(True)
            scroll.set_child(picture)

            frame = Gtk.Frame()
            frame.set_child(scroll)
            frame.add_css_class("ib-card")

            # Adw.PreferencesGroup takes children via .add() — but not
            # arbitrary widgets. Wrap in a Box that acts as one "row".
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            row_box.append(frame)
            preview_group.add(row_box)

            vbox.append(preview_group)
        except Exception as exc:  # noqa: BLE001 — never break the modal
            _log.warning(
                "annotated_screenshot: failed to render preview %r: %s: %s",
                safe_path, type(exc).__name__, exc,
            )
