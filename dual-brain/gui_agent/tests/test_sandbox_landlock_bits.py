"""Regression guard for F-107 (2026-08-02): Landlock ABI v1 bit values.

The pre-V.6a code used WRONG bit values for every LANDLOCK_ACCESS_FS_*
constant (READ_FILE=1<<0 when kernel says EXECUTE=1<<0, WRITE_FILE=1<<5
when kernel says REMOVE_FILE=1<<5, etc). This caused the sandbox to
silently mis-wire which access classes it was controlling and which
paths were granted which rights. This test enforces the correct values
against the kernel UAPI header canonical table so a future refactor
can't silently reintroduce the bug.

Kernel source of truth:
  include/uapi/linux/landlock.h @ upstream torvalds/linux
  https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git

Verified via `gh api repos/torvalds/linux/contents/include/uapi/linux/
landlock.h` on 2026-08-02 — every #define LANDLOCK_ACCESS_FS_* has a
literal `(1ULL << N)` value that must match the constants below.
"""

from __future__ import annotations

import pytest


# Canonical kernel UAPI values — see file docstring.
# These SHALL NOT change across kernel versions per Landlock's
# stable-ABI commitment. If the kernel adds a new bit (e.g. TRUNCATE
# at 1<<14 in ABI v3, IOCTL_DEV at 1<<15 in v5), it goes into a NEW
# constant, never a renumber of an existing one.
_KERNEL_CANONICAL_BITS: dict[str, int] = {
    "EXECUTE":      1 << 0,
    "WRITE_FILE":   1 << 1,
    "READ_FILE":    1 << 2,
    "READ_DIR":     1 << 3,
    "REMOVE_DIR":   1 << 4,
    "REMOVE_FILE":  1 << 5,
    "MAKE_CHAR":    1 << 6,
    "MAKE_DIR":     1 << 7,
    "MAKE_REG":     1 << 8,
    "MAKE_SOCK":    1 << 9,
    "MAKE_FIFO":    1 << 10,
    "MAKE_BLOCK":   1 << 11,
    "MAKE_SYM":     1 << 12,
}


class TestGuiAgentLandlockBits:
    """Assert every LANDLOCK_ACCESS_FS_* constant in gui_agent/sandbox.py
    matches the kernel UAPI canonical bit value."""

    @pytest.mark.parametrize("name,expected", _KERNEL_CANONICAL_BITS.items())
    def test_constant_matches_kernel_value(self, name: str, expected: int) -> None:
        import gui_agent.sandbox as mod
        attr_name = f"_LANDLOCK_ACCESS_FS_{name}"
        assert hasattr(mod, attr_name), (
            f"{attr_name} missing from gui_agent.sandbox — kernel defines "
            f"this bit at 1<<{expected.bit_length() - 1}"
        )
        actual = getattr(mod, attr_name)
        assert actual == expected, (
            f"{attr_name} = {actual} (bit {actual.bit_length() - 1}) but "
            f"kernel UAPI says {expected} (bit {expected.bit_length() - 1}). "
            f"This is the F-107 regression."
        )

    def test_read_only_composite_correct(self):
        """READ_ONLY = READ_FILE | READ_DIR — no execute, no write."""
        from gui_agent.sandbox import (
            _LANDLOCK_ACCESS_FS_READ_FILE,
            _LANDLOCK_ACCESS_FS_READ_DIR,
            _LANDLOCK_ACCESS_FS_EXECUTE,
            _LANDLOCK_ACCESS_FS_WRITE_FILE,
            _LANDLOCK_READ_ONLY,
        )
        assert _LANDLOCK_READ_ONLY == (
            _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR
        )
        # Critically: READ_ONLY must NOT include exec or write.
        assert not (_LANDLOCK_READ_ONLY & _LANDLOCK_ACCESS_FS_EXECUTE), \
            "F-107 regression: READ_ONLY grants EXECUTE"
        assert not (_LANDLOCK_READ_ONLY & _LANDLOCK_ACCESS_FS_WRITE_FILE), \
            "F-107 regression: READ_ONLY grants WRITE_FILE"

    def test_read_exec_includes_execute(self):
        """READ_EXEC must include the EXECUTE bit — that's the whole
        point of the /usr/bin allowlist for input_synth."""
        from gui_agent.sandbox import (
            _LANDLOCK_ACCESS_FS_EXECUTE,
            _LANDLOCK_READ_EXEC,
        )
        assert _LANDLOCK_READ_EXEC & _LANDLOCK_ACCESS_FS_EXECUTE, \
            "F-103 regression: READ_EXEC does not grant EXECUTE"

    def test_read_write_excludes_execute(self):
        """READ_WRITE (scratch dir, XDG runtime) must NOT include
        EXECUTE — an attacker dropping a binary in /tmp/icebreaker-gui/
        should not be able to run it."""
        from gui_agent.sandbox import (
            _LANDLOCK_ACCESS_FS_EXECUTE,
            _LANDLOCK_READ_WRITE,
        )
        assert not (_LANDLOCK_READ_WRITE & _LANDLOCK_ACCESS_FS_EXECUTE), \
            "F-103 regression: READ_WRITE grants EXECUTE (scratch " \
            "dir would become exec-eligible)"

    def test_handled_mask_includes_execute(self):
        """handled_access_fs MUST include EXECUTE so Landlock actually
        gates exec globally (paths without READ_EXEC are denied exec)."""
        from gui_agent.sandbox import (
            _LANDLOCK_ACCESS_FS_EXECUTE,
            _LANDLOCK_HANDLED,
        )
        assert _LANDLOCK_HANDLED & _LANDLOCK_ACCESS_FS_EXECUTE

    def test_handled_mask_covers_all_access_classes(self):
        """F-107: handled_access_fs must cover EVERY defined ABI v1
        bit. Any bit omitted becomes UNRESTRICTED globally — which is
        exactly the pre-V.6a bug (READ_FILE/READ_DIR/MAKE_DIR were
        silently unrestricted because they weren't in the mask)."""
        from gui_agent.sandbox import _LANDLOCK_HANDLED
        for name, bit in _KERNEL_CANONICAL_BITS.items():
            assert _LANDLOCK_HANDLED & bit, (
                f"handled_access_fs missing {name} (bit {bit.bit_length()-1}). "
                f"F-107 says the sandbox will silently unrestrict {name}."
            )


class TestRpaBridgeLandlockBits:
    """Same fix must apply to rpa_bridge/sandbox.py — the bug was
    copy-pasted there."""

    @pytest.mark.parametrize("name,expected", _KERNEL_CANONICAL_BITS.items())
    def test_constant_matches_kernel_value(self, name: str, expected: int) -> None:
        import rpa_bridge.sandbox as mod
        attr_name = f"_LANDLOCK_ACCESS_FS_{name}"
        assert hasattr(mod, attr_name), (
            f"{attr_name} missing from rpa_bridge.sandbox"
        )
        actual = getattr(mod, attr_name)
        assert actual == expected, (
            f"{attr_name} = {actual} but kernel UAPI says {expected}. "
            f"F-107 regression in rpa_bridge."
        )


class TestNoRawWrongBitValues:
    """Belt-and-braces guard: grep the source files for any bare
    `1 << 5`, `1 << 9`, `1 << 11`, `1 << 12` — the pre-F-107 wrong
    values. If any survive a future refactor, this test catches it."""

    @pytest.mark.parametrize("mod_name,filename", [
        ("gui_agent.sandbox", "gui_agent/sandbox.py"),
        ("rpa_bridge.sandbox", "rpa_bridge/sandbox.py"),
    ])
    def test_no_bare_wrong_shifts_in_landlock_context(
        self, mod_name: str, filename: str
    ) -> None:
        """The pre-F-107 code had `_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 5`
        which is wrong (WRITE_FILE=1<<1, REMOVE_FILE=1<<5 in the kernel).
        Ensure no _LANDLOCK_ACCESS_FS_* line uses a bare wrong shift."""
        import importlib
        mod = importlib.import_module(mod_name)
        with open(mod.__file__) as fh:
            src = fh.read()
        # If the WRITE_FILE constant appears with the old wrong value on
        # the same line, that's a regression.
        for wrong_line_marker in (
            "_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 5",
            "_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 0",
            "_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 1",
            "_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 9",
            "_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 11",
            "_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 12",
        ):
            assert wrong_line_marker not in src, (
                f"F-107 regression in {filename}: found "
                f"{wrong_line_marker!r} — that's the pre-fix wrong value."
            )
