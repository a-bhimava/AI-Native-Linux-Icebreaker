"""Tests for the Tier 0–3 risk classifier.

The classifier is the gate that decides which intents auto-execute, which
get notified, and which require HITL. Drift between this logic and mcpd's
catalogue is dangerous; the test_mcpd_catalogue suite covers that surface.
This suite focuses on the tier-decision rules themselves.
"""

import os
from unittest.mock import patch

import pytest

from controller._mcpd_tools import ALL_TOOLS
from controller.risk_classifier import (
    ClassificationResult,
    Tier,
    _get_user_home,
    _target_is_in_user_home,
    classify,
)


HOME = os.path.expanduser("~")


def _make_intent(action: str, target: str = "", risk_level: str = "low") -> dict:
    return {
        "action": action,
        "target": target,
        "params": {},
        "reason": "test",
        "risk_level": risk_level,
    }


# ── Tier 0: read-only ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", [
    "system.status", "system.uptime", "system.cpu",
    "system.memory", "system.disk",
    "process.list", "process.inspect",
    "fs.read", "fs.list", "fs.stat",
    "service.logs",
    "network.status", "network.dns.read",
    "package.query",
])
def test_tier0_read_only(action):
    r = classify(_make_intent(action))
    assert r.tier == Tier.READ_ONLY
    assert r.auto_execute
    assert not r.requires_hitl
    assert r.reversible


# ── Tier 1: fs.write inside $HOME ────────────────────────────────────────────

# These pin HOME to a neutral path instead of using the invoking user's:
# under root, expanduser("~") is /root, which the classifier correctly
# tiers HIGH as a protected system path — the test must not depend on
# who runs the suite. (_get_user_home resolves at call time; see the
# resolution test below.)

def test_fs_write_in_home_is_tier1(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    r = classify(_make_intent("fs.write", "/home/testuser/notes.md"))
    assert r.tier == Tier.LOW
    assert r.auto_execute
    assert not r.requires_hitl


def test_fs_write_deep_in_home_is_tier1(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testuser")
    r = classify(_make_intent("fs.write", "/home/testuser/projects/icebreaker/scratch"))
    assert r.tier == Tier.LOW


# ── Tier 2: system writes ────────────────────────────────────────────────────

@pytest.mark.parametrize("action,target", [
    ("package.install", "curl"),
    ("package.remove",  "vim"),
    ("package.upgrade", "openssl"),
    ("service.start",   "nginx.service"),
    ("service.stop",    "nginx.service"),
    ("service.restart", "nginx.service"),
])
def test_tier2_system_write(action, target):
    r = classify(_make_intent(action, target, risk_level="medium"))
    assert r.tier == Tier.MEDIUM
    assert r.auto_execute
    assert not r.requires_hitl


def test_package_actions_are_reversible():
    r = classify(_make_intent("package.install", "htop"))
    assert r.reversible is True


def test_service_actions_marked_irreversible():
    r = classify(_make_intent("service.restart", "nginx"))
    assert r.reversible is False


# ── Tier 3: destructive tools ────────────────────────────────────────────────

def test_fs_delete_is_tier3():
    r = classify(_make_intent("fs.delete", "/var/log/app.log"))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl
    assert not r.auto_execute


# ── Tier 3: fs.write outside $HOME ───────────────────────────────────────────

def test_fs_write_outside_home_is_tier3():
    r = classify(_make_intent("fs.write", "/etc/nginx/nginx.conf"))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl


# ── Tier 3: critical path targets ────────────────────────────────────────────

@pytest.mark.parametrize("target", [
    "/boot/grub/grub.cfg",
    "/etc/sudoers",
    "/etc/shadow",
    "/etc/passwd",
    "/root/.ssh/authorized_keys",
    "/proc/sys/kernel/random/boot_id",
    "/dev/sda",
    "/dev/nvme0n1",
    "/sys/firmware/efi/efivars/anything",
])
def test_critical_paths_force_tier3(target):
    # Even with a Tier 0 read action, a critical-path target escalates to HIGH.
    r = classify(_make_intent("fs.read", target))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl
    assert r.blocked_pattern is not None


# ── Tier 3: explicit risk_level=="critical" ──────────────────────────────────

def test_explicit_critical_risk_level_forces_tier3():
    r = classify(_make_intent("fs.write", f"{HOME}/notes", risk_level="critical"))
    assert r.tier == Tier.HIGH
    assert r.requires_hitl


# ── Coverage: no tool falls into the "Unclassified" fallback ─────────────────

def test_every_mcpd_tool_classifies_without_fallback():
    fallback_msg = "Unclassified action"  # BP-5 escalation path
    unclassified = []
    for action in sorted(ALL_TOOLS):
        # For fs.write, supply an in-home target so it lands in Tier 1
        # (the alternate Tier 3 outside-home path is exercised above).
        target = f"{HOME}/scratch" if action == "fs.write" else ""
        r = classify(_make_intent(action, target))
        if fallback_msg in r.reason:
            unclassified.append(action)
    assert not unclassified, f"Tools landed in 'Unclassified' fallback: {unclassified}"


# ── Result invariants ────────────────────────────────────────────────────────

def test_classification_result_invariants():
    # requires_hitl exactly when tier == HIGH; auto_execute exactly otherwise.
    for action in ALL_TOOLS:
        target = f"{HOME}/x" if action == "fs.write" else ""
        r = classify(_make_intent(action, target))
        assert isinstance(r, ClassificationResult)
        assert r.requires_hitl == (r.tier == Tier.HIGH)
        assert r.auto_execute != r.requires_hitl


# ── Path traversal: prefix collision (CVE-grade, P0) ────────────────────────

class TestPathTraversalPrefixCollision:
    """Verify _target_is_in_user_home rejects paths that merely share a prefix
    with the home dir (e.g. /home/alice-evil vs /home/alice)."""

    def test_exact_home_dir_accepted(self):
        assert _target_is_in_user_home(HOME) is True

    def test_child_of_home_accepted(self):
        assert _target_is_in_user_home(f"{HOME}/notes.md") is True

    def test_deep_child_of_home_accepted(self):
        assert _target_is_in_user_home(f"{HOME}/a/b/c/d/file") is True

    def test_prefix_collision_rejected(self):
        assert _target_is_in_user_home(f"{HOME}-evil/payload") is False

    def test_prefix_collision_no_slash_rejected(self):
        assert _target_is_in_user_home(f"{HOME}SUFFIX") is False

    def test_prefix_collision_dot_rejected(self):
        assert _target_is_in_user_home(f"{HOME}..sneaky/x") is False

    def test_sibling_dir_rejected(self):
        parent = os.path.dirname(HOME)
        assert _target_is_in_user_home(f"{parent}/other-user/file") is False

    def test_parent_dir_rejected(self):
        parent = os.path.dirname(HOME)
        assert _target_is_in_user_home(parent) is False

    def test_root_rejected(self):
        assert _target_is_in_user_home("/") is False

    def test_empty_string_rejected(self):
        assert _target_is_in_user_home("") is False

    def test_none_like_empty_rejected(self):
        assert _target_is_in_user_home("") is False

    def test_dotdot_escape_from_home_rejected(self):
        assert _target_is_in_user_home(f"{HOME}/../etc/shadow") is False

    def test_classify_prefix_collision_is_tier3(self):
        r = classify(_make_intent("fs.write", f"{HOME}-evil/payload"))
        assert r.tier == Tier.HIGH
        assert r.requires_hitl

    def test_classify_dotdot_escape_is_tier3(self):
        r = classify(_make_intent("fs.write", f"{HOME}/../../../etc/passwd"))
        assert r.tier == Tier.HIGH
        assert r.requires_hitl


# ── $HOME not frozen at import time ──────────────────────────────────────────

class TestHomeDirNotFrozen:
    """_get_user_home() must resolve at call time, not import time."""

    def test_home_follows_env_change(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser42")
        assert _get_user_home() == "/home/testuser42"

    def test_home_follows_second_env_change(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/first")
        assert _get_user_home() == "/home/first"
        monkeypatch.setenv("HOME", "/home/second")
        assert _get_user_home() == "/home/second"

    def test_classify_uses_current_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/dynamic_user")
        r = classify(_make_intent("fs.write", "/home/dynamic_user/notes.md"))
        assert r.tier == Tier.LOW


# ── BP-5: unclassified actions escalate to HIGH ─────────────────────────────

class TestUnclassifiedActionEscalation:
    """Unknown actions must default to Tier.HIGH (BP-5 escalate-only)."""

    def test_unknown_action_is_tier3(self):
        r = classify(_make_intent("totally.bogus.action", "/tmp/x"))
        assert r.tier == Tier.HIGH
        assert r.requires_hitl
        assert not r.auto_execute

    def test_unknown_action_not_reversible(self):
        r = classify(_make_intent("unknown.cmd"))
        assert r.reversible is False

    def test_empty_action_is_tier3(self):
        r = classify(_make_intent(""))
        assert r.tier == Tier.HIGH

    def test_unknown_reason_mentions_bp5(self):
        r = classify(_make_intent("injection.attempt"))
        assert "BP-5" in r.reason
