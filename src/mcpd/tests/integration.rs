//! Integration tests for mcpd.
//!
//! Spawns the mcpd binary, pipes JSON-RPC requests to its stdin, and reads
//! responses from its stdout. The stdio protocol is what the Controller will
//! use, so these tests exercise the real wire format.
//!
//! Tests that don't touch /proc run on any OS (macOS dev loop). Tests that
//! depend on /proc are gated with #[cfg(target_os = "linux")].

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

/// A handle to a running mcpd subprocess plus line-buffered stdio.
struct Mcpd {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Mcpd {
    fn spawn() -> Self {
        let bin = env!("CARGO_BIN_EXE_mcpd");
        let mut child = Command::new(bin)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null()) // suppress tracing logs
            .spawn()
            .expect("spawn mcpd");
        let stdin = child.stdin.take().expect("stdin");
        let stdout = BufReader::new(child.stdout.take().expect("stdout"));
        Self { child, stdin, stdout }
    }

    /// Send a JSON-RPC request and parse one response line.
    fn call(&mut self, req: &Value) -> Value {
        let line = req.to_string() + "\n";
        self.stdin.write_all(line.as_bytes()).expect("write");
        self.stdin.flush().expect("flush");
        let mut resp_line = String::new();
        self.stdout.read_line(&mut resp_line).expect("read");
        serde_json::from_str(&resp_line).expect("parse response")
    }

    /// Send a raw line (may be malformed JSON).
    fn call_raw(&mut self, raw: &str) -> Value {
        self.stdin.write_all(raw.as_bytes()).expect("write");
        self.stdin.write_all(b"\n").expect("write");
        self.stdin.flush().expect("flush");
        let mut resp_line = String::new();
        self.stdout.read_line(&mut resp_line).expect("read");
        serde_json::from_str(&resp_line).expect("parse response")
    }
}

impl Drop for Mcpd {
    fn drop(&mut self) {
        // EOF triggers graceful shutdown; if that doesn't work, kill it.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

// ── Tests that work on any OS ─────────────────────────────────────────────────

#[test]
fn tools_list_returns_catalogue() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    }));
    assert_eq!(resp["jsonrpc"], "2.0");
    assert_eq!(resp["id"], 1);
    let tools = resp["result"]["tools"].as_array().expect("tools array");
    assert!(!tools.is_empty(), "expected at least one tool");
    // Every advertised tool must have name + category.
    for t in tools {
        assert!(t["name"].is_string());
        assert!(t["category"].is_string());
    }
}

#[test]
fn unknown_method_returns_minus_32601() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "does.not.exist",
        "id": 7,
    }));
    assert_eq!(resp["error"]["code"], -32601);
    assert_eq!(resp["id"], 7);
}

#[test]
fn malformed_json_returns_minus_32700() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call_raw("{ this is not json");
    assert_eq!(resp["error"]["code"], -32700);
    // Spec: id is null when the request couldn't be parsed.
    assert!(resp["id"].is_null());
}

#[test]
fn wrong_jsonrpc_version_returns_minus_32600() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "1.0",
        "method": "tools/list",
        "id": 3,
    }));
    assert_eq!(resp["error"]["code"], -32600);
    assert_eq!(resp["id"], 3);
}

#[test]
fn shutdown_on_stdin_close() {
    let bin = env!("CARGO_BIN_EXE_mcpd");
    let mut child = Command::new(bin)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn mcpd");
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());

    // One round-trip.
    writeln!(stdin, "{}", json!({"jsonrpc":"2.0","method":"tools/list","id":1})).unwrap();
    stdin.flush().unwrap();
    let mut resp = String::new();
    stdout.read_line(&mut resp).unwrap();
    assert!(resp.contains("\"jsonrpc\":\"2.0\""));

    // Close stdin → mcpd reads EOF → graceful shutdown.
    drop(stdin);
    let status = child.wait().expect("wait");
    assert!(status.success(), "mcpd should exit 0 on stdin EOF, got {:?}", status);
}

// ── Linux-only tests (require /proc) ──────────────────────────────────────────

#[cfg(target_os = "linux")]
#[test]
fn system_status_on_linux() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "system.status",
        "id": 1,
    }));
    let result = &resp["result"];
    assert!(result["uptime_seconds"].as_u64().unwrap() > 0);
    assert!(result["memory_mb"]["total_mb"].as_u64().unwrap() > 0);
    assert!(result["hostname"].is_string());
}

#[test]
fn process_inspect_missing_pid_returns_invalid_params() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "process.inspect",
        "params": {},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn process_inspect_wrong_type_returns_invalid_params() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "process.inspect",
        "params": {"pid": "not-an-integer"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn extra_param_rejected_by_no_param_method() {
    // additionalProperties: false in the schema should catch this.
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "system.status",
        "params": {"unexpected": "field"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_list_carries_schema_version() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    }));
    assert!(resp["result"]["schema_version"].is_string());
}

#[test]
fn tools_list_includes_real_schemas() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    }));
    let inspect = resp["result"]["tools"].as_array().unwrap().iter()
        .find(|t| t["name"] == "process.inspect").unwrap().clone();
    assert_eq!(inspect["params_schema"]["required"][0], "pid");
}

#[cfg(target_os = "linux")]
#[test]
fn process_list_returns_self() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "process.list",
        "id": 1,
    }));
    let count = resp["result"]["count"].as_u64().unwrap();
    assert!(count > 0);
}

// ── fs.* tests (work on both Linux and macOS since /tmp is in the whitelist) ─

#[test]
fn fs_read_rejects_path_outside_whitelist() {
    let mut mcpd = Mcpd::spawn();
    // /boot is not under any whitelisted root. Schema accepts it (it's just
    // a string), so the failure surfaces as -32603 from validate() inside the
    // tool function — not -32602 from the schema layer.
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.read",
        "params": {"path": "/boot/vmlinuz"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32603);
    assert!(resp["error"]["message"].as_str().unwrap().contains("whitelisted"));
}

#[test]
fn fs_read_rejects_relative_path() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.read",
        "params": {"path": "etc/hosts"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32603);
}

#[test]
fn fs_read_rejects_nul_byte() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.read",
        "params": {"path": "/etc/hosts\u{0000}/passwd"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32603);
}

#[test]
fn fs_read_round_trip_with_tempfile() {
    use std::io::Write;
    let dir = std::env::temp_dir();
    let unique = format!("mcpd_test_{}.txt", std::process::id());
    let path = dir.join(&unique);
    {
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(b"hello mcpd\n").unwrap();
    }
    let abs = path.to_str().unwrap().to_string();

    // Skip if the temp dir isn't under our whitelist (e.g. macOS uses /var/folders/...
    // which is symlinked to /private/var/folders/... — neither is in the v1 whitelist).
    let whitelisted = abs.starts_with("/tmp/") || abs.starts_with("/var/log/");
    if !whitelisted {
        let _ = std::fs::remove_file(&path);
        eprintln!("skipping: temp dir {} is not under whitelist root", abs);
        return;
    }

    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.read",
        "params": {"path": abs},
        "id": 1,
    }));

    let _ = std::fs::remove_file(&path);

    assert!(resp["error"].is_null(), "expected ok response, got error: {:?}", resp["error"]);
    assert_eq!(resp["result"]["content"], "hello mcpd\n");
    assert_eq!(resp["result"]["size_bytes"], 11);
}

#[test]
fn fs_stat_on_etc() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.stat",
        "params": {"path": "/etc"},
        "id": 1,
    }));
    // /etc exists on both Linux and macOS.
    assert!(resp["error"].is_null());
    assert_eq!(resp["result"]["is_dir"], true);
}

#[test]
fn fs_list_on_etc() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.list",
        "params": {"path": "/etc"},
        "id": 1,
    }));
    assert!(resp["error"].is_null());
    let entries = resp["result"]["entries"].as_array().unwrap();
    assert!(!entries.is_empty());
    // Sorted ascending.
    let names: Vec<_> = entries.iter()
        .map(|e| e["name"].as_str().unwrap())
        .collect();
    let mut sorted = names.clone();
    sorted.sort();
    assert_eq!(names, sorted);
}

#[test]
fn fs_read_missing_path_is_invalid_params() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.read",
        "params": {},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn fs_read_rejects_extra_field() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.read",
        "params": {"path": "/etc/hosts", "binary": true},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

// ── M1.5: fs.write + fs.delete + COW gate (INV-6) ────────────────────────────

#[test]
fn fs_delete_always_returns_cow_gate() {
    // Per the catalogue, fs.delete never executes synchronously in Phase 1.
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.delete",
        "params": {"path": "/tmp/whatever"},
        "id": 1,
    }));
    assert!(resp["error"].is_null(), "expected ok result, got {:?}", resp["error"]);
    assert_eq!(resp["result"]["status"], "requires_cow_approval");
    let intent_id = resp["result"]["intent_id"].as_str().unwrap();
    assert!(intent_id.len() >= 32, "intent_id should be a UUID-ish string");
    assert_eq!(resp["result"]["preview"]["operation"], "fs.delete");
}

#[test]
fn fs_write_outside_home_returns_cow_gate() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.write",
        "params": {"path": "/etc/hosts.test", "content": "x"},
        "id": 1,
    }));
    assert!(resp["error"].is_null(), "expected ok, got {:?}", resp["error"]);
    assert_eq!(resp["result"]["status"], "requires_cow_approval");
    assert_eq!(resp["result"]["preview"]["operation"], "fs.write");
    assert_eq!(resp["result"]["preview"]["proposed_size_bytes"], 1);
}

#[test]
fn fs_write_outside_whitelist_returns_error() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.write",
        "params": {"path": "/boot/foo", "content": "x"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32603);
}

#[test]
fn fs_write_missing_content_is_invalid_params() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.write",
        "params": {"path": "/tmp/x"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn fs_write_rejects_bad_mode() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "fs.write",
        "params": {"path": "/tmp/x", "content": "x", "mode": 99999},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

// ── M1.6: service.* via D-Bus (graceful degradation) ─────────────────────────

#[test]
fn service_start_invalid_unit_rejected_by_schema() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "service.start",
        "params": {"unit": "nginx; rm -rf /"},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602, "schema pattern should reject shell metachars");
}

#[test]
fn service_start_missing_unit_rejected() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "service.start",
        "params": {},
        "id": 1,
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn service_start_returns_unavailable_when_bus_missing() {
    // On macOS there's no systemd → unavailable. On Linux in dev, the system
    // bus may exist but cron.service may not be installed; in that case we
    // accept either "ok", "err", or "unavailable" as a healthy response shape.
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "service.start",
        "params": {"unit": "cron.service"},
        "id": 1,
    }));
    assert!(resp["error"].is_null(), "graceful degradation should not bubble -32603");
    let status = resp["result"]["status"].as_str().unwrap();
    assert!(
        matches!(status, "ok" | "err" | "unavailable"),
        "got unexpected status: {}",
        status
    );
}

#[test]
fn service_logs_advertises_default_lines() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    }));
    let logs = resp["result"]["tools"].as_array().unwrap().iter()
        .find(|t| t["name"] == "service.logs").unwrap().clone();
    assert_eq!(logs["params_schema"]["properties"]["lines"]["default"], 200);
}
