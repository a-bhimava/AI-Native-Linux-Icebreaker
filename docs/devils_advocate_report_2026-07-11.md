# Daily Devil's Advocate Report - 2026-07-11

### Issue #1: `$HOME` Fallback in `fs.rs` Opens the Entire Filesystem
* **Category:** Code/Architecture
* **Location:** `src/mcpd/src/tools/fs.rs`, line 60; `src/mcpd/src/sandbox/landlock.rs`, line 41
* **The Flaw:** `validate()` constructs the allowed-root whitelist with `std::env::var("HOME").unwrap_or_else(|_| "/".into())`. When `HOME` is unset — which is the normal case under `DynamicUser=yes` systemd units — the whitelist root becomes `/`. Every absolute path passes `validate()`, including `/etc/shadow`, `/proc/sysrq-trigger`, and `/root/.ssh`. Meanwhile `landlock.rs` falls back to `/root`, not `/`. The two layers now disagree: userspace says every path is valid, Landlock allows only `/root`. This is TOCTOU-flavored false assurance — the userspace check that is supposed to reject dangerous paths has silently become a pass-through, and the Landlock kernel check carries the full load alone. One layer of defense has been deleted at deployment time with zero log output.
* **The Fix:** Replace the fallback with an explicit failure: `std::env::var("HOME").map_err(|_| anyhow!("HOME is unset; refusing to start without a defined filesystem root"))?`. Align `landlock.rs` to use the same computed root, and add a CI assertion (`ci.sh` G1 block) that verifies mcpd exits non-zero when `HOME` is unset.

---

### Issue #2: 13 `expect("schema-validated")` Panics in the Production JSON-RPC Dispatcher
* **Category:** Code/Architecture
* **Location:** `src/mcpd/src/server.rs`, lines 205, 216, 223, 235–237, 251, 253, 261, 274, 280, 284, 288
* **The Flaw:** CLAUDE.md's Forbidden Patterns section explicitly prohibits `unwrap()` on security-critical paths. `dispatch()` is the live stdio request handler and uses `.expect("schema-validated")` in at least 13 places to coerce JSON values to Rust types. JSON Schema validates structural constraints (`required`, `minimum`, pattern), but `.as_str()` and `.as_u64()` check Rust's internal JSON enum variant — a field that satisfies `required` but carries the wrong JSON type (e.g., an integer where a string is expected) passes schema validation and then panics the server. A panic in the stdio JSON-RPC process kills mcpd, severs the controller's tool execution channel, and constitutes a complete denial of service against the AI's ability to take any action. This is not theoretical: JSON type confusion is a well-known fuzzing finding in RPC services.
* **The Fix:** Replace every `.expect("schema-validated")` with explicit `match` or `.ok_or_else(|| McpdError::InvalidParam(...))` returning a JSON-RPC error response, not a process abort. Add a fuzz corpus in `tests/fuzz/` that sends type-confused requests to `dispatch()`.

---

### Issue #3: Post-Validation Mutation of Intent Dict Violates INV-2
* **Category:** Conflicts
* **Location:** `dual-brain/controller/main.py`, lines 534–537; `schemas/intent.json` (`additionalProperties: false`)
* **The Flaw:** INV-2 states the Controller must "reject any Intent Object with fields outside the defined schema." The schema declares `additionalProperties: false`. After `validate(raw_intent)` accepts the object, `main.py` immediately mutates it by injecting `target_realpath` — a field not in the schema. The validated dict is then passed to `intent_store.put()` with this extra field intact. The invariant's guarantee is broken the moment the function that enforces it returns: the "validated" dict is no longer schema-compliant. Additionally `os.path.realpath()` is called here and again in `risk_classifier.py`, creating two separate TOCTOU windows on the same symlink-resolvable path.
* **The Fix:** Never mutate the validated intent dict. Compute `target_realpath` separately and carry it in a parallel `ResolvedContext` dataclass that is explicitly not the Intent Object. Remove the duplicate `realpath` call from `risk_classifier.py` and thread the already-resolved path through instead.

---

### Issue #4: HITL EXPLAIN Loop Resets the Full Timeout on Every Iteration — Infinite Gate Extension
* **Category:** Code/Architecture
* **Location:** `dual-brain/controller/hitl.py`, lines 508–511
* **The Flaw:** BP-4 requires the HITL gate to enforce a definitive yes/no within the configured timeout window using a monotonic clock. The `while decision == Decision.EXPLAIN` loop calls `read_decision(self._timeout_seconds)` with the full original timeout on each iteration. Pressing the EXPLAIN key resets the countdown to 30 seconds from scratch. An attacker or a stuck keyboard macro that continuously sends the EXPLAIN keypress keeps the approval gate open indefinitely — every EXPLAIN response buys another 30 seconds. The `decision_latency_ms` measurement accumulates across explains, so the telemetry grossly understates actual gate-open time. This directly violates BP-4's "monotonic clock" requirement.
* **The Fix:** Compute a `deadline = time.monotonic() + self._timeout_seconds` before the loop. On each iteration pass `remaining = max(0.0, deadline - time.monotonic())` to `read_decision()`. If `remaining <= 0`, break and return `Decision.DENY`. This makes the gate non-renewable regardless of EXPLAIN hammering.

---

### Issue #5: `build.sh` Final Stage Hardcodes a Specific Developer's macOS Home Directory
* **Category:** Code/Architecture
* **Location:** `cx-distro/build.sh`, lines 855–859
* **The Flaw:** The ISO build script's final stage moves the built ISO to `/Users/aditya/Documents/Icebreaker/ISO/icebreaker_full_ubuntu.iso`. The script opens with `set -euo pipefail` (line 28). On every Linux system — Docker CI, GCP VM, bare metal — `/Users/` does not exist. `mkdir -p` on that path fails, `set -e` aborts the entire 6-stage build after it has already spent CPU on the chroot and ISO construction. The SHA-256 hash file is written to the temp build directory before this move, so the hash references a path that no longer exists. This is not an edge case: it is the production build target. Every ISO build outside the original developer's macOS laptop has been broken since this line was written.
* **The Fix:** Replace the hardcoded path with `ISO_DEST="${ISO_OUTPUT_DIR:-$(pwd)/output}/icebreaker_full_ubuntu.iso"`. Document `ISO_OUTPUT_DIR` in `cx-distro/README.md`. Add a smoke test in CI that runs `build.sh --dry-run` in a Docker container and asserts the ISO path resolves to a writable location.

---

### Issue #6: `MCPD_SECCOMP_LOG_ONLY` Disables Kernel Syscall Enforcement With No CI Gate
* **Category:** Conflicts
* **Location:** `src/mcpd/src/sandbox/seccomp.rs`, lines 42–55, 89; `src/mcpd/ci.sh`, line 187
* **The Flaw:** `MCPD_SECCOMP_LOG_ONLY=1` switches the seccomp filter's default action from `KillProcess` to `SeccompAction::Log`, effectively disabling the syscall sandbox — unknown syscalls execute and are merely written to the kernel log. CLAUDE.md and ci.sh both acknowledge the parallel risk of `MCPD_FS_TEST_ROOTS` and gate its absence explicitly (ci.sh line 187: `strings target/release/mcpd | grep -c MCPD_FS_TEST_ROOTS` must be 0). No equivalent gate exists for `MCPD_SECCOMP_LOG_ONLY`. A developer who sets this env var for debugging and forgets to unset it ships a production binary with a completely open syscall surface. The only signal is a `warn!()` log line that is indistinguishable from routine startup noise.
* **The Fix:** Add a CI gate alongside G3: `env MCPD_SECCOMP_LOG_ONLY=1 ./mcpd --dry-run 2>&1 | grep -q "REFUSING TO START" || fail "seccomp log-only mode must refuse to start in release builds"`. Add an `#[cfg(not(debug_assertions))]` compile-time check that makes `MCPD_SECCOMP_LOG_ONLY=1` in a release binary emit a startup error and exit.

---

### Issue #7: `LandlockStatus::PartiallyEnforced` Logs a Warning Instead of Aborting — INV-5 Lie
* **Category:** Conflicts
* **Location:** `src/mcpd/src/sandbox/landlock.rs`, lines 102–104; CLAUDE.md INV-5
* **The Flaw:** The module-level doc comment on `landlock.rs` states: "If Landlock is unavailable (kernel < 5.13), the process hard-exits." INV-5 in CLAUDE.md doubles down: "If Landlock is unavailable, mcpd MUST exit with a clear error — never run without it." `PartiallyEnforced` means the kernel accepted the Landlock ruleset but silently dropped access-type flags it does not support — the filesystem sandbox is weaker than declared without the process or the user knowing. The code handles this with `warn!("landlock: only partially enforced") + Ok(())`. mcpd continues to run advertising full sandbox enforcement while delivering something weaker. This is a documentation-to-code conflict that creates a false security guarantee: the audit log says "sandbox applied"; the kernel applied something less than what was requested.
* **The Fix:** Treat `PartiallyEnforced` identically to `NotEnforced` for the purpose of the INV-5 hard-exit decision. Add a new `[landlock] allow_partial = false` config knob (default off) that an operator can set to `true` with explicit acknowledgement, per BP-2 (feature-flag new behavior, default to safe path).

---

### Issue #8: Missing Schema File Silently Bypasses All Parameter Validation — INV-4 Defeated
* **Category:** Conflicts
* **Location:** `dual-brain/controller/main.py`, lines 2002–2024
* **The Flaw:** INV-4 states "every MCP tool call parameter MUST be validated against the tool's JSON Schema before the tool executes." `_get_tool_schema()` returns `{"type": "object"}` (no `properties` key) when the schema file is missing or corrupt, swallowing any parse exception silently. `_validate_tool_call()` then gates on `if tool_schema.get("properties")`, which is `False` for the fallback dict, so `jsonschema.validate()` is never called. The Privileged Brain's tool parameters — paths, command strings, package names — go to the MCP dispatcher completely unvalidated. The invariant that makes injected shell metacharacters detectable is silently gone. This is not a theoretical edge case: `PKG-1` in CLAUDE.md documents that schema files are not automatically installed by `pip install`, meaning any deployment that missed the post-install copy step (build.sh Stage 3 or deploy.sh Step 8) runs permanently in this degraded state with no log warning.
* **The Fix:** If schema loading fails for any action, raise a hard error — do not fall back to permissive validation. Log the missing schema path at ERROR level at startup (not at call time). Add a CI gate that enumerates every known MCP action and asserts a non-empty schema file exists for each one.

---

### Issue #9: RPA Watchdog Race: Off-Track Kill Always Reported as Timeout
* **Category:** Code/Architecture
* **Location:** `dual-brain/controller/main.py`, lines 2119–2127, 2222–2226
* **The Flaw:** Two distinct kill paths (watchdog timeout and QB off-track detection) both produce `proc.returncode == -9`. The code uses a single `threading.Event` (`killed_by_watchdog`) for two logically separate purposes: "cancel the watchdog" (line 2223: unconditional `set()`) and "did the watchdog fire?" (line 2225: `is_set()` check). Line 2223 sets the event unconditionally as the cancel signal, immediately making the check on line 2225 always evaluate to `True`. Every subprocess kill — including the QB-initiated off-track kill — is therefore logged as `timed_out=True`. The result dict simultaneously carries `timed_out=True` and `qb_paused_at_keyword=N`, a contradiction. Audit entries are wrong; alerting on RPA timeouts fires for off-track events; debugging becomes impossible.
* **The Fix:** Separate the two concerns with two events: `watchdog_fired = threading.Event()` and `watchdog_cancel = threading.Event()`. The watchdog fires `watchdog_fired.set()` before killing the process. The cancel path sets `watchdog_cancel.set()`. The result check reads `rpa_result["timed_out"] = watchdog_fired.is_set()`.

---

### Issue #10: `fs.write` Silently Swallows `sync_all()` Failure — Durability Guarantee Is a Lie
* **Category:** Code/Architecture
* **Location:** `src/mcpd/src/tools/fs.rs`, line ~479; `src/mcpd/src/sandbox/seccomp.rs`, line 203
* **The Flaw:** After writing file content, `fs.write` calls `file.sync_all().ok()`. `.ok()` discards the `Result`, converting any `fsync(2)` error into a silently ignored `None`. If the underlying storage returns an I/O error (disk full, failing NVM, NFS timeout), the write is not durable but the tool returns success to the controller, which logs the operation as completed and tells the user it succeeded. The seccomp allowlist explicitly permits `SYS_fsync` (seccomp.rs line 203) for the express purpose of making writes durable. That allowlist entry is meaningless if the Rust code discards the fsync result. On a COW overlay — which is how INV-6 dry-runs work — a silently failed sync means the overlay state is inconsistent with what was reported to the approval UI.
* **The Fix:** Replace `.ok()` with `?`: `file.sync_all()?;`. Add a unit test that uses a mock `Write` implementation that fails `sync_all()` and asserts the tool returns an error rather than success.
