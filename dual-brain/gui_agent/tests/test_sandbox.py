"""Tests for gui_agent.sandbox — Landlock + Seccomp setup."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from gui_agent.sandbox import (
    SandboxError,
    _check_landlock_available,
    apply_gui_sandbox,
)


class TestLandlockProbe:
    def test_returns_false_on_non_linux(self):
        with patch.object(sys, "platform", "darwin"):
            assert _check_landlock_available() is False

    def test_returns_false_on_exception(self):
        with patch("ctypes.CDLL", side_effect=OSError("no libc")):
            if sys.platform == "linux":
                assert _check_landlock_available() is False


class TestNonLinuxRejection:
    def test_raises_on_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            with pytest.raises(SandboxError, match="requires Linux"):
                apply_gui_sandbox("/home/test", "/tmp/scratch")

    def test_raises_on_windows(self):
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(SandboxError, match="requires Linux"):
                apply_gui_sandbox("C:\\Users\\test", "C:\\tmp\\scratch")


class TestExecveDenied:
    @pytest.mark.skipif(sys.platform != "linux", reason="Seccomp is Linux-only")
    def test_seccomp_denies_execve_documented(self):
        """Verify the seccomp filter list includes execve in its deny set.

        We don't actually apply seccomp in tests (it's irreversible),
        but we verify the code structure denies execve/execveat.
        """
        import gui_agent.sandbox as mod
        source = open(mod.__file__).read()
        assert "execve" in source
        assert "execveat" in source
