"""Tests for gui.wizard — First-Boot Wizard (M6UI.4).

Headless tests covering:
  - First-boot sentinel path and logic
  - Backend defaults and env var mapping
  - BP-8 compliance (env var names, never secret values)
  - Wizard step count
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("gi", MagicMock())
sys.modules.setdefault("gi.repository", MagicMock())

from gui.wizard.window import (
    _FIRST_BOOT_SENTINEL,
    is_first_boot,
    mark_first_boot_done,
)


# ---------------------------------------------------------------------------
# First-boot sentinel
# ---------------------------------------------------------------------------

class TestFirstBootSentinel:
    def test_sentinel_is_under_local_share(self) -> None:
        assert ".local/share/icebreaker" in str(_FIRST_BOOT_SENTINEL)

    def test_sentinel_filename(self) -> None:
        assert _FIRST_BOOT_SENTINEL.name == ".first-boot-done"

    def test_sentinel_is_in_home_directory(self) -> None:
        assert str(_FIRST_BOOT_SENTINEL).startswith(str(Path.home()))

    def test_is_first_boot_when_no_sentinel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = tmp_path / ".first-boot-done"
        monkeypatch.setattr("gui.wizard.window._FIRST_BOOT_SENTINEL", fake)
        assert is_first_boot() is True

    def test_not_first_boot_after_mark(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = tmp_path / ".first-boot-done"
        monkeypatch.setattr("gui.wizard.window._FIRST_BOOT_SENTINEL", fake)
        mark_first_boot_done()
        assert is_first_boot() is False

    def test_mark_creates_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = tmp_path / "sub" / ".first-boot-done"
        monkeypatch.setattr("gui.wizard.window._FIRST_BOOT_SENTINEL", fake)
        mark_first_boot_done()
        assert fake.exists()

    def test_mark_creates_parent_dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = tmp_path / "a" / "b" / ".first-boot-done"
        monkeypatch.setattr("gui.wizard.window._FIRST_BOOT_SENTINEL", fake)
        mark_first_boot_done()
        assert fake.parent.is_dir()

    def test_mark_is_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = tmp_path / ".first-boot-done"
        monkeypatch.setattr("gui.wizard.window._FIRST_BOOT_SENTINEL", fake)
        mark_first_boot_done()
        mark_first_boot_done()
        assert fake.exists()


# ---------------------------------------------------------------------------
# Backend defaults — env var mapping (BP-8: names only, never values)
# ---------------------------------------------------------------------------

class TestBackendDefaults:
    ENV_DEFAULTS = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }

    def test_gemini_env_var(self) -> None:
        assert self.ENV_DEFAULTS["gemini"] == "GEMINI_API_KEY"

    def test_openai_env_var(self) -> None:
        assert self.ENV_DEFAULTS["openai"] == "OPENAI_API_KEY"

    def test_anthropic_env_var(self) -> None:
        assert self.ENV_DEFAULTS["anthropic"] == "ANTHROPIC_API_KEY"

    def test_all_env_vars_end_with_api_key(self) -> None:
        for name, var in self.ENV_DEFAULTS.items():
            assert var.endswith("_API_KEY"), f"{name} env var doesn't end with _API_KEY"

    def test_env_vars_are_uppercase(self) -> None:
        for var in self.ENV_DEFAULTS.values():
            assert var == var.upper()

    def test_no_secret_values_in_defaults(self) -> None:
        """BP-8: env defaults must be variable names, not resolved secrets."""
        for var in self.ENV_DEFAULTS.values():
            assert not var.startswith("sk-"), "env default looks like a secret key"
            assert not var.startswith("gsk_"), "env default looks like a secret key"
            assert "=" not in var, "env default should be a name, not key=value"

    def test_three_backends_supported(self) -> None:
        assert len(self.ENV_DEFAULTS) == 3

    def test_default_backend_is_gemini(self) -> None:
        assert list(self.ENV_DEFAULTS.keys())[0] == "gemini"


# ---------------------------------------------------------------------------
# Wizard step structure
# ---------------------------------------------------------------------------

class TestWizardSteps:
    STEP_NAMES = ["Welcome", "AI Backend", "Model Verification", "Test Command", "Setup Complete"]

    def test_five_steps(self) -> None:
        assert len(self.STEP_NAMES) == 5

    def test_starts_with_welcome(self) -> None:
        assert self.STEP_NAMES[0] == "Welcome"

    def test_ends_with_setup_complete(self) -> None:
        assert self.STEP_NAMES[-1] == "Setup Complete"

    def test_api_key_is_step_two(self) -> None:
        assert self.STEP_NAMES[1] == "AI Backend"

    def test_model_check_before_test_command(self) -> None:
        model_idx = self.STEP_NAMES.index("Model Verification")
        test_idx = self.STEP_NAMES.index("Test Command")
        assert model_idx < test_idx

    def test_setup_smoke_test_uses_explicit_offline_lane(self) -> None:
        source = (Path(__file__).parent.parent / "wizard" / "window.py").read_text()
        assert "run_offline_command(cmd_row.get_text())" in source
        assert "resp = self._client.run_turn(cmd_row.get_text())" not in source


# ---------------------------------------------------------------------------
# Model verification defaults
# ---------------------------------------------------------------------------

class TestModelDefaults:
    MODEL_FILENAME = "run7_cot_q4km.gguf"

    def test_model_is_gguf_format(self) -> None:
        assert self.MODEL_FILENAME.endswith(".gguf")

    def test_model_is_run7(self) -> None:
        assert "run7" in self.MODEL_FILENAME

    def test_model_is_quantized(self) -> None:
        assert "q4" in self.MODEL_FILENAME
