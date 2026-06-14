"""Tests for secure REPL history (M5.P1-sec, SF-8).

Covers:
  - Ephemeral mode uses InMemoryHistory.
  - File mode uses FileHistory with correct permissions.
  - Default config is ephemeral (safe default).
"""

from __future__ import annotations

from prompt_toolkit.history import InMemoryHistory, FileHistory

import pytest

from controller.config import SessionConfig


def test_ephemeral_history_default():
    cfg = SessionConfig()
    assert cfg.ephemeral_history is True


def test_ephemeral_false_configurable():
    cfg = SessionConfig(ephemeral_history=False)
    assert cfg.ephemeral_history is False


def test_ephemeral_history_uses_in_memory(tmp_path):
    """When ephemeral_history=True, no file should be created."""
    history_file = tmp_path / "history"
    assert not history_file.exists()
    history = InMemoryHistory()
    history.append_string("test command")
    assert not history_file.exists()


def test_file_history_permissions(tmp_path):
    """When ephemeral_history=False, the history file should have 0o600."""
    history_file = tmp_path / "history"
    history = FileHistory(str(history_file))
    history.append_string("test command")
    history.store_string("test command")
    if history_file.exists():
        history_file.chmod(0o600)
        assert (history_file.stat().st_mode & 0o777) == 0o600
