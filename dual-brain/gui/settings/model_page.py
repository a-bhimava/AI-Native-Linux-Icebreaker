"""Model page — GGUF model manager with checksum verification.

Scans model_search_dirs for .gguf files, shows size and checksum status.
Verification runs in a Gio.Task thread to avoid blocking the UI.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

_DEFAULT_SEARCH_DIRS = [
    Path.home() / "models",
    Path("/var/lib/icebreaker/models"),
]

_CHUNK_SIZE = 65536


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} TB"


def _load_checksums(search_dirs: list[Path]) -> dict[str, str]:
    """Load filename→sha256 from checksums.sha256 in any search dir."""
    result: dict[str, str] = {}
    for d in search_dirs:
        csum_file = d / "checksums.sha256"
        if csum_file.exists():
            for line in csum_file.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    sha, name = parts
                    result[Path(name).name] = sha
    return result


class _ModelRow(Adw.ActionRow):
    """Row for a single GGUF model file."""

    def __init__(self, path: Path, expected_sha: Optional[str]) -> None:
        super().__init__()
        self.path = path
        self.expected_sha = expected_sha
        self.set_title(path.name)
        size = _human_size(path.stat().st_size)
        self.set_subtitle(size)

        self._status = Gtk.Label(label="?")
        self._status.set_valign(Gtk.Align.CENTER)
        self._status.add_css_class("ib-muted-text")
        self.add_suffix(self._status)

    def set_status(self, status: str) -> None:
        self._status.set_label(status)
        self._status.remove_css_class("ib-muted-text")
        if status == "OK":
            self._status.add_css_class("ib-secondary")
        elif status == "FAIL":
            self._status.add_css_class("ib-destructive")


class ModelPage(Adw.PreferencesPage):
    """Model list with verify-all button."""

    def __init__(self, raw: dict) -> None:
        super().__init__()
        self.set_title("Models")
        self.set_icon_name("drive-harddisk-symbolic")

        self._dirty_cb: Optional[Callable[[], None]] = None

        paths_section = raw.get("paths", {})
        self._search_dirs = [
            Path(d).expanduser()
            for d in paths_section.get("model_search_dirs", [str(d) for d in _DEFAULT_SEARCH_DIRS])
        ]
        self._checksums = _load_checksums(self._search_dirs)

        models_group = Adw.PreferencesGroup(title="Installed Models")
        models_group.set_description("GGUF model files found in search directories")
        self.add(models_group)

        self._model_rows: list[_ModelRow] = []
        gguf_files: list[Path] = []
        for d in self._search_dirs:
            if d.exists():
                gguf_files.extend(sorted(d.glob("*.gguf")))

        if not gguf_files:
            empty = Adw.ActionRow(title="No models found")
            empty.set_subtitle(", ".join(str(d) for d in self._search_dirs))
            models_group.add(empty)
        else:
            for gf in gguf_files:
                expected = self._checksums.get(gf.name)
                row = _ModelRow(gf, expected)
                self._model_rows.append(row)
                models_group.add(row)

        actions_group = Adw.PreferencesGroup()
        self.add(actions_group)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)

        self._verify_btn = Gtk.Button(label="Verify All Checksums")
        self._verify_btn.add_css_class("ib-primary-button")
        self._verify_btn.connect("clicked", self._on_verify)
        btn_box.append(self._verify_btn)

        self._progress = Gtk.ProgressBar()
        self._progress.set_visible(False)
        self._progress.set_hexpand(True)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)
        vbox.append(btn_box)
        vbox.append(self._progress)
        actions_group.add(vbox)

    def connect_dirty(self, cb: Callable[[], None]) -> None:
        self._dirty_cb = cb

    def _on_verify(self, _btn: Gtk.Button) -> None:
        if not self._model_rows:
            return
        self._verify_btn.set_sensitive(False)
        self._progress.set_visible(True)
        self._progress.set_fraction(0.0)

        task = Gio.Task.new(None, None, self._on_verify_done)
        task.run_in_thread(self._verify_thread)

    def _verify_thread(self, task: Gio.Task, _source: object,
                       _data: object, _cancel: object) -> None:
        total = len(self._model_rows)
        results: list[tuple[int, str]] = []
        for i, row in enumerate(self._model_rows):
            if row.expected_sha is None:
                results.append((i, "?"))
            else:
                sha = hashlib.sha256()
                with open(row.path, "rb") as fh:
                    while True:
                        chunk = fh.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        sha.update(chunk)
                actual = sha.hexdigest()
                status = "OK" if actual == row.expected_sha else "FAIL"
                results.append((i, status))
            frac = (i + 1) / total
            GLib.idle_add(self._progress.set_fraction, frac)

        task.results = results  # type: ignore[attr-defined]
        task.return_boolean(True)

    def _on_verify_done(self, _source: object, task: Gio.Task) -> None:
        results = getattr(task, "results", [])
        for idx, status in results:
            self._model_rows[idx].set_status(status)
        self._verify_btn.set_sensitive(True)
        self._progress.set_visible(False)

    def collect_into(self, data: dict) -> None:
        pass
