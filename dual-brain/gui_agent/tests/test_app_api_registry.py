"""Tests for app API registry, LibreOffice API, and GenericAtSpi fallback (PR #27)."""

from __future__ import annotations

import pytest

from gui_agent.app_apis.base import AppApi
from gui_agent.app_apis.registry import (
    _reset_registry_for_tests,
    get_app_api,
    list_app_apis,
    register_app_api,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset registry before/after each test to avoid cross-contamination."""
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


class TestRegistryDecorator:
    def test_register_and_list(self):
        @register_app_api("test_app")
        class TestApi(AppApi):
            def supports(self, window_title):
                return False
            def execute(self, action, params):
                return {}
            def capabilities(self):
                return []

        assert "test_app" in list_app_apis()

    def test_duplicate_registration_raises(self):
        @register_app_api("dup")
        class First(AppApi):
            def supports(self, wt): return False
            def execute(self, a, p): return {}
            def capabilities(self): return []

        with pytest.raises(ValueError, match="already registered"):
            @register_app_api("dup")
            class Second(AppApi):
                def supports(self, wt): return False
                def execute(self, a, p): return {}
                def capabilities(self): return []

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            @register_app_api("")
            class Bad(AppApi):
                def supports(self, wt): return False
                def execute(self, a, p): return {}
                def capabilities(self): return []

    def test_non_subclass_raises(self):
        with pytest.raises(TypeError, match="not an AppApi subclass"):
            @register_app_api("bad")
            class NotAnApi:
                pass


class TestGetAppApi:
    def test_returns_matching_api(self):
        @register_app_api("matcher")
        class Matcher(AppApi):
            def supports(self, wt):
                return "Special" in wt
            def execute(self, a, p):
                return {"matched": True}
            def capabilities(self):
                return ["test"]

        result = get_app_api("My Special App")
        assert result is not None
        assert isinstance(result, Matcher)

    def test_returns_none_when_no_match(self):
        @register_app_api("nomatch")
        class NoMatch(AppApi):
            def supports(self, wt):
                return False
            def execute(self, a, p):
                return {}
            def capabilities(self):
                return []

        assert get_app_api("anything") is None

    def test_list_empty_after_reset(self):
        assert list_app_apis() == ()


class TestLibreOfficeApiSupports:
    """Test LibreOffice window title matching without needing UNO."""

    def _get_lo_class(self):
        from gui_agent.app_apis.libreoffice import LibreOfficeApi
        return LibreOfficeApi

    def test_matches_calc_title(self):
        api = self._get_lo_class()()
        assert api.supports("Untitled 1 - LibreOffice Calc")

    def test_matches_writer_title(self):
        api = self._get_lo_class()()
        assert api.supports("document.odt - LibreOffice Writer")

    def test_matches_impress_title(self):
        api = self._get_lo_class()()
        assert api.supports("slides.odp - LibreOffice Impress")

    def test_matches_file_extension(self):
        api = self._get_lo_class()()
        assert api.supports("budget.xlsx - LibreOffice Calc")

    def test_rejects_firefox(self):
        api = self._get_lo_class()()
        assert not api.supports("Mozilla Firefox")

    def test_rejects_empty(self):
        api = self._get_lo_class()()
        assert not api.supports("")


class TestLibreOfficeApiExecute:
    """Test execute() with UNO unavailable — should return structured error."""

    def test_returns_fallback_when_uno_unavailable(self):
        from gui_agent.app_apis.libreoffice import LibreOfficeApi
        api = LibreOfficeApi()
        if api.available:
            pytest.skip("UNO is available — test only valid without UNO")
        result = api.execute("get_cell", {"component": "calc"})
        assert result["success"] is False
        assert "UNO bridge not available" in result["error"]
        assert result["fallback"] == "atspi"

    def test_capabilities_returns_all_actions(self):
        from gui_agent.app_apis.libreoffice import LibreOfficeApi
        api = LibreOfficeApi()
        caps = api.capabilities()
        assert "open_document" in caps
        assert "get_cell" in caps
        assert "insert_slide" in caps
        assert len(caps) > 10

    def test_invalid_action_rejected(self):
        from gui_agent.app_apis.libreoffice import LibreOfficeApi
        api = LibreOfficeApi()
        if api.available:
            pytest.skip("UNO is available — test only valid without UNO")
        result = api.execute("nonexistent", {"component": "calc"})
        assert result["success"] is False


class TestGenericAtSpiApi:
    def test_supports_everything(self):
        from gui_agent.app_apis.generic_atspi import GenericAtSpiApi
        api = GenericAtSpiApi()
        assert api.supports("Firefox")
        assert api.supports("Terminal")
        assert api.supports("")

    def test_capabilities(self):
        from gui_agent.app_apis.generic_atspi import GenericAtSpiApi
        api = GenericAtSpiApi()
        caps = api.capabilities()
        assert "click" in caps
        assert "type" in caps
        assert "find_element" in caps

    def test_invalid_action_rejected(self):
        from gui_agent.app_apis.generic_atspi import GenericAtSpiApi
        api = GenericAtSpiApi()
        result = api.execute("nonexistent", {})
        assert result["success"] is False


class TestAgentAppApiIntegration:
    """Test that GuiAgent routes to app API when prefer_app_api=True."""

    def test_try_app_api_returns_none_when_disabled(self):
        from gui_agent.agent import GuiAgent
        agent = GuiAgent(prefer_app_api=False)
        result = agent._try_app_api("click", {"window": "Test"})
        assert result is None

    def test_try_app_api_returns_none_for_empty_window(self):
        from gui_agent.agent import GuiAgent
        agent = GuiAgent(prefer_app_api=True)
        result = agent._try_app_api("click", {"window": ""})
        assert result is None

    def test_try_app_api_falls_back_on_uno_error(self):
        from gui_agent.agent import GuiAgent
        agent = GuiAgent(prefer_app_api=True)
        result = agent._try_app_api("get_cell", {
            "window": "budget.xlsx - LibreOffice Calc",
            "component": "calc",
        })
        # UNO is not available on macOS, so it should return fallback=atspi → None
        assert result is None
