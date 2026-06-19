"""Tests for RPA Bridge sandbox (PR #28)."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from rpa_bridge.sandbox import SandboxError, apply_rpa_sandbox


class TestNonLinuxRejection:
    def test_raises_on_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            with pytest.raises(SandboxError, match="requires Linux"):
                apply_rpa_sandbox("/home/test", "/tmp/scratch")

    def test_raises_on_windows(self):
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(SandboxError, match="requires Linux"):
                apply_rpa_sandbox("/home/test", "/tmp/scratch")


class TestUinputRule:
    def test_sandbox_source_references_uinput(self):
        import rpa_bridge.sandbox as mod
        source = open(mod.__file__).read()
        assert "/dev/uinput" in source

    def test_sandbox_more_permissive_than_gui(self):
        """RPA sandbox adds /dev/uinput rule that GUI sandbox doesn't have."""
        import gui_agent.sandbox as gui_mod
        import rpa_bridge.sandbox as rpa_mod
        gui_src = open(gui_mod.__file__).read()
        rpa_src = open(rpa_mod.__file__).read()
        assert "/dev/uinput" not in gui_src
        assert "/dev/uinput" in rpa_src

    def test_alarm_in_seccomp_allowlist(self):
        """SIGALRM is needed for timeout — alarm must be in the seccomp safe list."""
        import rpa_bridge.sandbox as mod
        source = open(mod.__file__).read()
        assert '"alarm"' in source
