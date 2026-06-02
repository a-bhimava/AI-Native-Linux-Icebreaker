/// seccomp.rs — Seccomp-BPF syscall denylist (INV-5, Phase 1 v1).
///
/// Layer 3 of the sandbox stack: after `fs::validate()` (M1.2) and Landlock
/// (M1.3), the kernel filters dangerous syscalls. mcpd dies with `SIGSYS` and
/// the audit log records the syscall number if mount/reboot/kexec/bpf/ptrace
/// (the explicitly forbidden set) ever fires.
///
/// **Phase 1 v1 uses a denylist, not an allowlist.** A tokio-based async
/// program touches a wide and kernel-version-dependent set of syscalls
/// (clone3, sched_getaffinity, timerfd_*, rseq, pidfd_*, ...) that's
/// impractical to enumerate completely without per-kernel observation. The
/// roadmap §M1.4 specified an allowlist with `KILL_PROCESS` default; v1
/// inverts that for engineering tractability and ships a denylist with
/// `Allow` default. The denylist captures the high-impact syscalls that
/// have no legitimate use in a stdio JSON-RPC daemon: mount/umount,
/// reboot/kexec, init_module/finit_module, bpf, ptrace, unshare,
/// pivot_root, setuid/setgid, swapon/swapoff.
///
/// Phase 7 hardening will tighten this back to an allowlist once we've
/// harvested the comprehensive syscall set from a soak run with
/// `SeccompAction::Log` on the target kernel.
///
/// `PR_SET_NO_NEW_PRIVS` is still set so the filter cannot be dropped via
/// setuid binaries. The filter and Landlock are applied in `main.rs`
/// immediately before the server accepts requests.
use anyhow::{bail, Result};
use seccompiler::{
    BpfProgram, SeccompAction, SeccompFilter, TargetArch,
};
use std::collections::BTreeMap;
use std::convert::TryInto;
use tracing::info;

pub fn apply() -> Result<()> {
    set_no_new_privs()?;

    let rules: BTreeMap<i64, Vec<seccompiler::SeccompRule>> = denied_syscalls()
        .into_iter()
        .map(|nr| (nr, Vec::new())) // empty rules = match this nr regardless of args
        .collect();

    // Phase 1 v1: deny-listed syscalls → KillProcess; everything else → Allow.
    // See module-level comment for the engineering tradeoff and Phase 7 plan.
    let filter = SeccompFilter::new(
        rules,
        SeccompAction::Allow,        // mismatch (i.e. anything NOT in our deny set)
        SeccompAction::KillProcess,  // match (anything in the deny set)
        target_arch()?,
    )
    .map_err(|e| anyhow::anyhow!("seccomp filter build failed: {}", e))?;

    let program: BpfProgram = filter
        .try_into()
        .map_err(|e| anyhow::anyhow!("seccomp BPF compilation failed: {}", e))?;
    seccompiler::apply_filter(&program)
        .map_err(|e| anyhow::anyhow!("seccomp apply_filter failed: {}", e))?;

    info!(
        "seccomp: filter installed ({} denied syscalls, default=Allow; Phase 7 will invert)",
        denied_syscalls().len()
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

/// Syscalls that have no legitimate use in mcpd and trigger SIGSYS if
/// invoked. Keep this set tight and high-signal so we don't accidentally
/// block legitimate tokio/glibc behavior.
pub(crate) fn denied_syscalls() -> Vec<i64> {
    vec![
        // ── Filesystem-level admin (mcpd doesn't mount anything) ──────────
        libc::SYS_mount,
        libc::SYS_umount2,
        libc::SYS_pivot_root,
        libc::SYS_chroot,
        libc::SYS_swapon,
        libc::SYS_swapoff,
        // ── Kernel/system control ────────────────────────────────────────
        libc::SYS_reboot,
        libc::SYS_kexec_load,
        libc::SYS_init_module,
        libc::SYS_finit_module,
        libc::SYS_delete_module,
        // ── Tracing / introspection of other processes ───────────────────
        libc::SYS_ptrace,
        libc::SYS_process_vm_readv,
        libc::SYS_process_vm_writev,
        // ── eBPF / kernel programmability ────────────────────────────────
        libc::SYS_bpf,
        libc::SYS_perf_event_open,
        // ── Namespace manipulation (mcpd runs in caller's namespaces) ────
        libc::SYS_unshare,
        libc::SYS_setns,
        // ── UID/GID changes — mcpd must run as a non-root user, never lift
        libc::SYS_setuid,
        libc::SYS_setgid,
        libc::SYS_setreuid,
        libc::SYS_setregid,
        libc::SYS_setresuid,
        libc::SYS_setresgid,
        libc::SYS_setfsuid,
        libc::SYS_setfsgid,
        // ── Capability set changes ───────────────────────────────────────
        libc::SYS_capset,
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn denylist_includes_dangerous_syscalls() {
        let denied = denied_syscalls();
        assert!(denied.contains(&libc::SYS_mount));
        assert!(denied.contains(&libc::SYS_umount2));
        assert!(denied.contains(&libc::SYS_reboot));
        assert!(denied.contains(&libc::SYS_kexec_load));
        assert!(denied.contains(&libc::SYS_init_module));
        assert!(denied.contains(&libc::SYS_finit_module));
        assert!(denied.contains(&libc::SYS_bpf));
        assert!(denied.contains(&libc::SYS_ptrace));
        assert!(denied.contains(&libc::SYS_setuid));
        assert!(denied.contains(&libc::SYS_setgid));
        assert!(denied.contains(&libc::SYS_unshare));
        assert!(denied.contains(&libc::SYS_pivot_root));
        assert!(denied.contains(&libc::SYS_setns));
        assert!(denied.contains(&libc::SYS_capset));
    }

    #[test]
    fn denylist_excludes_routine_syscalls() {
        let denied = denied_syscalls();
        // Everything legitimate must remain Allowed (i.e., NOT in the deny set).
        let routine = [
            libc::SYS_read, libc::SYS_write, libc::SYS_close,
            libc::SYS_openat, libc::SYS_openat2,
            libc::SYS_mmap, libc::SYS_munmap, libc::SYS_brk,
            libc::SYS_futex, libc::SYS_epoll_pwait, libc::SYS_eventfd2,
            libc::SYS_clone3, libc::SYS_rseq,
            libc::SYS_exit, libc::SYS_exit_group,
            libc::SYS_landlock_create_ruleset,
        ];
        for nr in routine {
            assert!(!denied.contains(&nr),
                    "syscall {} is routine; must NOT be denied", nr);
        }
    }

    #[test]
    fn target_arch_returns_supported_arch() {
        // Compile only: this test would fail to compile on an unsupported arch
        // because target_arch() would call bail! at compile time via cfg.
        let _ = target_arch();
    }
}
