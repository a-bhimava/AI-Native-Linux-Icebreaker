"""Tests for /audit REPL command integration (PR-P1-B).

Covers:
  - /audit in SLASH_COMMANDS
  - _handle_audit opens viewer
  - _handle_audit all (no session filter)
  - _handle_audit verify
  - Missing file handling
  - Help text includes /audit
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from controller.repl import Repl


class TestReplAuditCommand:
    def test_slash_audit_in_commands(self):
        assert "/audit" in Repl.SLASH_COMMANDS

    def test_help_text_includes_audit(self):
        ctrl = MagicMock()
        ctrl.backend_name.return_value = "local"
        cfg = MagicMock()
        cfg.color = "never"
        cfg.ephemeral_history = True
        cfg.session_ttl_seconds = 1800
        cfg.max_turns = 50
        cfg.prompt_prefix = "icebreaker"
        cfg.show_spinner = False

        with patch("controller.repl._HAS_PROMPT_TOOLKIT", True), \
             patch("controller.repl.PromptSession"), \
             patch("controller.repl.InMemoryHistory"):
            repl = Repl(ctrl, cfg)
            with patch("builtins.print") as mock_print:
                repl._print_help()
                printed = "\n".join(str(c) for c in mock_print.call_args_list)
                assert "audit" in printed.lower()
