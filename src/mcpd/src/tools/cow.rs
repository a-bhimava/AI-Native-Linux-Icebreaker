/// cow.rs — COW simulation + intent store (M7.0.1 / v6.16, 2026-08-03).
///
/// The whitepaper §5 "Imagination Layer" says destructive ops are routed
/// through a copy-on-write overlay, run inside the overlay, and only
/// committed after a human sees the diff. Until v6.15 this was a stub:
/// `fs.delete` / `fs.write outside home` / `package.*` returned a
/// `requires_cow_approval` ticket carrying only path+size — no real diff,
/// no commit path. The audit at `docs/2026-08-03_whitepaper_vs_reality_audit.md`
/// tagged this as Tier-A #1 (row C-1).
///
/// M7.0.1 closes the gap via **targeted simulation** instead of literal
/// overlayfs mount:
///   - fs.delete → walkdir + `metadata().len()` sum, count entries.
///   - fs.write outside home → stat existing target, compute byte delta.
///   - package.install/remove/upgrade → `apt-get -s -qq` and parse output.
///
/// Rationale for simulation over real overlayfs: real overlay would need
/// `SYS_mount`/`SYS_umount2`/`SYS_unshare` moved from seccomp deny→allow,
/// plus Landlock rules for the upperdir/workdir/dpkg-state/apt-cache paths.
/// That's material sandbox weakening for equivalent user outcome (the
/// whitepaper §8.2 modal text — "3.2 GB will be freed. 847 files will be
/// deleted." — is achievable from stat + apt-get -s output directly).
///
/// This module is the primitive layer. Wiring into `fs.rs` / `package.rs`
/// ticket envelopes lands in M7.0.1b; the `cow.commit` RPC + intent-store
/// hook-up lands in M7.0.1c.
use anyhow::{bail, Result};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::Path;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime};
use uuid::Uuid;
use walkdir::WalkDir;

/// Cap the affected-paths sample for the HITL modal. Full list would blow
/// the modal render budget; the human summary carries the count.
const AFFECTED_SAMPLE_CAP: usize = 20;

/// Cap on how deep we walk during `simulate_fs_delete`. Pathological trees
/// (e.g. a symlink loop that Landlock didn't catch, or a fuse mount going
/// wild) shouldn't hang the daemon. WalkDir returns entries as it descends
/// so this cap bounds both time and memory. Real deletes larger than this
/// still fire; the preview just says "~N entries (sample truncated)".
const WALK_ENTRY_CAP: usize = 50_000;

/// TTL for pending intents in the store. Matches the default HITL prompt
/// timeout (controller.toml `[hitl] timeout_seconds = 300`) so a stale
/// approval can never be committed.
pub const INTENT_TTL: Duration = Duration::from_secs(300);

// ── DryRunDiff — the payload we return to the client ────────────────────────

/// Result of a COW dry-run simulation. Serialized into the ticket's
/// `preview.diff` sub-object; the controller renders `human_summary` in the
/// Tier-3 HITL modal (whitepaper §8.2 shape).
#[derive(Debug, Clone, Serialize)]
pub struct DryRunDiff {
    /// Which operation this diff describes: `"fs.delete"`, `"fs.write"`,
    /// `"package.install"`, `"package.remove"`, `"package.upgrade"`.
    pub operation: String,
    /// Signed byte-delta on disk: negative for delete/remove, positive for
    /// write/install/upgrade. Zero if unknown (e.g. apt-cache miss).
    pub bytes_delta: i64,
    /// Signed file-count delta. Zero for package ops (per-file granularity
    /// isn't cheap to compute pre-install).
    pub file_count_delta: i64,
    /// First N affected paths for the modal. Empty for package ops.
    pub affected_paths_sample: Vec<String>,
    /// Human-readable one-liner rendered in the HITL modal. mcpd owns the
    /// primary phrasing so on-guest debugging is straightforward.
    pub human_summary: String,
    /// "LOW" | "MED" | "HIGH". Mirrors the intent's risk classification
    /// (advisory — the Controller's risk_classifier is authoritative).
    pub risk: String,
    /// True if the op can be trivially reversed (e.g. re-download package,
    /// restore prior file contents from the intent store).
    pub reversible: bool,
}

impl DryRunDiff {
    pub fn to_json(&self) -> Value {
        json!({
            "operation": self.operation,
            "bytes_delta": self.bytes_delta,
            "file_count_delta": self.file_count_delta,
            "affected_paths_sample": self.affected_paths_sample,
            "human_summary": self.human_summary,
            "risk": self.risk,
            "reversible": self.reversible,
        })
    }
}

// ── Byte formatting ──────────────────────────────────────────────────────────

/// Format an unsigned byte count in the closest human unit. Used inside
/// `human_summary` strings so users see "3.2 GB" not "3435973836 bytes".
pub fn human_bytes(n: u64) -> String {
    const KB: u64 = 1024;
    const MB: u64 = 1024 * KB;
    const GB: u64 = 1024 * MB;
    const TB: u64 = 1024 * GB;

    if n >= TB {
        format!("{:.1} TB", n as f64 / TB as f64)
    } else if n >= GB {
        format!("{:.1} GB", n as f64 / GB as f64)
    } else if n >= MB {
        format!("{:.1} MB", n as f64 / MB as f64)
    } else if n >= KB {
        format!("{:.1} KB", n as f64 / KB as f64)
    } else if n == 1 {
        "1 byte".to_string()
    } else {
        format!("{} bytes", n)
    }
}

// ── fs.delete simulator ──────────────────────────────────────────────────────

/// Walk the target path, sum sizes, count entries, sample the first N paths.
/// Symlinks are NOT followed (`WalkDir::follow_links(false)` is the default)
/// so a symlink under the tree contributes its own size (usually a few bytes)
/// rather than the resolved target's size.
///
/// Returns a `DryRunDiff` with `operation="fs.delete"`, negative `bytes_delta`
/// / `file_count_delta`, and `reversible=false` (deleted bytes are gone unless
/// the caller separately backs them up before commit).
pub fn simulate_fs_delete(path: &Path) -> Result<DryRunDiff> {
    // Reject symlink at the top: deleting a symlink is a separate semantic
    // (unlink the link, not the target). If Landlock allowed the caller to
    // reach a symlink, the delete op should fail explicitly rather than
    // walking through it to a location the caller didn't intend.
    if path.is_symlink() {
        bail!("simulate_fs_delete: path is a symlink; explicit unlink required");
    }

    let meta = match std::fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) => {
            // Path doesn't exist → nothing to delete. Return a valid diff
            // that surfaces this to the user instead of erroring.
            return Ok(DryRunDiff {
                operation: "fs.delete".to_string(),
                bytes_delta: 0,
                file_count_delta: 0,
                affected_paths_sample: vec![],
                human_summary: format!("Path does not exist ({}); nothing to delete.", e),
                risk: "LOW".to_string(),
                reversible: false,
            });
        }
    };

    // Fast path: single file.
    if meta.is_file() {
        let bytes = meta.len();
        return Ok(DryRunDiff {
            operation: "fs.delete".to_string(),
            bytes_delta: -(bytes as i64),
            file_count_delta: -1,
            affected_paths_sample: vec![path.display().to_string()],
            human_summary: format!(
                "{} will be freed. 1 file will be deleted.",
                human_bytes(bytes)
            ),
            risk: risk_for_bytes(bytes),
            reversible: false,
        });
    }

    // Directory: walk it.
    let mut total_bytes: u64 = 0;
    let mut file_count: usize = 0;
    let mut dir_count: usize = 0;
    let mut sample: Vec<String> = Vec::new();
    let mut walked: usize = 0;
    let mut truncated = false;

    for entry in WalkDir::new(path).follow_links(false).into_iter() {
        walked += 1;
        if walked > WALK_ENTRY_CAP {
            truncated = true;
            break;
        }
        let entry = match entry {
            Ok(e) => e,
            Err(_) => continue, // permission denied on a subtree — count zero and continue
        };
        if sample.len() < AFFECTED_SAMPLE_CAP {
            sample.push(entry.path().display().to_string());
        }
        let m = match entry.metadata() {
            Ok(m) => m,
            Err(_) => continue,
        };
        if m.is_file() {
            file_count += 1;
            total_bytes += m.len();
        } else if m.is_dir() {
            dir_count += 1;
        }
        // Symlinks contribute their own size to bytes_delta via file_type
        // branch above? WalkDir with follow_links(false) yields them; we
        // don't classify them as files (m.is_file() is false for symlinks
        // on unix), so they add 0 bytes. Correct — a symlink is a name, not
        // a payload.
    }

    let total_entries = file_count + dir_count;
    let human = if truncated {
        format!(
            "{} will be freed. ~{} files and {} directories will be deleted (walk truncated at {} entries).",
            human_bytes(total_bytes),
            file_count,
            dir_count,
            WALK_ENTRY_CAP,
        )
    } else if dir_count > 0 {
        format!(
            "{} will be freed. {} files and {} {} will be deleted.",
            human_bytes(total_bytes),
            file_count,
            dir_count,
            if dir_count == 1 { "directory" } else { "directories" },
        )
    } else {
        format!(
            "{} will be freed. {} {} will be deleted.",
            human_bytes(total_bytes),
            file_count,
            if file_count == 1 { "file" } else { "files" },
        )
    };

    Ok(DryRunDiff {
        operation: "fs.delete".to_string(),
        bytes_delta: -(total_bytes as i64),
        file_count_delta: -(total_entries as i64),
        affected_paths_sample: sample,
        human_summary: human,
        risk: risk_for_bytes(total_bytes),
        reversible: false,
    })
}

/// Size-based risk classification for delete/write ops. LOW for anything
/// under 100 MB, MED for 100 MB - 5 GB, HIGH beyond that. The Controller's
/// risk_classifier is authoritative (BP-5 escalate-only); this is advisory.
fn risk_for_bytes(bytes: u64) -> String {
    const MB100: u64 = 100 * 1024 * 1024;
    const GB5: u64 = 5 * 1024 * 1024 * 1024;
    if bytes >= GB5 {
        "HIGH".to_string()
    } else if bytes >= MB100 {
        "MED".to_string()
    } else {
        "LOW".to_string()
    }
}

// ── fs.write outside home simulator ──────────────────────────────────────────

/// Stat the target (if present) and compute `content_bytes - existing_size`.
/// `reversible=true` because M7.0.1c's intent store snapshots the prior
/// bytes so cow.commit can restore on demand.
pub fn simulate_fs_write_outside_home(path: &Path, content_bytes: u64) -> Result<DryRunDiff> {
    let existing = std::fs::symlink_metadata(path)
        .ok()
        .and_then(|m| if m.is_file() { Some(m.len()) } else { None })
        .unwrap_or(0);
    let delta = content_bytes as i64 - existing as i64;

    let is_new = existing == 0 && !path.exists();
    let human = if is_new {
        format!(
            "{} will be written to {} (new file).",
            human_bytes(content_bytes),
            path.display()
        )
    } else {
        let word = if delta >= 0 { "grow" } else { "shrink" };
        format!(
            "{} will be written to {} ({} by {}, currently {}).",
            human_bytes(content_bytes),
            path.display(),
            word,
            human_bytes(delta.unsigned_abs()),
            human_bytes(existing),
        )
    };

    Ok(DryRunDiff {
        operation: "fs.write".to_string(),
        bytes_delta: delta,
        file_count_delta: if is_new { 1 } else { 0 },
        affected_paths_sample: vec![path.display().to_string()],
        human_summary: human,
        risk: "MED".to_string(), // write outside $HOME is inherently privileged
        reversible: true,
    })
}

// ── package.* simulator ──────────────────────────────────────────────────────

/// Debian package op simulator. Shells `apt-get -s -qq {op} {pkg}` and
/// parses `Inst`/`Remv`/`Conf`/`Purg` lines to count affected packages.
/// Falls back to a visible-warning DryRunDiff (`reversible=false`,
/// `risk="MED"`) on any failure so the user sees WHY the preview failed
/// rather than getting a silent-pass.
pub async fn simulate_package_op(op: &str, package: &str) -> Result<DryRunDiff> {
    let apt_op = match op {
        "package.install" => "install",
        "package.remove" => "remove",
        "package.upgrade" => "install", // "upgrade this specific pkg" = re-install
        _ => bail!("simulate_package_op: unsupported op {:?}", op),
    };

    let apt_result = tokio::process::Command::new("apt-get")
        .arg("-s")
        .arg("-qq")
        .arg(apt_op)
        .arg(package)
        .env("DEBIAN_FRONTEND", "noninteractive")
        .output()
        .await;

    let out = match apt_result {
        Ok(o) => o,
        Err(e) => {
            return Ok(DryRunDiff {
                operation: op.to_string(),
                bytes_delta: 0,
                file_count_delta: 0,
                affected_paths_sample: vec![],
                human_summary: format!(
                    "Preview unavailable: apt-get not runnable ({}). Approve only if you trust the intent.",
                    e
                ),
                risk: "MED".to_string(),
                reversible: false,
            });
        }
    };

    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Ok(DryRunDiff {
            operation: op.to_string(),
            bytes_delta: 0,
            file_count_delta: 0,
            affected_paths_sample: vec![],
            human_summary: format!(
                "Preview failed: apt-get -s exit={:?}. stderr: {}",
                out.status.code(),
                stderr.trim(),
            ),
            risk: "MED".to_string(),
            reversible: false,
        });
    }

    let stdout = String::from_utf8_lossy(&out.stdout);
    let (inst_count, remv_count, upgrade_count) = count_apt_actions(&stdout);
    let total_affected = inst_count + remv_count + upgrade_count;

    let (bytes_delta, human) = match op {
        "package.install" => {
            let human = format!(
                "{} package{} will be installed{}.",
                inst_count,
                if inst_count == 1 { "" } else { "s" },
                if remv_count > 0 {
                    format!(" ({} will be removed)", remv_count)
                } else if upgrade_count > 0 {
                    format!(" ({} will be upgraded)", upgrade_count)
                } else {
                    String::new()
                },
            );
            (0i64, human) // apt-get -s doesn't emit sizes reliably in -qq mode
        }
        "package.remove" => {
            let human = format!(
                "{} package{} will be removed.",
                remv_count,
                if remv_count == 1 { "" } else { "s" },
            );
            (0i64, human)
        }
        "package.upgrade" => {
            let human = format!(
                "{} package{} will be upgraded{}.",
                upgrade_count + inst_count,
                if (upgrade_count + inst_count) == 1 { "" } else { "s" },
                if remv_count > 0 {
                    format!(" ({} will be removed)", remv_count)
                } else {
                    String::new()
                },
            );
            (0i64, human)
        }
        _ => unreachable!(),
    };

    Ok(DryRunDiff {
        operation: op.to_string(),
        bytes_delta,
        file_count_delta: total_affected as i64,
        affected_paths_sample: vec![],
        human_summary: human,
        risk: if total_affected > 20 { "HIGH" } else { "MED" }.to_string(),
        reversible: op != "package.upgrade", // upgrades can't be rolled back cleanly
    })
}

/// Parse `apt-get -s -qq` output. Returns `(install_count, remove_count,
/// upgrade_count)`. apt-get emits lines like:
///     Inst nginx (1.24.0-2ubuntu1 Ubuntu:24.04/noble [amd64])
///     Conf nginx (1.24.0-2ubuntu1 Ubuntu:24.04/noble [amd64])
///     Remv old-nginx [1.20.0]
/// We count `Inst` as install, `Remv`/`Purg` as remove, and a line that
/// starts with `Inst` for a package that also has a `Remv` in the output
/// (i.e. version bump) as upgrade. That last classification is best-effort
/// and lives inside the simulator (not the caller) so the caller sees a
/// single denormalized (inst_count, remv_count, upgrade_count) triple.
pub fn count_apt_actions(apt_output: &str) -> (usize, usize, usize) {
    let mut inst: Vec<&str> = Vec::new();
    let mut remv: Vec<&str> = Vec::new();
    for line in apt_output.lines() {
        let line = line.trim_start();
        if let Some(rest) = line.strip_prefix("Inst ") {
            if let Some(name) = rest.split_whitespace().next() {
                inst.push(name);
            }
        } else if let Some(rest) = line.strip_prefix("Remv ").or_else(|| line.strip_prefix("Purg ")) {
            if let Some(name) = rest.split_whitespace().next() {
                remv.push(name);
            }
        }
    }
    // A package appearing in both Inst and Remv is an upgrade.
    let upgrade_count = inst.iter().filter(|n| remv.contains(*n)).count();
    let pure_inst = inst.len() - upgrade_count;
    let pure_remv = remv.len() - upgrade_count;
    (pure_inst, pure_remv, upgrade_count)
}

// ── Intent store ─────────────────────────────────────────────────────────────

/// A committed-but-not-yet-executed intent. Held in `IntentStore` between
/// the preview call (which returns `intent_id`) and the eventual
/// `cow.commit` call from the client after the HITL modal approves.
#[derive(Debug, Clone)]
pub struct PendingIntent {
    pub intent_id: Uuid,
    pub operation: String,
    pub subject: String,
    /// For fs.write only — the bytes the caller proposed to write. On
    /// commit, mcpd writes these atomically. `None` for fs.delete /
    /// package.*.
    pub proposed_bytes: Option<Vec<u8>>,
    /// For fs.write only — target file mode (rwx bits). None → uses the
    /// module default (0o644).
    pub proposed_mode: Option<u32>,
    /// For fs.write only — snapshot of the prior file contents so we can
    /// roll back on future undo requests. Only populated when the target
    /// existed and was small enough (see `MAX_UNDO_SNAPSHOT_BYTES`).
    pub prior_snapshot: Option<Vec<u8>>,
    pub diff_snapshot: DryRunDiff,
    pub expires_at: SystemTime,
}

/// In-memory store of pending intents. Each preview call `register`s an
/// intent; the client's `cow.commit` calls `take` which validates + removes
/// atomically. TTL-based expiry protects against stale approvals.
///
/// Thread-safety: `std::sync::Mutex` — locks are held briefly (HashMap ops
/// only), and mcpd's request dispatch is a single tokio runtime so lock
/// contention is not a bottleneck.
pub struct IntentStore {
    inner: Mutex<HashMap<Uuid, PendingIntent>>,
    ttl: Duration,
}

/// Errors returned by `IntentStore::take` on commit-time validation.
#[derive(Debug, thiserror::Error)]
pub enum CowError {
    #[error("intent_id not found: {0}")]
    NotFound(Uuid),
    #[error("intent expired at {0:?} (TTL = {1:?})")]
    Expired(SystemTime, Duration),
    #[error("operation mismatch: preview was {expected}, commit is {actual}")]
    OperationMismatch { expected: String, actual: String },
    #[error("subject mismatch: preview was {expected:?}, commit is {actual:?}")]
    SubjectMismatch { expected: String, actual: String },
}

impl IntentStore {
    pub fn new(ttl: Duration) -> Self {
        Self {
            inner: Mutex::new(HashMap::new()),
            ttl,
        }
    }

    pub fn with_default_ttl() -> Self {
        Self::new(INTENT_TTL)
    }

    /// Register a new pending intent; returns its ID. Called by the
    /// preview path (M7.0.1b wiring). Also opportunistically GCs expired
    /// entries so a long-running daemon doesn't leak them.
    pub fn register(&self, intent: PendingIntent) -> Uuid {
        let id = intent.intent_id;
        let mut inner = self.inner.lock().expect("IntentStore mutex poisoned");
        inner.retain(|_, v| v.expires_at > SystemTime::now());
        inner.insert(id, intent);
        id
    }

    /// Remove and return the intent identified by `id`, validating that
    /// `expected_op` and `expected_subject` match what was registered (no
    /// op-swap or subject-swap between approval and commit).
    pub fn take(
        &self,
        id: Uuid,
        expected_op: &str,
        expected_subject: &str,
    ) -> Result<PendingIntent, CowError> {
        let mut inner = self.inner.lock().expect("IntentStore mutex poisoned");
        let intent = inner.remove(&id).ok_or(CowError::NotFound(id))?;
        if intent.expires_at <= SystemTime::now() {
            return Err(CowError::Expired(intent.expires_at, self.ttl));
        }
        if intent.operation != expected_op {
            return Err(CowError::OperationMismatch {
                expected: intent.operation,
                actual: expected_op.to_string(),
            });
        }
        if intent.subject != expected_subject {
            return Err(CowError::SubjectMismatch {
                expected: intent.subject,
                actual: expected_subject.to_string(),
            });
        }
        Ok(intent)
    }

    /// Drop expired entries. Called opportunistically by `register`; can
    /// also be called from a periodic tokio task if we ever add one.
    pub fn gc(&self) {
        let now = SystemTime::now();
        self.inner
            .lock()
            .expect("IntentStore mutex poisoned")
            .retain(|_, v| v.expires_at > now);
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().len()
    }
}

impl Default for IntentStore {
    fn default() -> Self {
        Self::with_default_ttl()
    }
}

// ── Process-wide accessor (used by preview + commit paths) ──────────────────

/// The single mcpd-process IntentStore. Preview handlers (`fs::delete`,
/// `fs::write` outside home, `package::install/remove/upgrade`) call
/// `register_intent(...)` to reserve the intent_id they emit in the
/// ticket; the `cow.commit` RPC handler calls `intent_store().take(...)`
/// to consume it before executing the real op.
///
/// Follows the same lazy-init pattern as `fs::home()` and
/// `schema::registry()` — no config plumbing needed to reach it from any
/// tool handler.
pub fn intent_store() -> &'static IntentStore {
    static STORE: OnceLock<IntentStore> = OnceLock::new();
    STORE.get_or_init(IntentStore::with_default_ttl)
}

/// Convenience wrapper: mint a fresh Uuid, wrap the caller's op-specific
/// bits in a PendingIntent, register it, return the id. Preview handlers
/// use this to keep the "reserve + return-ticket" flow one line.
pub fn register_intent(
    operation: &str,
    subject: &str,
    diff: DryRunDiff,
    proposed_bytes: Option<Vec<u8>>,
    proposed_mode: Option<u32>,
    prior_snapshot: Option<Vec<u8>>,
) -> Uuid {
    let intent = PendingIntent {
        intent_id: Uuid::new_v4(),
        operation: operation.to_string(),
        subject: subject.to_string(),
        proposed_bytes,
        proposed_mode,
        prior_snapshot,
        diff_snapshot: diff,
        expires_at: SystemTime::now() + INTENT_TTL,
    };
    intent_store().register(intent)
}

// ── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::Write;
    use tempfile::TempDir;

    // --- human_bytes ---

    #[test]
    fn human_bytes_edges() {
        assert_eq!(human_bytes(0), "0 bytes");
        assert_eq!(human_bytes(1), "1 byte");
        assert_eq!(human_bytes(1023), "1023 bytes");
        assert_eq!(human_bytes(1024), "1.0 KB");
        assert_eq!(human_bytes(1024 * 1024), "1.0 MB");
        assert_eq!(human_bytes(3_435_973_836), "3.2 GB");
        assert_eq!(human_bytes(2 * 1024_u64.pow(4)), "2.0 TB");
    }

    // --- simulate_fs_delete ---

    #[test]
    fn simulate_fs_delete_missing_returns_zero_diff() {
        let d = TempDir::new().unwrap();
        let missing = d.path().join("nope");
        let diff = simulate_fs_delete(&missing).unwrap();
        assert_eq!(diff.bytes_delta, 0);
        assert_eq!(diff.file_count_delta, 0);
        assert!(diff.human_summary.contains("does not exist"));
        assert_eq!(diff.reversible, false);
    }

    #[test]
    fn simulate_fs_delete_single_file() {
        let d = TempDir::new().unwrap();
        let f = d.path().join("hello.txt");
        fs::write(&f, "hello world\n").unwrap();
        let diff = simulate_fs_delete(&f).unwrap();
        assert_eq!(diff.operation, "fs.delete");
        assert_eq!(diff.bytes_delta, -12);
        assert_eq!(diff.file_count_delta, -1);
        assert_eq!(diff.affected_paths_sample.len(), 1);
        assert!(diff.human_summary.contains("12 bytes"));
        assert!(diff.human_summary.contains("1 file"));
        assert_eq!(diff.risk, "LOW");
    }

    #[test]
    fn simulate_fs_delete_directory_sums_and_counts() {
        let d = TempDir::new().unwrap();
        // Layout: root/a.txt (100 B), root/sub/b.txt (200 B), root/sub/c.txt (300 B)
        fs::write(d.path().join("a.txt"), vec![b'a'; 100]).unwrap();
        fs::create_dir(d.path().join("sub")).unwrap();
        fs::write(d.path().join("sub/b.txt"), vec![b'b'; 200]).unwrap();
        fs::write(d.path().join("sub/c.txt"), vec![b'c'; 300]).unwrap();

        let diff = simulate_fs_delete(d.path()).unwrap();
        assert_eq!(diff.operation, "fs.delete");
        assert_eq!(diff.bytes_delta, -600);
        // 3 files + 2 dirs (root + sub) = -5 entries
        assert_eq!(diff.file_count_delta, -5);
        assert!(diff.human_summary.contains("600 bytes"));
        assert!(diff.human_summary.contains("3 files"));
    }

    #[test]
    fn simulate_fs_delete_symlink_at_top_rejects() {
        let d = TempDir::new().unwrap();
        let target = d.path().join("real.txt");
        let link = d.path().join("link");
        fs::write(&target, "x").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&target, &link).unwrap();
        #[cfg(not(unix))]
        return; // Windows-symlink path not exercised
        let err = simulate_fs_delete(&link).unwrap_err();
        assert!(err.to_string().contains("symlink"));
    }

    #[test]
    fn simulate_fs_delete_sample_capped() {
        let d = TempDir::new().unwrap();
        for i in 0..(AFFECTED_SAMPLE_CAP + 10) {
            fs::write(d.path().join(format!("f{}.txt", i)), b"x").unwrap();
        }
        let diff = simulate_fs_delete(d.path()).unwrap();
        assert_eq!(diff.affected_paths_sample.len(), AFFECTED_SAMPLE_CAP);
    }

    #[test]
    fn simulate_fs_delete_med_risk_over_100mb() {
        // Skip on CI (creating 100MB temp file eats disk); use a sparse file trick.
        let d = TempDir::new().unwrap();
        let f = d.path().join("big.bin");
        // set_len on a fresh file creates a sparse file — zero disk usage
        // but stat reports the length.
        let handle = fs::File::create(&f).unwrap();
        handle.set_len(101 * 1024 * 1024).unwrap();
        drop(handle);
        let diff = simulate_fs_delete(&f).unwrap();
        assert_eq!(diff.risk, "MED");
        assert!(diff.human_summary.contains("101.0 MB"));
    }

    // --- simulate_fs_write_outside_home ---

    #[test]
    fn simulate_fs_write_new_file() {
        let d = TempDir::new().unwrap();
        let f = d.path().join("new.txt");
        let diff = simulate_fs_write_outside_home(&f, 42).unwrap();
        assert_eq!(diff.operation, "fs.write");
        assert_eq!(diff.bytes_delta, 42);
        assert_eq!(diff.file_count_delta, 1);
        assert!(diff.human_summary.contains("new file"));
        assert_eq!(diff.risk, "MED");
        assert_eq!(diff.reversible, true);
    }

    #[test]
    fn simulate_fs_write_existing_shrink() {
        let d = TempDir::new().unwrap();
        let f = d.path().join("existing.txt");
        fs::write(&f, vec![b'a'; 100]).unwrap();
        let diff = simulate_fs_write_outside_home(&f, 30).unwrap();
        assert_eq!(diff.bytes_delta, -70);
        assert_eq!(diff.file_count_delta, 0);
        assert!(diff.human_summary.contains("shrink"));
        assert!(diff.human_summary.contains("70 bytes"));
    }

    #[test]
    fn simulate_fs_write_existing_grow() {
        let d = TempDir::new().unwrap();
        let f = d.path().join("existing.txt");
        fs::write(&f, b"tiny").unwrap();
        let diff = simulate_fs_write_outside_home(&f, 1024).unwrap();
        assert_eq!(diff.bytes_delta, 1020);
        assert!(diff.human_summary.contains("grow"));
    }

    // --- count_apt_actions parser ---

    #[test]
    fn count_apt_actions_install_only() {
        let sample = "\
Reading package lists...
Building dependency tree...
The following NEW packages will be installed:
  htop
0 upgraded, 1 newly installed, 0 to remove and 3 not upgraded.
Inst htop (3.3.0-4build1 Ubuntu:24.04/noble [amd64])
Conf htop (3.3.0-4build1 Ubuntu:24.04/noble [amd64])
";
        let (inst, remv, upg) = count_apt_actions(sample);
        assert_eq!(inst, 1);
        assert_eq!(remv, 0);
        assert_eq!(upg, 0);
    }

    #[test]
    fn count_apt_actions_upgrade_detected() {
        let sample = "\
Inst nginx-common [1.20.0-1ubuntu1] (1.24.0-2ubuntu1 [all])
Inst nginx [1.20.0-1ubuntu1] (1.24.0-2ubuntu1 [amd64])
Remv nginx-common [1.20.0-1ubuntu1]
Remv nginx [1.20.0-1ubuntu1]
";
        let (inst, remv, upg) = count_apt_actions(sample);
        assert_eq!(upg, 2);
        assert_eq!(inst, 0);
        assert_eq!(remv, 0);
    }

    #[test]
    fn count_apt_actions_mixed() {
        // install htop (new) + upgrade nginx (both Inst + Remv)
        let sample = "\
Inst htop (3.3.0-4build1 [amd64])
Inst nginx [1.20] (1.24 [amd64])
Remv nginx [1.20]
Remv old-crufty-lib [0.9]
Purg deprecated-thing [2.0]
";
        let (inst, remv, upg) = count_apt_actions(sample);
        assert_eq!(inst, 1); // htop
        assert_eq!(upg, 1);  // nginx
        assert_eq!(remv, 2); // old-crufty-lib + deprecated-thing
    }

    #[test]
    fn count_apt_actions_empty() {
        assert_eq!(count_apt_actions(""), (0, 0, 0));
        assert_eq!(count_apt_actions("nothing here"), (0, 0, 0));
    }

    // --- DryRunDiff serialization ---

    #[test]
    fn dry_run_diff_serializes() {
        let diff = DryRunDiff {
            operation: "fs.delete".to_string(),
            bytes_delta: -3_435_973_836,
            file_count_delta: -847,
            affected_paths_sample: vec!["/var/cache/apt/archives/foo.deb".to_string()],
            human_summary: "3.2 GB will be freed. 847 files will be deleted.".to_string(),
            risk: "LOW".to_string(),
            reversible: false,
        };
        let v = diff.to_json();
        assert_eq!(v["operation"], "fs.delete");
        assert_eq!(v["bytes_delta"], -3_435_973_836i64);
        assert_eq!(v["file_count_delta"], -847);
        assert_eq!(v["human_summary"], "3.2 GB will be freed. 847 files will be deleted.");
        assert_eq!(v["risk"], "LOW");
        assert_eq!(v["reversible"], false);
    }

    // --- IntentStore ---

    fn make_intent(op: &str, subject: &str, ttl: Duration) -> PendingIntent {
        PendingIntent {
            intent_id: Uuid::new_v4(),
            operation: op.to_string(),
            subject: subject.to_string(),
            proposed_bytes: None,
            proposed_mode: None,
            prior_snapshot: None,
            diff_snapshot: DryRunDiff {
                operation: op.to_string(),
                bytes_delta: 0,
                file_count_delta: 0,
                affected_paths_sample: vec![],
                human_summary: "test".to_string(),
                risk: "LOW".to_string(),
                reversible: true,
            },
            expires_at: SystemTime::now() + ttl,
        }
    }

    #[test]
    fn intent_store_register_and_take_round_trip() {
        let store = IntentStore::new(INTENT_TTL);
        let intent = make_intent("fs.delete", "/tmp/x", INTENT_TTL);
        let id = store.register(intent.clone());
        assert_eq!(store.len(), 1);
        let taken = store.take(id, "fs.delete", "/tmp/x").unwrap();
        assert_eq!(taken.operation, "fs.delete");
        assert_eq!(taken.subject, "/tmp/x");
        assert_eq!(store.len(), 0); // consumed
    }

    #[test]
    fn intent_store_take_missing_returns_not_found() {
        let store = IntentStore::new(INTENT_TTL);
        let bogus = Uuid::new_v4();
        match store.take(bogus, "fs.delete", "/tmp/x") {
            Err(CowError::NotFound(id)) => assert_eq!(id, bogus),
            other => panic!("expected NotFound, got {:?}", other),
        }
    }

    #[test]
    fn intent_store_take_expired_rejects() {
        let store = IntentStore::new(Duration::from_millis(1));
        let intent = make_intent("fs.delete", "/tmp/x", Duration::from_millis(1));
        let id = store.register(intent);
        std::thread::sleep(Duration::from_millis(10));
        match store.take(id, "fs.delete", "/tmp/x") {
            Err(CowError::Expired(_, _)) => {}
            other => panic!("expected Expired, got {:?}", other),
        }
    }

    #[test]
    fn intent_store_take_op_mismatch_rejects() {
        let store = IntentStore::new(INTENT_TTL);
        let intent = make_intent("fs.delete", "/tmp/x", INTENT_TTL);
        let id = store.register(intent);
        match store.take(id, "fs.write", "/tmp/x") {
            Err(CowError::OperationMismatch { expected, actual }) => {
                assert_eq!(expected, "fs.delete");
                assert_eq!(actual, "fs.write");
            }
            other => panic!("expected OperationMismatch, got {:?}", other),
        }
    }

    #[test]
    fn intent_store_take_subject_mismatch_rejects() {
        let store = IntentStore::new(INTENT_TTL);
        let intent = make_intent("fs.delete", "/tmp/x", INTENT_TTL);
        let id = store.register(intent);
        match store.take(id, "fs.delete", "/etc/passwd") {
            Err(CowError::SubjectMismatch { expected, actual }) => {
                assert_eq!(expected, "/tmp/x");
                assert_eq!(actual, "/etc/passwd");
            }
            other => panic!("expected SubjectMismatch, got {:?}", other),
        }
    }

    #[test]
    fn intent_store_replay_after_take_fails() {
        let store = IntentStore::new(INTENT_TTL);
        let intent = make_intent("fs.delete", "/tmp/x", INTENT_TTL);
        let id = store.register(intent);
        store.take(id, "fs.delete", "/tmp/x").unwrap();
        assert!(matches!(
            store.take(id, "fs.delete", "/tmp/x"),
            Err(CowError::NotFound(_))
        ));
    }

    #[test]
    fn intent_store_register_gcs_expired() {
        let store = IntentStore::new(INTENT_TTL);
        // Register one that expires immediately.
        store.register(make_intent("fs.delete", "/tmp/old", Duration::from_millis(1)));
        std::thread::sleep(Duration::from_millis(10));
        // Registering a fresh one should trigger GC and drop the stale entry.
        store.register(make_intent("fs.delete", "/tmp/new", INTENT_TTL));
        assert_eq!(store.len(), 1);
    }

    #[test]
    fn intent_store_gc_manual() {
        let store = IntentStore::new(Duration::from_millis(1));
        for i in 0..5 {
            store.register(make_intent("fs.delete", &format!("/tmp/{}", i), Duration::from_millis(1)));
        }
        std::thread::sleep(Duration::from_millis(10));
        store.gc();
        assert_eq!(store.len(), 0);
    }

    // --- register_intent + intent_store() convenience ---

    #[test]
    fn register_intent_uses_process_store() {
        // Serialization guard: this test shares the process-wide store with
        // any parallel test invocation; we only assert our own id survives
        // long enough to be `take`n by us.
        let diff = DryRunDiff {
            operation: "fs.delete".to_string(),
            bytes_delta: -100,
            file_count_delta: -1,
            affected_paths_sample: vec![],
            human_summary: "test".to_string(),
            risk: "LOW".to_string(),
            reversible: false,
        };
        let id = register_intent("fs.delete", "/tmp/rit-test", diff, None, None, None);
        let taken = intent_store()
            .take(id, "fs.delete", "/tmp/rit-test")
            .expect("register_intent id must be takeable");
        assert_eq!(taken.operation, "fs.delete");
        assert_eq!(taken.subject, "/tmp/rit-test");
    }

    #[test]
    fn register_intent_fs_write_carries_bytes_and_mode() {
        let diff = DryRunDiff {
            operation: "fs.write".to_string(),
            bytes_delta: 5,
            file_count_delta: 1,
            affected_paths_sample: vec![],
            human_summary: "test".to_string(),
            risk: "MED".to_string(),
            reversible: true,
        };
        let id = register_intent(
            "fs.write",
            "/etc/rit-write-test",
            diff,
            Some(b"hello".to_vec()),
            Some(0o600),
            None,
        );
        let taken = intent_store()
            .take(id, "fs.write", "/etc/rit-write-test")
            .expect("takeable");
        assert_eq!(taken.proposed_bytes.as_deref(), Some(&b"hello"[..]));
        assert_eq!(taken.proposed_mode, Some(0o600));
    }

    // The `Write` import is used indirectly by TempDir tests writing binary
    // content; keep it in scope so future test additions don't re-import.
    #[allow(dead_code)]
    fn _write_marker(_: &mut dyn Write) {}
}
