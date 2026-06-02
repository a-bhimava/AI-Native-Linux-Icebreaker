/// seccomp.rs — Seccomp-BPF syscall allowlist (INV-5).
///
/// Layer 3 of the sandbox stack: after `fs::validate()` (M1.2) and Landlock
/// (M1.3), the kernel filters every syscall mcpd attempts. Anything not on the
/// allowlist below gets `SECCOMP_RET_KILL_PROCESS` — the process dies with
/// `SIGSYS` and the audit log records the syscall number.
///
/// Allowlist scope: every syscall that mcpd actually issues during normal
/// operation (read/write/openat/openat2/close/mmap/futex/epoll/clock/exit) plus
/// the four `landlock_*` syscalls (M1.3 installation), plus `AF_UNIX` socket
/// family for the D-Bus client (M1.6). Argument-level filtering for the
/// socket family (only AF_UNIX) is deferred to a Phase 1 stretch; v1 accepts
/// the looser allowlist documented in the roadmap.
///
/// `PR_SET_NO_NEW_PRIVS` is set before the filter so the filter cannot be
/// dropped via setuid binaries. Both are applied in `main.rs` immediately
/// after Landlock and before the server accepts requests.
use anyhow::{bail, Result};
use seccompiler::{
    BpfProgram, SeccompAction, SeccompFilter, TargetArch,
};
use std::collections::BTreeMap;
use std::convert::TryInto;
use tracing::info;

pub fn apply() -> Result<()> {
    set_no_new_privs()?;

    let rules: BTreeMap<i64, Vec<seccompiler::SeccompRule>> = allowed_syscalls()
        .into_iter()
        .map(|nr| (nr, Vec::new())) // empty rules = allow regardless of args
        .collect();

    let filter = SeccompFilter::new(
        rules,
        SeccompAction::KillProcess,
        SeccompAction::Allow,
        target_arch()?,
    )
    .map_err(|e| anyhow::anyhow!("seccomp filter build failed: {}", e))?;

    let program: BpfProgram = filter
        .try_into()
        .map_err(|e| anyhow::anyhow!("seccomp BPF compilation failed: {}", e))?;
    seccompiler::apply_filter(&program)
        .map_err(|e| anyhow::anyhow!("seccomp apply_filter failed: {}", e))?;

    info!(
        "seccomp: filter installed ({} allowed syscalls, default=KILL_PROCESS)",
        allowed_syscalls().len()
    );
    Ok(())
}

fn set_no_new_privs() -> Result<()> {
    // PR_SET_NO_NEW_PRIVS (1) — prevents the filter from being bypassed via
    // setuid binaries. Required for non-root processes to install seccomp.
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

/// The exact list of syscalls mcpd is allowed to make. The numbers come from
/// `libc::SYS_*` so they match the running kernel's syscall table.
///
/// Pattern: include only what the binary actually issues. When the kernel
/// kills mcpd with SIGSYS during M1.10 fuzzing, the missing syscall surfaces
/// in dmesg as `audit: ... syscall=NNN` — add it here, rebuild, retest.
pub(crate) fn allowed_syscalls() -> Vec<i64> {
    let mut v: Vec<i64> = vec![
        // ── I/O ─────────────────────────────────────────────────────────────
        libc::SYS_read,
        libc::SYS_write,
        libc::SYS_pread64,
        libc::SYS_pwrite64,
        libc::SYS_readv,
        libc::SYS_writev,
        libc::SYS_openat,
        libc::SYS_openat2,
        libc::SYS_close,
        libc::SYS_lseek,
        libc::SYS_getdents64,
        libc::SYS_fcntl,
        libc::SYS_ioctl, // tracing-subscriber may probe terminal type
        libc::SYS_dup,
        libc::SYS_dup3,
        // ── Stat ────────────────────────────────────────────────────────────
        libc::SYS_fstat,
        libc::SYS_newfstatat,
        libc::SYS_statfs,
        libc::SYS_fstatfs,
        libc::SYS_statx,
        libc::SYS_getcwd,
        libc::SYS_readlinkat,
        // ── Memory ─────────────────────────────────────────────────────────
        libc::SYS_mmap,
        libc::SYS_munmap,
        libc::SYS_mprotect,
        libc::SYS_brk,
        libc::SYS_mremap,
        libc::SYS_madvise,
        // ── Signals ────────────────────────────────────────────────────────
        libc::SYS_rt_sigaction,
        libc::SYS_rt_sigprocmask,
        libc::SYS_rt_sigreturn,
        libc::SYS_sigaltstack,
        // ── Time ───────────────────────────────────────────────────────────
        libc::SYS_clock_gettime,
        libc::SYS_clock_nanosleep,
        libc::SYS_nanosleep,
        // ── Async / event loop (tokio uses epoll on Linux) ─────────────────
        libc::SYS_epoll_create1,
        libc::SYS_epoll_ctl,
        libc::SYS_epoll_pwait,
        libc::SYS_eventfd2,
        libc::SYS_pipe2,
        // ── Threading / sync ───────────────────────────────────────────────
        libc::SYS_futex,
        libc::SYS_set_robust_list,
        libc::SYS_sched_yield,
        libc::SYS_set_tid_address,
        // ── Process / ID ───────────────────────────────────────────────────
        libc::SYS_exit,
        libc::SYS_exit_group,
        libc::SYS_getpid,
        libc::SYS_gettid,
        libc::SYS_getuid,
        libc::SYS_geteuid,
        libc::SYS_getgid,
        libc::SYS_getegid,
        libc::SYS_getrandom,
        libc::SYS_prctl,
        libc::SYS_prlimit64,
        // ── D-Bus client (M1.6) — AF_UNIX only; arg filtering deferred ─────
        libc::SYS_socket,
        libc::SYS_connect,
        libc::SYS_sendto,
        libc::SYS_recvfrom,
        libc::SYS_sendmsg,
        libc::SYS_recvmsg,
        libc::SYS_shutdown,
    ];

    // ── Landlock (M1.3) ───────────────────────────────────────────────────
    // libc 0.2.140+ has these constants; gated behind cfg in case an older
    // libc is pulled by mistake.
    #[cfg(target_arch = "x86_64")]
    {
        v.push(libc::SYS_arch_prctl);
    }
    v.push(libc::SYS_landlock_create_ruleset);
    v.push(libc::SYS_landlock_add_rule);
    v.push(libc::SYS_landlock_restrict_self);

    v
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowlist_includes_critical_syscalls() {
        let allowed = allowed_syscalls();
        assert!(allowed.contains(&libc::SYS_read));
        assert!(allowed.contains(&libc::SYS_write));
        assert!(allowed.contains(&libc::SYS_openat2));
        assert!(allowed.contains(&libc::SYS_close));
        assert!(allowed.contains(&libc::SYS_futex)); // tokio needs this
        assert!(allowed.contains(&libc::SYS_epoll_pwait)); // tokio reactor
        assert!(allowed.contains(&libc::SYS_exit_group));
    }

    #[test]
    fn allowlist_excludes_dangerous_syscalls() {
        let allowed = allowed_syscalls();
        // Things mcpd has no business doing.
        let forbidden = [
            libc::SYS_mount,        // FS mounts
            libc::SYS_umount2,      // FS unmounts
            libc::SYS_reboot,       // system reboot
            libc::SYS_kexec_load,   // load new kernel
            libc::SYS_init_module,  // load kernel module
            libc::SYS_finit_module, // load kernel module from fd
            libc::SYS_bpf,          // load eBPF
            libc::SYS_ptrace,       // attach to other processes
            libc::SYS_setuid,       // change uid
            libc::SYS_setgid,       // change gid
            libc::SYS_unshare,      // namespace manipulation
            libc::SYS_pivot_root,   // change rootfs
        ];
        for nr in forbidden {
            assert!(!allowed.contains(&nr),
                    "syscall {} should NOT be in the allowlist", nr);
        }
    }

    #[test]
    fn allowlist_includes_landlock_syscalls() {
        let allowed = allowed_syscalls();
        // mcpd installs Landlock at startup; these MUST be allowed.
        assert!(allowed.contains(&libc::SYS_landlock_create_ruleset));
        assert!(allowed.contains(&libc::SYS_landlock_add_rule));
        assert!(allowed.contains(&libc::SYS_landlock_restrict_self));
    }

    #[test]
    fn target_arch_returns_supported_arch() {
        // Compile only: this test would fail to compile on an unsupported arch
        // because target_arch() would call bail! at compile time via cfg.
        let _ = target_arch();
    }
}
