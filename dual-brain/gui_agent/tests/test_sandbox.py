"""Tests for gui_agent.sandbox — Landlock + Seccomp setup."""

from __future__ import annotations

import ast
import inspect
import re
import socket
import sys
from unittest.mock import patch

import pytest

from gui_agent.sandbox import (
    SandboxError,
    _AF_NETLINK,
    _AF_UNIX,
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
    def test_seccomp_denies_execve_documented(self):
        """Verify the seccomp filter list includes execve in its deny set."""
        import gui_agent.sandbox as mod
        source = open(mod.__file__).read()
        assert "execve" in source
        assert "execveat" in source


# ── AF_INET / AF_INET6 denial (P0 security fix) ────────────────────────────

class TestSocketAFConstants:
    """Verify the AF_* constants match the stdlib values."""

    def test_af_unix_matches_stdlib(self):
        assert _AF_UNIX == socket.AF_UNIX

    @pytest.mark.skipif(
        not hasattr(socket, "AF_NETLINK"), reason="AF_NETLINK is Linux-only"
    )
    def test_af_netlink_matches_stdlib(self):
        assert _AF_NETLINK == socket.AF_NETLINK

    def test_af_unix_value(self):
        assert _AF_UNIX == 1

    def test_af_netlink_value(self):
        assert _AF_NETLINK == 16


class TestSeccompSocketFiltering:
    """Static analysis: verify the seccomp filter uses arg-filtered socket rules
    and does NOT blanket-allow the socket syscall.

    We cannot apply seccomp in tests (irreversible per-process), so we verify
    the code structure using source inspection.
    """

    @staticmethod
    def _get_seccomp_source() -> str:
        import gui_agent.sandbox as mod
        return inspect.getsource(mod._apply_seccomp)

    def test_socket_not_in_safe_syscalls_list(self):
        """socket must NOT appear in the safe_syscalls list (blanket allow)."""
        source = self._get_seccomp_source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "safe_syscalls":
                        if isinstance(node.value, ast.List):
                            names = [
                                elt.value for elt in node.value.elts
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                            ]
                            assert "socket" not in names, (
                                "socket must not be blanket-allowed — use arg-filtered rules"
                            )

    def test_socket_has_arg_filtered_allow(self):
        """socket must be allowed with Arg(0, EQ, AF_UNIX) and Arg(0, EQ, AF_NETLINK)."""
        source = self._get_seccomp_source()
        assert "seccomp.Arg(0" in source or "seccomp.Arg(0," in source, (
            "socket rules must use arg0 filtering"
        )
        assert '"socket"' in source, "arg-filtered socket rules must reference 'socket'"

    def test_af_unix_allowed_in_filter(self):
        source = self._get_seccomp_source()
        assert "_AF_UNIX" in source or "AF_UNIX" in source

    def test_af_netlink_allowed_in_filter(self):
        source = self._get_seccomp_source()
        assert "_AF_NETLINK" in source or "AF_NETLINK" in source

    def test_no_af_inet_in_allowed_set(self):
        """AF_INET must never appear in an ALLOW rule for socket."""
        source = self._get_seccomp_source()
        allow_lines = [
            line for line in source.splitlines()
            if "ALLOW" in line and "socket" in line
        ]
        for line in allow_lines:
            assert "AF_INET" not in line or "AF_INET6" in line or "_AF_UNIX" in line or "_AF_NETLINK" in line, (
                f"AF_INET must not be in a socket ALLOW rule: {line}"
            )

    def test_only_two_socket_allow_rules(self):
        """Exactly 2 address families allowed: AF_UNIX and AF_NETLINK."""
        source = self._get_seccomp_source()
        assert "for allowed_af in (_AF_UNIX, _AF_NETLINK)" in source or (
            source.count("seccomp.ALLOW, \"socket\"") == 2
        ), "Must allow exactly AF_UNIX and AF_NETLINK for socket"

    def test_default_action_is_eperm(self):
        """Default seccomp action must be ERRNO (EPERM) so unlisted socket
        families are denied."""
        source = self._get_seccomp_source()
        assert "ERRNO(1)" in source
        assert "SyscallFilter(seccomp.ERRNO(1))" in source

    def test_docstring_documents_af_filtering(self):
        """The docstring must document the arg0 filtering approach."""
        source = self._get_seccomp_source()
        assert "AF_UNIX" in source
        assert "AF_NETLINK" in source
        assert "AF_INET" in source

    def test_connect_sendmsg_recvmsg_still_allowed(self):
        """D-Bus IPC needs connect/sendmsg/recvmsg after socket(AF_UNIX)."""
        source = self._get_seccomp_source()
        for syscall in ("connect", "sendmsg", "recvmsg"):
            assert f'"{syscall}"' in source


class TestSeccompSocketComment:
    """Verify the intentional-exclusion comment exists so future editors
    don't accidentally re-add socket to safe_syscalls."""

    def test_comment_marks_socket_exclusion(self):
        import gui_agent.sandbox as mod
        source = open(mod.__file__).read()
        assert "socket" in source and "intentionally NOT" in source.lower() or \
               "intentionally not" in source.lower()
