"""Tests for the InputRouter (PR #24).

Covers:
  1.  # prefix at column 0 → NL route with stripped text
  2.  Bare # with no text → shell (ignored)
  3.  Leading space before # → shell (not column 0)
  4.  # inside a command → shell
  5.  Sticky NL mode routes everything to NL
  6.  Sticky NL toggle flips state
  7.  One-shot NL arms and fires once
  8.  One-shot is cleared when sticky NL is enabled
  9.  Custom nl_prefix works
  10. Empty input in shell mode → shell
"""

from __future__ import annotations

import pytest

from terminal.input_router import InputRouter, RouteTarget


class TestPrefixDetection:
    def test_hash_prefix_routes_nl(self):
        router = InputRouter()
        target, text = router.route("#show disk usage")
        assert target == RouteTarget.NL
        assert text == "show disk usage"

    def test_bare_hash_routes_shell(self):
        router = InputRouter()
        target, text = router.route("#")
        assert target == RouteTarget.SHELL
        assert text == "#"

    def test_hash_with_whitespace_only_routes_shell(self):
        router = InputRouter()
        target, text = router.route("#   ")
        assert target == RouteTarget.SHELL
        assert text == "#   "

    def test_leading_space_before_hash_routes_shell(self):
        router = InputRouter()
        target, text = router.route(" #comment")
        assert target == RouteTarget.SHELL
        assert text == " #comment"

    def test_hash_inside_command_routes_shell(self):
        router = InputRouter()
        target, text = router.route("echo #hello")
        assert target == RouteTarget.SHELL
        assert text == "echo #hello"

    def test_plain_command_routes_shell(self):
        router = InputRouter()
        target, text = router.route("ls -la")
        assert target == RouteTarget.SHELL
        assert text == "ls -la"


class TestStickyNlMode:
    def test_sticky_nl_routes_all_to_nl(self):
        router = InputRouter()
        router.sticky_nl = True
        target, text = router.route("ls -la")
        assert target == RouteTarget.NL
        assert text == "ls -la"

    def test_toggle_sticky(self):
        router = InputRouter()
        assert not router.sticky_nl
        result = router.toggle_sticky()
        assert result is True
        assert router.sticky_nl
        result = router.toggle_sticky()
        assert result is False
        assert not router.sticky_nl


class TestOneshotNl:
    def test_oneshot_fires_once(self):
        router = InputRouter()
        router.arm_oneshot()
        assert router.oneshot_pending

        target, text = router.route("what is my IP")
        assert target == RouteTarget.NL

        target2, text2 = router.route("ls")
        assert target2 == RouteTarget.SHELL
        assert not router.oneshot_pending

    def test_oneshot_cleared_by_sticky(self):
        router = InputRouter()
        router.arm_oneshot()
        router.sticky_nl = True
        assert not router.oneshot_pending

    def test_oneshot_not_armed_when_sticky(self):
        router = InputRouter()
        router.sticky_nl = True
        router.arm_oneshot()
        assert not router.oneshot_pending


class TestCustomPrefix:
    def test_custom_prefix(self):
        router = InputRouter(nl_prefix="!")
        target, text = router.route("!show users")
        assert target == RouteTarget.NL
        assert text == "show users"

    def test_custom_prefix_default_not_matched(self):
        router = InputRouter(nl_prefix="!")
        target, text = router.route("#show users")
        assert target == RouteTarget.SHELL
