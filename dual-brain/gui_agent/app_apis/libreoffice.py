"""LibreOffice app API — UNO bridge via D-Bus.

Provides structured operations for Calc, Writer, and Impress without
relying on AT-SPI element tree traversal. Uses the ``com.sun.star.*``
D-Bus interface when available; degrades gracefully otherwise.

All returned data is structured dicts, never raw UNO objects.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .base import AppApi
from .registry import register_app_api


_LO_TITLE_PATTERNS = (
    re.compile(r".*LibreOffice\s+(Calc|Writer|Impress|Draw|Base|Math)", re.I),
    re.compile(r".*\.(ods|xlsx|csv|odt|docx|odp|pptx)\b.*LibreOffice", re.I),
    re.compile(r".*\.(ods|xlsx|csv|odt|docx|odp|pptx)\s*[-–—]", re.I),
)

_CALC_ACTIONS = frozenset({
    "open_document", "save_document", "close_document",
    "get_cell", "set_cell", "insert_row", "delete_row",
    "insert_column", "delete_column", "format_cell",
    "get_sheet_names", "set_active_sheet",
})

_WRITER_ACTIONS = frozenset({
    "open_document", "save_document", "close_document",
    "get_text", "insert_text", "format_text",
    "get_paragraph_count",
})

_IMPRESS_ACTIONS = frozenset({
    "open_document", "save_document", "close_document",
    "insert_slide", "delete_slide", "navigate_slide",
    "get_slide_count", "get_current_slide",
})

_COMMON_ACTIONS = frozenset({
    "open_document", "save_document", "close_document",
})


def _detect_component(window_title: str) -> Optional[str]:
    """Detect which LO component from the window title."""
    low = window_title.lower()
    if "calc" in low or any(ext in low for ext in (".ods", ".xlsx", ".csv")):
        return "calc"
    if "writer" in low or any(ext in low for ext in (".odt", ".docx")):
        return "writer"
    if "impress" in low or any(ext in low for ext in (".odp", ".pptx")):
        return "impress"
    for pat in _LO_TITLE_PATTERNS:
        m = pat.match(window_title)
        if m:
            component = m.group(1).lower() if m.lastindex else None
            if component in ("calc", "writer", "impress"):
                return component
    return None


@register_app_api("libreoffice")
class LibreOfficeApi(AppApi):
    """LibreOffice automation via UNO bridge (D-Bus).

    Lazy-imports UNO to avoid hard dependency. When UNO is unavailable,
    ``execute()`` returns structured error dicts instead of crashing.
    """

    def __init__(self) -> None:
        self._uno = None
        self._available = False
        self._init_error: str = ""

        try:
            import uno  # noqa: F401
            from com.sun.star.beans import PropertyValue  # noqa: F401
            self._uno = uno
            self._available = True
        except ImportError as exc:
            self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._available

    def supports(self, window_title: str) -> bool:
        return any(pat.match(window_title) for pat in _LO_TITLE_PATTERNS)

    def capabilities(self) -> list[str]:
        return sorted(_CALC_ACTIONS | _WRITER_ACTIONS | _IMPRESS_ACTIONS)

    def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._available:
            return {
                "success": False,
                "error": f"UNO bridge not available: {self._init_error}. "
                         "Install libreoffice-script-provider-python.",
                "fallback": "atspi",
            }

        component = params.get("component", "")
        actions_for_component = self._actions_for_component(component)
        if action not in actions_for_component:
            return {
                "success": False,
                "error": f"Action {action!r} not supported for component "
                         f"{component!r}. Available: {sorted(actions_for_component)}",
            }

        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return {
                "success": False,
                "error": f"Handler for {action!r} not implemented",
            }

        try:
            return handler(params)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _actions_for_component(self, component: str) -> frozenset[str]:
        if component == "calc":
            return _CALC_ACTIONS
        elif component == "writer":
            return _WRITER_ACTIONS
        elif component == "impress":
            return _IMPRESS_ACTIONS
        return _COMMON_ACTIONS

    def _get_desktop(self) -> Any:
        """Connect to the running LibreOffice instance via UNO."""
        uno = self._uno
        local_ctx = uno.getComponentContext()
        resolver = local_ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local_ctx,
        )
        ctx = resolver.resolve(
            "uno:socket,host=localhost,port=2002;"
            "urp;StarOffice.ComponentContext"
        )
        smgr = ctx.ServiceManager
        return smgr.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx,
        )

    def _do_open_document(self, params: dict) -> dict:
        path = params.get("path", "")
        if not path:
            return {"success": False, "error": "path is required"}
        desktop = self._get_desktop()
        url = f"file://{path}" if not path.startswith("file://") else path
        doc = desktop.loadComponentFromURL(url, "_blank", 0, ())
        return {"success": doc is not None, "url": url}

    def _do_save_document(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        doc.store()
        return {"success": True}

    def _do_close_document(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        doc.close(True)
        return {"success": True}

    def _do_get_cell(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        sheet_name = params.get("sheet", "")
        sheet = (
            doc.getSheets().getByName(sheet_name)
            if sheet_name else doc.getSheets().getByIndex(0)
        )
        col = params.get("column", 0)
        row = params.get("row", 0)
        cell = sheet.getCellByPosition(col, row)
        value = cell.getString() or cell.getValue()
        return {"success": True, "value": value, "column": col, "row": row}

    def _do_set_cell(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        sheet_name = params.get("sheet", "")
        sheet = (
            doc.getSheets().getByName(sheet_name)
            if sheet_name else doc.getSheets().getByIndex(0)
        )
        col = params.get("column", 0)
        row = params.get("row", 0)
        value = params.get("value", "")
        cell = sheet.getCellByPosition(col, row)
        if isinstance(value, (int, float)):
            cell.setValue(value)
        else:
            cell.setString(str(value))
        return {"success": True, "column": col, "row": row}

    def _do_insert_row(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        sheet = doc.getSheets().getByIndex(0)
        row = params.get("row", 0)
        count = params.get("count", 1)
        sheet.getRows().insertByIndex(row, count)
        return {"success": True, "row": row, "count": count}

    def _do_delete_row(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        sheet = doc.getSheets().getByIndex(0)
        row = params.get("row", 0)
        count = params.get("count", 1)
        sheet.getRows().removeByIndex(row, count)
        return {"success": True, "row": row, "count": count}

    def _do_insert_column(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        sheet = doc.getSheets().getByIndex(0)
        col = params.get("column", 0)
        count = params.get("count", 1)
        sheet.getColumns().insertByIndex(col, count)
        return {"success": True, "column": col, "count": count}

    def _do_delete_column(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        sheet = doc.getSheets().getByIndex(0)
        col = params.get("column", 0)
        count = params.get("count", 1)
        sheet.getColumns().removeByIndex(col, count)
        return {"success": True, "column": col, "count": count}

    def _do_format_cell(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        sheet = doc.getSheets().getByIndex(0)
        col = params.get("column", 0)
        row = params.get("row", 0)
        cell = sheet.getCellByPosition(col, row)
        if "bold" in params:
            cell.setPropertyValue("CharWeight", 150 if params["bold"] else 100)
        if "italic" in params:
            cell.setPropertyValue("CharPosture", 2 if params["italic"] else 0)
        if "font_size" in params:
            cell.setPropertyValue("CharHeight", params["font_size"])
        return {"success": True, "column": col, "row": row}

    def _do_get_sheet_names(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        names = list(doc.getSheets().getElementNames())
        return {"success": True, "sheets": names}

    def _do_set_active_sheet(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        name = params.get("sheet", "")
        if not name:
            return {"success": False, "error": "sheet name is required"}
        sheet = doc.getSheets().getByName(name)
        doc.getCurrentController().setActiveSheet(sheet)
        return {"success": True, "sheet": name}

    def _do_get_text(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        text = doc.getText().getString()
        return {"success": True, "text": text}

    def _do_insert_text(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        content = params.get("text", "")
        cursor = doc.getText().createTextCursor()
        cursor.gotoEnd(False)
        doc.getText().insertString(cursor, content, False)
        return {"success": True}

    def _do_format_text(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        if "bold" in params:
            cursor.setPropertyValue("CharWeight", 150 if params["bold"] else 100)
        if "italic" in params:
            cursor.setPropertyValue("CharPosture", 2 if params["italic"] else 0)
        if "font_size" in params:
            cursor.setPropertyValue("CharHeight", params["font_size"])
        return {"success": True}

    def _do_get_paragraph_count(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        enum = doc.getText().createEnumeration()
        count = sum(1 for _ in iter(enum.hasMoreElements, False))
        return {"success": True, "count": count}

    def _do_insert_slide(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        slides = doc.getDrawPages()
        index = params.get("index", slides.getCount())
        slides.insertNewByIndex(index)
        return {"success": True, "index": index, "total": slides.getCount()}

    def _do_delete_slide(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        slides = doc.getDrawPages()
        index = params.get("index", slides.getCount() - 1)
        if slides.getCount() <= 1:
            return {"success": False, "error": "Cannot delete last slide"}
        page = slides.getByIndex(index)
        slides.remove(page)
        return {"success": True, "index": index, "total": slides.getCount()}

    def _do_navigate_slide(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        index = params.get("index", 0)
        slides = doc.getDrawPages()
        if index >= slides.getCount():
            return {"success": False, "error": f"Slide {index} out of range"}
        page = slides.getByIndex(index)
        doc.getCurrentController().setCurrentPage(page)
        return {"success": True, "index": index}

    def _do_get_slide_count(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        return {"success": True, "count": doc.getDrawPages().getCount()}

    def _do_get_current_slide(self, params: dict) -> dict:
        desktop = self._get_desktop()
        doc = desktop.getCurrentComponent()
        if doc is None:
            return {"success": False, "error": "No document open"}
        page = doc.getCurrentController().getCurrentPage()
        slides = doc.getDrawPages()
        for i in range(slides.getCount()):
            if slides.getByIndex(i) == page:
                return {"success": True, "index": i}
        return {"success": True, "index": 0}
