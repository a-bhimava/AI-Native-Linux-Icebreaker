"""Landlock + Seccomp sandbox for the GUI Agent process.

Applied BEFORE any D-Bus connection (INV-5). On failure, the process
exits — never runs unsandboxed.

Landlock (kernel 5.13+, ABI v1):
  - ``$HOME`` read-only
  - ``/tmp/icebreaker-gui/`` read-write (scratch dir for screenshots)
  - D-Bus socket paths readable

Seccomp:
  - Allow ``socket(AF_UNIX)``, ``connect``, ``sendmsg``, ``recvmsg``
  - Allow standard I/O, memory, and signal syscalls
  - Deny ``execve``, ``execveat`` (no arbitrary command execution)
  - Deny ``socket(AF_INET)``, ``socket(AF_INET6)`` (no network)
  - Default: ``EPERM``

Platform: Linux only. Raises ``SandboxError`` on other platforms.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import socket as _socket_mod
import sys
from pathlib import Path


class SandboxError(Exception):
    """Raised when sandbox setup fails. The agent must not proceed."""


_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 0
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 1
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 9
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 11
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 12

_LANDLOCK_READ_ONLY = _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR
_LANDLOCK_READ_WRITE = (
    _LANDLOCK_ACCESS_FS_READ_FILE
    | _LANDLOCK_ACCESS_FS_READ_DIR
    | _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
)

_SYS_landlock_create_ruleset = 444
_SYS_landlock_add_rule = 445
_SYS_landlock_restrict_self = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int),
    ]


def _check_landlock_available() -> bool:
    """Probe for Landlock ABI v1 support."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        result = libc.syscall(
            _SYS_landlock_create_ruleset,
            None, 0,
            _LANDLOCK_CREATE_RULESET_VERSION,
        )
        if result >= 1:
            return True
        return ctypes.get_errno() != 38  # ENOSYS
    except Exception:
        return False


def _apply_landlock(home_dir: str, scratch_dir: str) -> None:
    """Apply Landlock filesystem restrictions."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    all_access = _LANDLOCK_READ_WRITE
    attr = _LandlockRulesetAttr(handled_access_fs=all_access, handled_access_net=0)

    ruleset_fd = libc.syscall(
        _SYS_landlock_create_ruleset,
        ctypes.byref(attr), ctypes.sizeof(attr), 0,
    )
    if ruleset_fd < 0:
        errno = ctypes.get_errno()
        raise SandboxError(f"landlock_create_ruleset failed: errno {errno}")

    def _add_path_rule(path: str, access: int) -> None:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = _LandlockPathBeneathAttr(allowed_access=access, parent_fd=fd)
            ret = libc.syscall(
                _SYS_landlock_add_rule,
                ruleset_fd,
                _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(rule), 0,
            )
            if ret < 0:
                errno = ctypes.get_errno()
                raise SandboxError(
                    f"landlock_add_rule for {path!r} failed: errno {errno}"
                )
        finally:
            os.close(fd)

    _add_path_rule(home_dir, _LANDLOCK_READ_ONLY)
    _add_path_rule(scratch_dir, _LANDLOCK_READ_WRITE)

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if os.path.isdir(xdg_runtime):
        _add_path_rule(xdg_runtime, _LANDLOCK_READ_WRITE)

    ret = libc.syscall(_SYS_landlock_restrict_self, ruleset_fd, 0)
    os.close(ruleset_fd)
    if ret < 0:
        errno = ctypes.get_errno()
        raise SandboxError(f"landlock_restrict_self failed: errno {errno}")


_AF_UNIX = _socket_mod.AF_UNIX                              # 1
_AF_NETLINK = getattr(_socket_mod, "AF_NETLINK", 16)        # 16 — Linux-only; D-Bus needs it


def _apply_seccomp() -> None:
    """Apply Seccomp-BPF filter denying execve and network sockets.

    Socket filtering: only AF_UNIX and AF_NETLINK are permitted (D-Bus
    needs both). AF_INET and AF_INET6 are denied by the default EPERM
    action because ``socket`` is NOT in the blanket allow list — it gets
    two arg-filtered ALLOW rules instead.
    """
    try:
        import seccomp
    except ImportError:
        raise SandboxError(
            "seccomp Python package required for GUI Agent sandbox. "
            "Install with: pip install seccomp"
        )

    f = seccomp.SyscallFilter(seccomp.ERRNO(1))  # EPERM default

    safe_syscalls = [
        "read", "write", "open", "openat", "close", "fstat", "stat", "lstat",
        "poll", "lseek", "mmap", "mprotect", "munmap", "brk",
        "rt_sigaction", "rt_sigprocmask", "rt_sigreturn",
        "ioctl", "access", "pipe", "select", "sched_yield",
        "mremap", "msync", "madvise", "dup", "dup2", "dup3",
        "nanosleep", "clock_gettime", "clock_nanosleep",
        "getpid", "getuid", "getgid", "geteuid", "getegid", "gettid",
        # "socket" intentionally NOT here — arg-filtered below
        "connect", "sendmsg", "recvmsg", "sendto", "recvfrom",
        "bind", "listen", "accept", "accept4",
        "setsockopt", "getsockopt", "getsockname", "getpeername",
        "shutdown",
        "clone", "clone3", "futex", "set_robust_list", "get_robust_list",
        "exit", "exit_group",
        "fcntl", "flock", "fsync", "fdatasync",
        "getcwd", "readlink", "readlinkat",
        "getdents", "getdents64",
        "mkdir", "mkdirat", "unlink", "unlinkat", "rename", "renameat", "renameat2",
        "chmod", "fchmod", "fchmodat",
        "umask", "pipe2", "eventfd2", "epoll_create1", "epoll_ctl", "epoll_wait",
        "newfstatat", "statx",
        "getrandom", "memfd_create",
        "pread64", "pwrite64", "writev", "readv",
        "set_tid_address", "arch_prctl", "prctl",
        "rseq", "prlimit64",
    ]

    for name in safe_syscalls:
        try:
            f.add_rule(seccomp.ALLOW, name)
        except Exception:
            pass

    # socket(domain, ...) — allow only AF_UNIX and AF_NETLINK (arg0 filter).
    # All other domains (AF_INET=2, AF_INET6=10, ...) hit the default EPERM.
    for allowed_af in (_AF_UNIX, _AF_NETLINK):
        try:
            f.add_rule(
                seccomp.ALLOW, "socket",
                seccomp.Arg(0, seccomp.EQ, allowed_af),
            )
        except Exception:
            pass

    for deny_name in ("execve", "execveat"):
        try:
            f.add_rule(seccomp.ERRNO(1), deny_name)
        except Exception:
            pass

    f.load()


def apply_gui_sandbox(home_dir: str, scratch_dir: str) -> None:
    """Apply Landlock + Seccomp sandbox. Must be called BEFORE any D-Bus connection.

    Raises ``SandboxError`` on non-Linux or if sandbox setup fails.
    """
    if sys.platform != "linux":
        raise SandboxError(
            "GUI Agent requires Linux (Landlock kernel 5.13+). "
            f"Current platform: {sys.platform}"
        )

    if not _check_landlock_available():
        raise SandboxError(
            "Landlock not available (requires kernel 5.13+). "
            "GUI Agent cannot run without filesystem sandboxing (INV-5)."
        )

    os.makedirs(scratch_dir, mode=0o700, exist_ok=True)
    _apply_landlock(home_dir, scratch_dir)
    _apply_seccomp()
