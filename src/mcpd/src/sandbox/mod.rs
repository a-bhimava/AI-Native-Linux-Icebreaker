/// sandbox/ — Kernel-enforced isolation layers for mcpd (INV-5).
///
/// `apply()` is the single entry point. It MUST be called before the server
/// accepts any JSON-RPC request. The intended sequence in main:
///
///   1. tracing_subscriber::init()
///   2. sandbox::apply()?         ← Landlock + (M1.4) Seccomp-BPF here
///   3. server::run_stdio_server().await
///
/// On Linux: applies a Landlock filesystem ruleset that pins mcpd to the
/// whitelist roots, then Seccomp-BPF (M1.4) restricts the syscall surface.
/// If either is unavailable (e.g. kernel < 5.13), the process hard-exits —
/// running un-sandboxed is an INV-5 violation.
///
/// On non-Linux (macOS dev loop): emits a warning and returns Ok. The fs
/// module's macOS fallback also reflects this — local builds run without
/// kernel sandboxing, production runs on Linux.

#[cfg(target_os = "linux")]
mod landlock;
#[cfg(target_os = "linux")]
mod seccomp;

/// Apply all available sandboxing layers. Idempotent in the sense that the
/// kernel will refuse to widen an already-applied ruleset, but `apply` should
/// only be called once at startup.
///
/// Order matters: Landlock first (filesystem access), Seccomp-BPF second
/// (syscall surface). Doing Seccomp first would block `landlock_*` syscalls
/// unless they're explicitly allowed; doing Landlock first means the
/// seccomp install itself doesn't trip the filesystem ruleset.
pub fn apply() -> anyhow::Result<()> {
    #[cfg(target_os = "linux")]
    {
        landlock::apply()?;
        seccomp::apply()?;
    }
    #[cfg(not(target_os = "linux"))]
    {
        tracing::warn!(
            "sandbox: not on Linux — Landlock+Seccomp skipped (dev-only mode). \
             Production builds run on Linux."
        );
    }
    Ok(())
}
