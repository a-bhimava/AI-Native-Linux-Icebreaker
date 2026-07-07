/// seccomp.rs — Seccomp-BPF syscall sandbox (INV-5).
///
/// Layer 3 of the sandbox stack, after `fs::validate()` (M1.2) and Landlock
/// (M1.3). The kernel filters every syscall mcpd attempts; the most
/// restrictive of the installed filters wins.
///
/// **Two filters are installed:**
///
/// 1. **Denylist** (mismatch=Allow, match=KillProcess).
///    Catches a hand-curated set that has no legitimate use in a stdio
///    JSON-RPC daemon (mount, reboot, kexec, bpf, ptrace, setuid, unshare…).
///    Acts as a hard floor: even if the allowlist below ever drifts open,
///    these still die.
///
/// 2. **Allowlist** (mismatch=KillProcess by default, match=Allow).
///    The positive surface — every syscall mcpd actually issues during
///    normal operation. Anything outside this list kills the process with
///    SIGSYS and the kernel audit log records the syscall number.
///
/// The two together encode "deny these, then strictly allow only those, kill
/// the rest." Denied syscalls still die even if accidentally listed in the
/// allowlist; unknown syscalls die unless they're in the allowlist.
///
/// **Harvest mode** (`MCPD_SECCOMP_LOG_ONLY=1`): allowlist mismatch becomes
/// `SeccompAction::Log` instead of `KillProcess`. Unknown syscalls execute
/// and get logged to `audit: type=1326 ... syscall=N`. Used to discover the
/// missing syscalls for the allowlist; never set in production.
///
/// `PR_SET_NO_NEW_PRIVS` is set before the filters so they cannot be dropped
/// via setuid. Both filters are applied in `main.rs` immediately after
/// Landlock and before the server accepts requests.
use anyhow::{bail, Result};
use seccompiler::{BpfProgram, SeccompAction, SeccompFilter, TargetArch};
use std::collections::BTreeMap;
use std::convert::TryInto;
use tracing::{info, warn};

pub fn apply() -> Result<()> {
    set_no_new_privs()?;

    let arch = target_arch()?;
    let harvest = std::env::var("MCPD_SECCOMP_LOG_ONLY").is_ok();

    install_denylist_filter(arch)?;
    install_allowlist_filter(arch, harvest)?;

    if harvest {
        warn!("seccomp: HARVEST MODE — unknown syscalls Log+Allow. NOT for production.");
    } else {
        info!(
            "seccomp: installed denylist ({}) + allowlist ({}); default=KillProcess",
            denied_syscalls().len(),
            allowed_syscalls().len()
        );
    }
    Ok(())
}

// ── Denylist filter ──────────────────────────────────────────────────────────

fn install_denylist_filter(arch: TargetArch) -> Result<()> {
    let rules: BTreeMap<i64, Vec<seccompiler::SeccompRule>> = denied_syscalls()
        .into_iter()
        .map(|nr| (nr, Vec::new()))
        .collect();
    let filter = SeccompFilter::new(
        rules,
        SeccompAction::Allow,        // mismatch — let the allowlist filter decide
        SeccompAction::KillProcess,  // match — denied syscalls always die
        arch,
    )
    .map_err(|e| anyhow::anyhow!("seccomp denylist build failed: {}", e))?;
    let program: BpfProgram = filter
        .try_into()
        .map_err(|e| anyhow::anyhow!("seccomp denylist BPF compile failed: {}", e))?;
    seccompiler::apply_filter(&program)
        .map_err(|e| anyhow::anyhow!("seccomp denylist apply failed: {}", e))?;
    Ok(())
}

// ── Allowlist filter ─────────────────────────────────────────────────────────

fn install_allowlist_filter(arch: TargetArch, harvest: bool) -> Result<()> {
    let rules: BTreeMap<i64, Vec<seccompiler::SeccompRule>> = allowed_syscalls()
        .into_iter()
        .map(|nr| (nr, Vec::new()))
        .collect();
    let mismatch = if harvest {
        SeccompAction::Log
    } else {
        SeccompAction::KillProcess
    };
    let filter = SeccompFilter::new(
        rules,
        mismatch,
        SeccompAction::Allow,
        arch,
    )
    .map_err(|e| anyhow::anyhow!("seccomp allowlist build failed: {}", e))?;
    let program: BpfProgram = filter
        .try_into()
        .map_err(|e| anyhow::anyhow!("seccomp allowlist BPF compile failed: {}", e))?;
    seccompiler::apply_filter(&program)
        .map_err(|e| anyhow::anyhow!("seccomp allowlist apply failed: {}", e))?;
    Ok(())
}

fn set_no_new_privs() -> Result<()> {
    let ret = unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
    if ret < 0 {
        bail!(
            "PR_SET_NO_NEW_PRIVS failed: {}",
            std::io::Error::last_os_error()
        );
    }
    Ok(())
}

fn target_arch() -> Result<TargetArch> {
    #[cfg(target_arch = "x86_64")]
    return Ok(TargetArch::x86_64);
    #[cfg(target_arch = "aarch64")]
    return Ok(TargetArch::aarch64);
    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    bail!("seccomp: unsupported target architecture")
}

// ── Denied syscalls ──────────────────────────────────────────────────────────

/// Hand-curated set of syscalls that mcpd has no legitimate reason to call.
/// Independent of the allowlist below — denied here means denied even if the
/// allowlist ever drifts open.
pub(crate) fn denied_syscalls() -> Vec<i64> {
    vec![
        // Filesystem-level admin
        libc::SYS_mount,
        libc::SYS_umount2,
        libc::SYS_pivot_root,
        libc::SYS_chroot,
        libc::SYS_swapon,
        libc::SYS_swapoff,
        // Kernel/system control
        libc::SYS_reboot,
        libc::SYS_kexec_load,
        libc::SYS_init_module,
        libc::SYS_finit_module,
        libc::SYS_delete_module,
        // Cross-process introspection / control
        libc::SYS_ptrace,
        libc::SYS_process_vm_readv,
        libc::SYS_process_vm_writev,
        // eBPF / kernel programmability
        libc::SYS_bpf,
        libc::SYS_perf_event_open,
        // Namespace manipulation
        libc::SYS_unshare,
        libc::SYS_setns,
        // UID/GID changes (mcpd runs as a non-root user; never lift)
        libc::SYS_setuid,
        libc::SYS_setgid,
        libc::SYS_setreuid,
        libc::SYS_setregid,
        libc::SYS_setresuid,
        libc::SYS_setresgid,
        libc::SYS_setfsuid,
        libc::SYS_setfsgid,
        // Capability set changes
        libc::SYS_capset,
        // Listening sockets (TCP/UDP/UNIX server). `bind()` is NOT here:
        // getifaddrs(3) binds an AF_NETLINK socket to query routing — that's
        // a query primitive, not a listener. The "no network listener" gate
        // (INV-3) is enforced by `ss -tlnp` in ci.sh G3. listen/accept turn
        // a bound socket into a server, which mcpd never needs.
        libc::SYS_listen,
        libc::SYS_accept,
        libc::SYS_accept4,
    ]
}

// ── Allowed syscalls ─────────────────────────────────────────────────────────

/// The positive surface of syscalls mcpd issues during normal operation.
/// Seeded from observation of the binary on Ubuntu 22.04 / kernel 6.1 with
/// tokio + zbus + jsonschema + landlock + uuid.
///
/// **Discovery procedure** when a new tokio/glibc/dep version changes the
/// syscall surface:
///   1. `export MCPD_SECCOMP_LOG_ONLY=1`
///   2. `cargo test --release` (or run a representative workload)
///   3. `sudo dmesg | grep -E 'mcpd.*syscall=' | grep -oE 'syscall=[0-9]+' | sort -u`
///   4. Decode the new numbers with `ausyscall <N>` (apt install auditd)
///   5. Add the libc::SYS_* constants to this list
///   6. Unset the env var; rebuild; the next run should be clean.
pub(crate) fn allowed_syscalls() -> Vec<i64> {
    let mut v: Vec<i64> = vec![
        // ── I/O ────────────────────────────────────────────────────────────
        libc::SYS_read,
        libc::SYS_write,
        libc::SYS_pread64,
        libc::SYS_pwrite64,
        libc::SYS_readv,
        libc::SYS_writev,
        libc::SYS_fsync,            // F-33: safe_write / canonicalize_write call file.sync_all()
        libc::SYS_fdatasync,        // sibling of fsync — data-only sync sibling
        libc::SYS_openat,
        libc::SYS_openat2,
        libc::SYS_close,
        libc::SYS_close_range,
        libc::SYS_lseek,
        libc::SYS_getdents64,
        libc::SYS_fcntl,
        libc::SYS_ioctl,
        libc::SYS_dup,
        libc::SYS_dup2,             // older glibc fd-dup fallback (== dup3 w/o flags)
        libc::SYS_dup3,
        // ── Stat / link / dir creation ────────────────────────────────────
        libc::SYS_fstat,
        libc::SYS_newfstatat,
        libc::SYS_statfs,
        libc::SYS_fstatfs,
        libc::SYS_statx,
        libc::SYS_getcwd,
        libc::SYS_readlink,         // F-29 fallback: std::fs::canonicalize
        libc::SYS_readlinkat,
        libc::SYS_mkdir,            // audit log dir creation (older path)
        libc::SYS_mkdirat,          // audit log dir creation (modern path)
        // ── Memory ────────────────────────────────────────────────────────
        libc::SYS_mmap,
        libc::SYS_munmap,
        libc::SYS_mprotect,
        libc::SYS_brk,
        libc::SYS_mremap,
        libc::SYS_madvise,
        // ── Signals ───────────────────────────────────────────────────────
        libc::SYS_rt_sigaction,
        libc::SYS_rt_sigprocmask,
        libc::SYS_rt_sigreturn,
        libc::SYS_sigaltstack,
        libc::SYS_rt_sigtimedwait,
        // ── Time ──────────────────────────────────────────────────────────
        libc::SYS_clock_gettime,
        libc::SYS_clock_getres,
        libc::SYS_clock_nanosleep,
        libc::SYS_nanosleep,
        // ── Async / event loop (tokio epoll-based reactor) ────────────────
        libc::SYS_epoll_create1,
        libc::SYS_epoll_ctl,
        libc::SYS_epoll_wait,       // legacy epoll_wait — tokio reactor on glibc 2.36
        libc::SYS_epoll_pwait,
        libc::SYS_epoll_pwait2,
        libc::SYS_eventfd2,
        libc::SYS_pipe2,
        libc::SYS_poll,
        libc::SYS_ppoll,
        // ── Timers (tokio time::sleep, intervals) ─────────────────────────
        libc::SYS_timerfd_create,
        libc::SYS_timerfd_settime,
        libc::SYS_timerfd_gettime,
        // ── Threading / sync ──────────────────────────────────────────────
        libc::SYS_futex,
        libc::SYS_set_robust_list,
        libc::SYS_get_robust_list,
        libc::SYS_sched_yield,
        libc::SYS_sched_getaffinity,
        libc::SYS_sched_setaffinity,
        libc::SYS_set_tid_address,
        libc::SYS_clone,    // worker thread spawn (older glibc)
        libc::SYS_clone3,   // worker thread spawn (newer glibc)
        libc::SYS_rseq,     // restartable sequences (glibc thread init)
        // ── Process / ID / random ─────────────────────────────────────────
        libc::SYS_exit,
        libc::SYS_exit_group,
        libc::SYS_getpid,
        libc::SYS_gettid,
        libc::SYS_getppid,
        libc::SYS_getuid,
        libc::SYS_geteuid,
        libc::SYS_getgid,
        libc::SYS_getegid,
        libc::SYS_getrandom,
        libc::SYS_prctl,
        libc::SYS_prlimit64,
        libc::SYS_uname,
        libc::SYS_sysinfo,
        libc::SYS_wait4,
        libc::SYS_waitid,
        libc::SYS_kill,
        libc::SYS_tkill,
        libc::SYS_tgkill,
        libc::SYS_pidfd_open,
        libc::SYS_pidfd_send_signal,
        // ── Sockets for D-Bus AF_UNIX (M1.6) + netlink for getifaddrs ─────
        // listen/accept/accept4 are in the DENY list above — mcpd is a client
        // only (D-Bus, netlink queries). bind() is here because getifaddrs(3)
        // uses bind on AF_NETLINK to receive routing/interface replies.
        libc::SYS_socket,
        libc::SYS_socketpair,
        libc::SYS_bind,
        libc::SYS_connect,
        libc::SYS_sendto,
        libc::SYS_recvfrom,
        libc::SYS_sendmsg,
        libc::SYS_recvmsg,
        libc::SYS_sendmmsg,
        libc::SYS_recvmmsg,
        libc::SYS_shutdown,
        libc::SYS_setsockopt,
        libc::SYS_getsockopt,
        libc::SYS_getsockname,
        libc::SYS_getpeername,
        // ── Process exec (M1.6 spawns journalctl, M1.8 spawns dpkg-query) ─
        libc::SYS_execve,
        libc::SYS_execveat,
        libc::SYS_vfork,
        // ── Landlock (M1.3 installation) ──────────────────────────────────
        libc::SYS_landlock_create_ruleset,
        libc::SYS_landlock_add_rule,
        libc::SYS_landlock_restrict_self,
        // ── Seccomp itself (installing the second filter requires this) ───
        libc::SYS_seccomp,
    ];

    // x86_64-only syscalls
    #[cfg(target_arch = "x86_64")]
    {
        v.push(libc::SYS_arch_prctl);
    }

    v
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowlist_includes_critical_syscalls() {
        let a = allowed_syscalls();
        for nr in [
            libc::SYS_read,
            libc::SYS_write,
            libc::SYS_fsync,           // F-33: safe_write durability sync
            libc::SYS_openat2,
            libc::SYS_close,
            libc::SYS_futex,
            libc::SYS_epoll_wait,
            libc::SYS_epoll_pwait,
            libc::SYS_dup2,
            libc::SYS_dup3,
            libc::SYS_clone3,
            libc::SYS_rseq,
            libc::SYS_sched_getaffinity,
            libc::SYS_exit_group,
            libc::SYS_landlock_create_ruleset,
        ] {
            assert!(a.contains(&nr), "syscall {} must be allowed", nr);
        }
    }

    #[test]
    fn denylist_excludes_routine_syscalls() {
        let d = denied_syscalls();
        for nr in [
            libc::SYS_read,
            libc::SYS_write,
            libc::SYS_openat2,
            libc::SYS_clone3,
            libc::SYS_exit_group,
        ] {
            assert!(!d.contains(&nr), "syscall {} is routine; must NOT be denied", nr);
        }
    }

    #[test]
    fn denylist_covers_high_impact_syscalls() {
        let d = denied_syscalls();
        for nr in [
            libc::SYS_mount,
            libc::SYS_umount2,
            libc::SYS_reboot,
            libc::SYS_kexec_load,
            libc::SYS_init_module,
            libc::SYS_finit_module,
            libc::SYS_bpf,
            libc::SYS_ptrace,
            libc::SYS_setuid,
            libc::SYS_setgid,
            libc::SYS_unshare,
            libc::SYS_pivot_root,
            libc::SYS_setns,
            libc::SYS_capset,
            libc::SYS_listen,
            libc::SYS_accept,
        ] {
            assert!(d.contains(&nr), "syscall {} must be denied", nr);
        }
        // bind() is intentionally allowed (netlink for getifaddrs)
        assert!(!d.contains(&libc::SYS_bind), "bind must be allowed for netlink");
    }

    #[test]
    fn allowlist_and_denylist_dont_overlap() {
        let allowed: std::collections::HashSet<i64> = allowed_syscalls().into_iter().collect();
        let denied: std::collections::HashSet<i64> = denied_syscalls().into_iter().collect();
        let overlap: Vec<_> = allowed.intersection(&denied).collect();
        assert!(overlap.is_empty(), "overlap between allow and deny: {:?}", overlap);
    }
}
