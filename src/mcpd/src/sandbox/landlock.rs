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
    RulesetCreatedAttr, RulesetError, RulesetStatus, ABI,
};
use std::path::PathBuf;
use tracing::{info, warn};

pub fn apply() -> Result<()> {
    // Use the minimum ABI we rely on. Newer kernels expose additional access
    // types we'd ignore here; landlock 0.4 surfaces that via
    // RulesetStatus::PartiallyEnforced — see match arms below.
    let abi = ABI::V1;

    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());
    let home_path = PathBuf::from(&home);

    // Build the rule list as Results so PathFd::new failures can short-circuit
    // via add_rules() per landlock 0.4's IntoIterator<Item = Result<_, _>> API.
    let read_only_roots = [
        "/proc", "/sys", "/etc", "/usr", "/lib", "/lib64", "/var/log",
    ];
    let read_write_roots = ["/tmp", "/var/log/mcpd"];

    let mut rules: Vec<std::result::Result<PathBeneath<PathFd>, RulesetError>> = Vec::new();

    for p in read_only_roots {
        match PathFd::new(p) {
            Ok(fd) => rules.push(Ok(PathBeneath::new(fd, AccessFs::from_read(abi)))),
            Err(e) => warn!("landlock: skipping read-only root '{}': {}", p, e),
        }
    }
    for p in read_write_roots {
        match PathFd::new(p) {
            Ok(fd) => rules.push(Ok(PathBeneath::new(fd, AccessFs::from_all(abi)))),
            Err(e) => warn!("landlock: skipping read-write root '{}': {}", p, e),
        }
    }
    if home_path.exists() {
        match PathFd::new(&home_path) {
            Ok(fd) => rules.push(Ok(PathBeneath::new(fd, AccessFs::from_read(abi)))),
            Err(e) => warn!("landlock: skipping $HOME '{}': {}", home_path.display(), e),
        }
    }

    // F-28: extra admin-configured read roots. Populated from
    // controller.toml [mcpd.fs] read_roots by the Controller and passed via
    // the scrubbed env. Kernel-level allow list matches userspace validate().
    if let Ok(extra) = std::env::var("MCPD_FS_READ_ROOTS") {
        for p in extra.split(':').filter(|s| !s.is_empty()) {
            if !p.starts_with('/') { continue; }
            let path = PathBuf::from(p);
            if !path.exists() { continue; }
            match PathFd::new(&path) {
                Ok(fd) => rules.push(Ok(PathBeneath::new(fd, AccessFs::from_read(abi)))),
                Err(e) => warn!("landlock: skipping read root '{}': {}", p, e),
            }
        }
    }

    let status = Ruleset::default()
        .handle_access(AccessFs::from_all(abi))?
        .create()?
        .add_rules(rules)?
        .restrict_self()?;

    match status.ruleset {
        RulesetStatus::FullyEnforced => {
            info!("landlock: ABI v{} fully enforced", abi as u32);
            Ok(())
        }
        RulesetStatus::PartiallyEnforced => {
            warn!("landlock: only partially enforced (kernel missing some ABI features)");
            Ok(())
        }
        RulesetStatus::NotEnforced => {
            bail!(
                "landlock: NOT enforced after restrict_self() — kernel < 5.13? \
                 INV-5 violation. Aborting."
            );
        }
    }
}
