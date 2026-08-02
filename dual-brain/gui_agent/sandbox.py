"""Landlock + Seccomp sandbox for the GUI Agent process.

F-51 marker: F-107-landlock-bits (per incremental/GROUND_TRUTH.md F-107).

Applied BEFORE any D-Bus connection (INV-5). On failure, the process
exits — never runs unsandboxed.

Landlock (kernel 5.13+, ABI v1):
  - ``$HOME`` read-only
  - ``/tmp/icebreaker-gui/`` read-write (scratch dir for screenshots)
  - ``$XDG_RUNTIME_DIR`` read-write (Wayland/D-Bus sockets)
  - ``/usr``, ``/lib``, ``/lib64`` read + EXECUTE (dynamic linker +
    Fix V input-synth helpers: xdotool, notify-send, xrandr)
  - ``/etc`` read-only (X11 configs, mime, fonts, timezone, trust
    store defaults under /etc/icebreaker/gui_trust.d/)
  - ``/var/lib/icebreaker`` read-only (trust store JSONL)
  - ``/opt/icebreaker`` read + EXECUTE (Python venv includes .so
    files that need PROT_EXEC mmap)

Seccomp:
  - Allow ``socket(AF_UNIX)``, ``connect``, ``sendmsg``, ``recvmsg``
  - Allow standard I/O, memory, and signal syscalls
  - Allow ``execve`` — exec is now GATED by Landlock (only paths with
    EXECUTE bit in the ruleset can be run). See F-107 comment below.
  - Deny ``execveat`` (exec-by-fd escape vector — see F-107)
  - Deny ``socket(AF_INET)``, ``socket(AF_INET6)`` (no network)
  - Default: ``EPERM``

**F-107 (2026-08-02) — pre-existing Landlock bit-value bug fixed here.**
Constants at lines below now match kernel UAPI ``include/uapi/linux/
landlock.h`` (verified against upstream). Prior code used wrong bit
values (``READ_FILE = 1<<0`` when kernel says ``EXECUTE = 1<<0``,
etc), which made the sandbox miswire what it was actually controlling.
The pre-fix sandbox happened to control EXECUTE globally (bit 0 was
in ``handled_access_fs`` under the wrong name); post-fix it correctly
controls READ + WRITE + EXECUTE + directory ops. Per-path rules now
express intent accurately (``$HOME`` really is read-only, not
execute-and-write).

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


# F-107 (Fix V.6a, 2026-08-02) — Landlock ABI v1 bit values from kernel
# UAPI ``include/uapi/linux/landlock.h``, verified against upstream at
# https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
# Prior values in this file (READ_FILE=1<<0 etc) were WRONG since day
# one — see F-107 in GROUND_TRUTH.md. Regression guard:
# gui_agent/tests/test_sandbox_landlock_bits.py asserts each constant
# equals the kernel canonical value.
_LANDLOCK_ACCESS_FS_EXECUTE      = 1 << 0
_LANDLOCK_ACCESS_FS_WRITE_FILE   = 1 << 1
_LANDLOCK_ACCESS_FS_READ_FILE    = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR     = 1 << 3
_LANDLOCK_ACCESS_FS_REMOVE_DIR   = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE  = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR    = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR     = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG     = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK    = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO    = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK   = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM     = 1 << 12

# Composite access sets — semantic groupings the per-path rules use.
_LANDLOCK_READ_ONLY = (
    _LANDLOCK_ACCESS_FS_READ_FILE
    | _LANDLOCK_ACCESS_FS_READ_DIR
)
# READ_EXEC: for /usr, /lib, /lib64, /opt/icebreaker — the paths that
# ship binaries and their dynamic-linker dependencies. Grants EXECUTE
# (bit 0) which is what Fix V.6a needs so input_synth can spawn
# /usr/bin/xdotool + /usr/bin/notify-send + /usr/bin/xrandr.
_LANDLOCK_READ_EXEC = (
    _LANDLOCK_ACCESS_FS_READ_FILE
    | _LANDLOCK_ACCESS_FS_READ_DIR
    | _LANDLOCK_ACCESS_FS_EXECUTE
)
# READ_WRITE: for scratch + XDG runtime. Deliberately NO exec bit —
# an attacker who drops a binary in scratch cannot then run it.
_LANDLOCK_READ_WRITE = (
    _LANDLOCK_ACCESS_FS_READ_FILE
    | _LANDLOCK_ACCESS_FS_READ_DIR
    | _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
)
# HANDLED: the superset that goes into landlock_ruleset_attr.
# handled_access_fs. Every access class here becomes DENIED for paths
# NOT covered by an explicit ALLOW rule (path-beneath rule with that
# bit in allowed_access). Access classes NOT in this mask are
# UNRESTRICTED globally — so what's here defines the sandbox's
# actual reach.
_LANDLOCK_HANDLED = (
    _LANDLOCK_ACCESS_FS_EXECUTE
    | _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_READ_FILE
    | _LANDLOCK_ACCESS_FS_READ_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_CHAR
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_SOCK
    | _LANDLOCK_ACCESS_FS_MAKE_FIFO
    | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | _LANDLOCK_ACCESS_FS_MAKE_SYM
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
    """Probe for Landlock ABI v1 support.

    See ``rpa_bridge.sandbox._check_landlock_available`` — same rationale.
    Silent swallow (F-53 Scope A.P3) is intentional because the caller
    raises ``SandboxError`` on False and refusing sandbox setup is the
    safe outcome under INV-5.
    """
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
    except Exception:  # noqa: BLE001
        # Intentional swallow — see docstring.
        return False


def _apply_landlock(home_dir: str, scratch_dir: str) -> None:
    """Apply Landlock filesystem restrictions.

    F-107 (Fix V.6a): handled_access_fs now uses the corrected
    ``_LANDLOCK_HANDLED`` mask covering every filesystem access class
    (execute, read, write, dir ops, mknod variants). Prior code used a
    wrong-bit READ_WRITE mask that happened to control a subset of
    accesses under mislabelled names — meaning READ_FILE / READ_DIR /
    MAKE_DIR / MAKE_CHAR were silently UNRESTRICTED globally. Post-fix
    the sandbox actually enforces its stated posture.
    """
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    attr = _LandlockRulesetAttr(
        handled_access_fs=_LANDLOCK_HANDLED,
        handled_access_net=0,
    )

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

    def _add_if_exists(path: str, access: int) -> None:
        if os.path.isdir(path):
            _add_path_rule(path, access)

    # ── Existing paths (semantics NOW correct post-F-107) ──
    _add_path_rule(home_dir, _LANDLOCK_READ_ONLY)
    _add_path_rule(scratch_dir, _LANDLOCK_READ_WRITE)

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    _add_if_exists(xdg_runtime, _LANDLOCK_READ_WRITE)

    # ── System dirs (READ_EXEC — binaries + libs the subprocess and
    # ── its exec'd children need). F-107 + F-103 (Fix V.6a):
    # /usr covers /usr/bin (xdotool, notify-send, xrandr), /usr/lib
    # (libc, libX11, libatspi, glib), /usr/share (fonts, mime, icons,
    # X11 config). /lib and /lib64 are 32-bit and legacy paths;
    # add-if-exists so operator machines (macOS) don't crash importing
    # this module.
    _add_if_exists("/usr", _LANDLOCK_READ_EXEC)
    _add_if_exists("/lib", _LANDLOCK_READ_EXEC)
    _add_if_exists("/lib64", _LANDLOCK_READ_EXEC)

    # /opt/icebreaker: Python venv site-packages includes compiled
    # .so extensions (jsonschema, Pillow, numpy) that need PROT_EXEC.
    _add_if_exists("/opt/icebreaker", _LANDLOCK_READ_EXEC)

    # /etc (read-only): X11 config, mime, fonts, timezone data, and
    # the trust store defaults under /etc/icebreaker/gui_trust.d/
    # (loaded by V.3b trust_store.TrustStore._load).
    _add_if_exists("/etc", _LANDLOCK_READ_ONLY)

    # /var/lib/icebreaker (read-only from agent's POV): trust store
    # JSONL. Writes come from the controller + `ib-trust` CLI, not
    # from within the sandboxed agent subprocess.
    _add_if_exists("/var/lib/icebreaker", _LANDLOCK_READ_ONLY)

    # /proc, /sys, /dev (read-only): xdotool + X11 libs read /proc/self,
    # /sys/class/input, /dev/urandom, etc. Narrower rules would need
    # per-file discovery + brittle maintenance; /proc + /sys are
    # read-only anyway from userspace, and /dev exposure is bounded by
    # DAC (owner/group).
    _add_if_exists("/proc", _LANDLOCK_READ_ONLY)
    _add_if_exists("/sys", _LANDLOCK_READ_ONLY)
    _add_if_exists("/dev", _LANDLOCK_READ_ONLY)

    # /run (read-only): systemd runtime state (/run/systemd/*), Wayland
    # display socket path (/run/user/... is XDG_RUNTIME_DIR handled
    # above, but /run itself needs read for other socket lookups).
    _add_if_exists("/run", _LANDLOCK_READ_ONLY)

    # /tmp (read-only): xdotool + Xlib may consult /tmp/.X11-unix for
    # the X server socket. Deliberately NOT read-write — attacker
    # shouldn't be able to drop payloads outside scratch dir.
    _add_if_exists("/tmp", _LANDLOCK_READ_ONLY)

    # V.6g (2026-08-02) — CT-scan R-1 remediation. Paths that Fix V
    # subprocess + xdotool+Xlib+libraries need at runtime but the
    # V.6a ruleset missed:
    #
    # /usr/local (READ_EXEC): user-installed libs + apps. Ubuntu 24.04
    # doesn't put system stuff here by default, but pip --user
    # installs land in ~/.local, and any operator-installed helper
    # (e.g. custom xdotool build) lives under /usr/local/bin. Missing
    # → EACCES on those libs' opens.
    _add_if_exists("/usr/local", _LANDLOCK_READ_EXEC)

    # /dev/shm (READ_WRITE): POSIX shared memory. Pillow allocates
    # tempfile-backed shm segments for image mmaps; numpy uses shm
    # for large arrays; python multiprocessing (if any dep uses it)
    # needs /dev/shm/*. /dev itself is READ_ONLY above — this rule
    # OVERLAPS + broadens to RW just for /dev/shm. Landlock allows
    # this: more specific paths compose additively with the parent.
    _add_if_exists("/dev/shm", _LANDLOCK_READ_WRITE)

    # $HOME/.cache (READ_WRITE): Python packages cache aggressively —
    # pip cache, matplotlib font cache, litellm model catalog cache,
    # huggingface cache. $HOME above is READ_ONLY — this override
    # opens .cache for writes. Same additive-composition pattern as
    # /dev/shm.
    home_cache = os.path.join(home_dir, ".cache")
    if not os.path.isdir(home_cache):
        # First run may not have created it yet — mkdir so Landlock
        # can add the path. mode=0o700 to match XDG spec.
        try:
            os.makedirs(home_cache, mode=0o700, exist_ok=True)
        except OSError:
            pass
    _add_if_exists(home_cache, _LANDLOCK_READ_WRITE)

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

    # F-53 Scope A.P3 (security-critical). See rpa_bridge/sandbox.py for
    # full rationale. ALLOW-rule failure: over-restriction that yields
    # cryptic child breakage — refuse spawn. DENY / arg-filtered ALLOW
    # failure: policy diverges from what INV-5 requires — refuse spawn.
    for name in safe_syscalls:
        try:
            f.add_rule(seccomp.ALLOW, name)
        except Exception as exc:
            raise SandboxError(
                f"seccomp ALLOW rule for '{name}' failed "
                f"({type(exc).__name__}: {exc}). Refusing to spawn "
                "GUI Agent child under an incomplete filter — INV-5."
            ) from exc

    # socket(domain, ...) — allow only AF_UNIX and AF_NETLINK (arg0 filter).
    # All other domains (AF_INET=2, AF_INET6=10, ...) hit the default EPERM.
    for allowed_af in (_AF_UNIX, _AF_NETLINK):
        try:
            f.add_rule(
                seccomp.ALLOW, "socket",
                seccomp.Arg(0, seccomp.EQ, allowed_af),
            )
        except Exception as exc:
            raise SandboxError(
                f"seccomp arg-filtered ALLOW for socket AF={allowed_af} "
                f"failed ({type(exc).__name__}: {exc}). D-Bus / UNIX "
                "socket policy incomplete — refusing to spawn. INV-5."
            ) from exc

    # F-103 (Fix V.6a, 2026-08-02): execve is now ALLOWED at seccomp
    # level because Fix V input_synth must spawn /usr/bin/xdotool
    # (mouse+kb synthesis), /usr/bin/notify-send (annotated preview
    # toast), and /usr/bin/xrandr (HiDPI/monitor layout).
    #
    # This is NOT a security downgrade — exec is now GATED by Landlock:
    # only paths in the ruleset with LANDLOCK_ACCESS_FS_EXECUTE (via
    # _LANDLOCK_READ_EXEC) can be run. That's /usr, /lib, /lib64,
    # /opt/icebreaker — all stock system dirs owned by root. The
    # scratch dir + $HOME are DENIED exec (per _LANDLOCK_READ_WRITE
    # and _LANDLOCK_READ_ONLY neither including EXECUTE), so an
    # attacker who drops a binary in /tmp/icebreaker-gui/ cannot run it.
    #
    # execveat REMAINS DENIED — it's the exec-by-fd variant (open a
    # file with O_PATH, then execveat(fd, "", AT_EMPTY_PATH)) which
    # is a well-known way to bypass path-based exec allowlists. See
    # https://man7.org/linux/man-pages/man2/execveat.2.html
    #
    # See the module docstring's F-107 note and GROUND_TRUTH.md F-103
    # for the full threat-model shift.
    try:
        f.add_rule(seccomp.ALLOW, "execve")
    except Exception as exc:
        raise SandboxError(
            f"seccomp ALLOW rule for 'execve' failed "
            f"({type(exc).__name__}: {exc}). Vision-actuation helpers "
            "(xdotool/notify-send/xrandr) cannot spawn — refusing to "
            "launch GUI Agent under an incomplete filter. INV-5."
        ) from exc

    try:
        f.add_rule(seccomp.ERRNO(1), "execveat")
    except Exception as exc:
        raise SandboxError(
            f"seccomp DENY rule for 'execveat' failed "
            f"({type(exc).__name__}: {exc}). Sandbox cannot block "
            "exec-by-fd escape variant — refusing to spawn GUI Agent. "
            "INV-5."
        ) from exc

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
