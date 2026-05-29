"""Tests for src/mcpd/sandbox/cow_analysis.py (Architecture 5 — COW diff analysis)."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcpd.sandbox.cow_analysis import analyse_diff, paths_from_overlayfs_upper, DiffAnalysis

_FS_WRITE_INTENT = {
    "intent_id": "00000000-0000-0000-0000-000000000002",
    "action": "fs.write",
    "target": "/var/log/myapp.log",
    "params": {},
    "reason": "user_requested",
    "risk_level": "medium",
}

_SERVICE_INTENT = {
    "intent_id": "00000000-0000-0000-0000-000000000003",
    "action": "service.restart",
    "target": "nginx",
    "params": {},
    "reason": "user_requested",
    "risk_level": "medium",
}

_PACKAGE_INTENT = {
    "intent_id": "00000000-0000-0000-0000-000000000004",
    "action": "package.install",
    "target": "curl",
    "params": {},
    "reason": "user_requested",
    "risk_level": "medium",
}


class TestAnalyseDiff:
    def test_within_scope_fs_write(self):
        result = analyse_diff(_FS_WRITE_INTENT, ["/var/log/myapp.log"])
        assert result.within_scope is True
        assert not result.unexpected_paths
        assert not result.sensitive_paths

    def test_unexpected_path_detected(self):
        result = analyse_diff(_FS_WRITE_INTENT, ["/var/log/myapp.log", "/etc/unexpected.conf"])
        assert result.within_scope is False
        assert "/etc/unexpected.conf" in result.unexpected_paths

    def test_sensitive_path_detected(self):
        result = analyse_diff(_FS_WRITE_INTENT, ["/boot/grub/grub.cfg"])
        assert result.within_scope is False
        assert "/boot/grub/grub.cfg" in result.sensitive_paths
        assert "HITL" in result.summary

    def test_correction_populated_for_unexpected(self):
        result = analyse_diff(_FS_WRITE_INTENT, ["/var/log/myapp.log", "/etc/myapp.conf"])
        assert result.within_scope is False
        assert "restrict_to" in result.correction.get("params", {})

    def test_no_correction_for_sensitive(self):
        # Sensitive paths escalate to HITL — no correction should be generated
        result = analyse_diff(_FS_WRITE_INTENT, ["/etc/sudoers"])
        assert result.correction == {}

    def test_tmp_always_allowed(self):
        result = analyse_diff(_SERVICE_INTENT, ["/tmp/systemd_tmp_12345"])
        assert result.within_scope is True

    def test_user_home_always_allowed(self):
        home = os.path.expanduser("~")
        result = analyse_diff(_SERVICE_INTENT, [f"{home}/.cache/something"])
        assert result.within_scope is True

    def test_package_scope(self):
        result = analyse_diff(_PACKAGE_INTENT, ["/usr/bin/curl", "/var/lib/dpkg/info/curl.list"])
        assert result.within_scope is True

    def test_empty_paths(self):
        result = analyse_diff(_FS_WRITE_INTENT, [])
        assert result.within_scope is True
        assert result.summary  # summary should still be populated

    def test_summary_within_scope(self):
        result = analyse_diff(_FS_WRITE_INTENT, ["/var/log/myapp.log"])
        assert "within expected scope" in result.summary

    def test_summary_unexpected(self):
        result = analyse_diff(_FS_WRITE_INTENT, ["/var/log/myapp.log", "/opt/surprise"])
        assert "unexpected" in result.summary.lower()


class TestPathsFromOverlayfsupper:
    def test_empty_upper_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = paths_from_overlayfs_upper(tmpdir)
            assert paths == []

    def test_files_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Simulate overlayfs upper with some changed files
            (Path(tmpdir) / "etc").mkdir()
            (Path(tmpdir) / "etc" / "nginx.conf").write_text("server {}")
            (Path(tmpdir) / "var").mkdir()
            (Path(tmpdir) / "var" / "log").mkdir()
            (Path(tmpdir) / "var" / "log" / "app.log").write_text("line1")

            paths = paths_from_overlayfs_upper(tmpdir)
            assert "/etc/nginx.conf" in paths
            assert "/var/log/app.log" in paths

    def test_nonexistent_dir_returns_empty(self):
        paths = paths_from_overlayfs_upper("/nonexistent/path/xyz")
        assert paths == []
