"""Tests for the --safe-mode CLI flag."""

from controller.__main__ import main


def test_safe_mode_recognized():
    rc = main(["--safe-mode"])
    assert rc == 1


def test_safe_mode_exclusive_with_repl():
    rc = main(["--safe-mode", "--repl"])
    assert rc == 2


def test_safe_mode_exclusive_with_command():
    rc = main(["--safe-mode", "some command"])
    assert rc == 2


def test_safe_mode_exclusive_with_daemon():
    rc = main(["--safe-mode", "--daemon"])
    assert rc == 2
