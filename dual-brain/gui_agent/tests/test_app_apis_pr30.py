"""Tests for PR #30 — Firefox and GNOME Files app APIs."""
from __future__ import annotations

import pytest

from gui_agent.app_apis.firefox import FirefoxApi
from gui_agent.app_apis.gnome_files import GnomeFilesApi, _validate_absolute_path


class TestFirefoxApi:
    def test_supports_firefox_titles(self):
        api = FirefoxApi()
        assert api.supports("Mozilla Firefox")
        assert api.supports("Iceweasel")
        assert api.supports("Example Page - Mozilla Firefox")

    def test_rejects_non_firefox(self):
        api = FirefoxApi()
        assert not api.supports("LibreOffice Calc")
        assert not api.supports("Terminal")
        assert not api.supports("")

    def test_capabilities(self):
        api = FirefoxApi()
        caps = api.capabilities()
        for action in ("navigate", "screenshot_tab", "get_dom_element", "click", "type_in_form"):
            assert action in caps
        assert len(caps) == 5

    def test_url_validation_rejects_file(self):
        api = FirefoxApi()
        err = api._validate_url("file:///etc/passwd")
        assert err is not None
        assert "file" in err.lower()

    def test_url_validation_rejects_javascript(self):
        api = FirefoxApi()
        err = api._validate_url("javascript:alert(1)")
        assert err is not None
        assert "javascript" in err.lower()

    def test_url_validation_accepts_https(self):
        api = FirefoxApi()
        assert api._validate_url("https://example.com") is None

    def test_fallback_when_websockets_unavailable(self):
        api = FirefoxApi.__new__(FirefoxApi)
        api._ws_mod = None
        api._available = False
        api._init_error = "test"
        api._cdp_port = 9222
        api._msg_id = 0
        result = api.execute("navigate", {"url": "https://example.com"})
        assert result["success"] is False
        assert result["fallback"] == "atspi"
        assert "websockets" in result["error"].lower()


class TestGnomeFilesApi:
    def test_supports_nautilus_titles(self):
        api = GnomeFilesApi()
        assert api.supports("Files")
        assert api.supports("Home - Files")
        assert api.supports("Nautilus")

    def test_rejects_non_nautilus(self):
        api = GnomeFilesApi()
        assert not api.supports("Firefox")
        assert not api.supports("Terminal")

    def test_capabilities(self):
        api = GnomeFilesApi()
        caps = api.capabilities()
        assert "open_folder" in caps
        assert "rename" in caps
        assert "select_file" in caps

    def test_path_validation_rejects_relative(self):
        err = _validate_absolute_path("relative/path")
        assert err is not None
        assert err["success"] is False
        assert "absolute" in err["error"]


class TestRegistration:
    def test_pr30_apis_registered(self):
        import importlib
        from gui_agent.app_apis.registry import _reset_registry_for_tests
        _reset_registry_for_tests()
        import gui_agent.app_apis.firefox as ff_mod
        import gui_agent.app_apis.gnome_files as naut_mod
        importlib.reload(ff_mod)
        importlib.reload(naut_mod)
        from gui_agent.app_apis import list_app_apis
        names = list_app_apis()
        assert "firefox" in names
        assert "gnome_files" in names
