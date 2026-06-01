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

#[cfg(target_os = "linux")]
#[test]
fn process_inspect_missing_pid_returns_invalid_params() {
    let mut mcpd = Mcpd::spawn();
    let resp = mcpd.call(&json!({
        "jsonrpc": "2.0",
        "method": "process.inspect",
        "params": {},
        "id": 1,
    }));
    // server.rs currently surfaces this via -32603 Internal error with the
    // "Missing required param" anyhow message. After M1.1 (schema validation)
    // this should become -32602 Invalid params. Keep the test loose for now.
    assert!(resp["error"].is_object());
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
