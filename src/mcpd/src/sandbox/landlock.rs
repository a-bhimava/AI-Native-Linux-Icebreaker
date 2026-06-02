/// landlock.rs — Apply the Landlock LSM ruleset (INV-5, Linux ≥ 5.13).
///
/// Landlock is the kernel-side filesystem sandbox. The ruleset is the second
/// layer of defense behind `fs::validate()` (M1.2). Even if a future tool fn
/// forgets to validate(), the kernel will refuse opens that escape the rules
/// defined here.
///
/// Ruleset (matches `docs/phase1_roadmap.md` §M1.3):
///
///   READ-ONLY:
///     /proc, /sys     — kernel-exposed read-only data
///     /etc            — system config
///     /usr            — installed binaries / libs
///     /lib, /lib64    — dynamic loader / shared libs
///     /var/log        — system logs (mcpd's audit log lives in a sub-tree)
///     $HOME           — user files; writes there land in M1.5 with COW
///
///   READ-WRITE:
///     /tmp            — scratch
///     /var/log/mcpd   — mcpd's own audit log (M1.9)
///
/// Anything outside this list is denied by the kernel at `open()` time.
///
/// The crate API used here is `landlock = 0.4`. If the kernel doesn't
/// support Landlock, `apply()` hard-exits the process — running without the
/// sandbox is an INV-5 violation per CLAUDE.md.
use anyhow::{bail, Result};
use landlock::{
    Access, AccessFs, PathBeneath, PathFd, Ruleset, RulesetAttr,
    RulesetCreatedAttr, RulesetStatus, ABI,
};
use std::path::PathBuf;
use tracing::{info, warn};

pub fn apply() -> Result<()> {
    let abi = ABI::new_current();
    // ABI::Unsupported has integer value 0; later versions are >0.
    if (abi as u32) == 0 {
        bail!(
            "Landlock not supported on this kernel (need ≥ 5.13). \
             Running un-sandboxed is an INV-5 violation. Aborting."
        );
    }
    info!("landlock: ABI v{}", abi as u32);

    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());
    let home_path = PathBuf::from(&home);

    // Step 1: declare which access types we're willing to handle at all.
    // Everything not declared here passes through unchecked (kernel allows).
    // We declare "from_all" so the kernel checks every operation type.
    let ruleset = Ruleset::default()
        .handle_access(AccessFs::from_all(abi))?
        .create()?;

    // Step 2: add per-path allowances. Anything not listed here is denied.
    let read_only: &[&str] = &[
        "/proc", "/sys", "/etc", "/usr", "/lib", "/lib64", "/var/log",
    ];
    let read_write: &[&str] = &[
        "/tmp", "/var/log/mcpd",
    ];

    let mut rules: Vec<PathBeneath<PathFd>> = Vec::new();

    for p in read_only {
        match PathFd::new(p) {
            Ok(fd) => rules.push(PathBeneath::new(fd, AccessFs::from_read(abi))),
            Err(e) => {
                // Missing root → skip. Common in containers (no /lib64 on Alpine etc.)
                warn!("landlock: skipping read-only root '{}': {}", p, e);
            }
        }
    }
    for p in read_write {
        match PathFd::new(p) {
            Ok(fd) => rules.push(PathBeneath::new(fd, AccessFs::from_all(abi))),
            Err(e) => {
                warn!("landlock: skipping read-write root '{}': {}", p, e);
            }
        }
    }
    // $HOME — read-only for v1 (writes inside $HOME land in M1.5).
    if home_path.exists() {
        match PathFd::new(&home_path) {
            Ok(fd) => rules.push(PathBeneath::new(fd, AccessFs::from_read(abi))),
            Err(e) => warn!("landlock: skipping $HOME '{}': {}", home_path.display(), e),
        }
    }

    let ruleset = ruleset.add_rules(rules)?;
    let status = ruleset.restrict_self()?;

    match status.ruleset {
        RulesetStatus::FullyEnforced => {
            info!("landlock: fully enforced");
            Ok(())
        }
        RulesetStatus::PartiallyEnforced => {
            // Kernel supports Landlock but not every access type in our ABI.
            // Continue — this is acceptable degradation, not a violation.
            warn!("landlock: only partially enforced (kernel missing some ABI features)");
            Ok(())
        }
        RulesetStatus::NotEnforced => {
            bail!(
                "landlock: NOT enforced after restrict_self(). \
                 INV-5 violation. Aborting."
            );
        }
    }
}
