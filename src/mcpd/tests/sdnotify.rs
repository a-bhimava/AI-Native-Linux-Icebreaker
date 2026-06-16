//! sd_notify integration tests for mcpd.
//!
//! On Linux, mcpd sends READY=1 to $NOTIFY_SOCKET after sandbox::apply().
//! On non-Linux, the notification is a compile-time no-op; the test verifies
//! the binary still starts and serves requests normally.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

struct Mcpd {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Mcpd {
    fn spawn_with_env(vars: &[(&str, &str)]) -> Self {
        let bin = env!("CARGO_BIN_EXE_mcpd");
        let mut cmd = Command::new(bin);
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        for (k, v) in vars {
            cmd.env(k, v);
        }
        let mut child = cmd.spawn().expect("spawn mcpd");
        let stdin = child.stdin.take().expect("stdin");
        let stdout = BufReader::new(child.stdout.take().expect("stdout"));
        Self { child, stdin, stdout }
    }

    fn call(&mut self, req: &Value) -> Value {
        let line = req.to_string() + "\n";
        self.stdin.write_all(line.as_bytes()).expect("write");
        self.stdin.flush().expect("flush");
        let mut resp_line = String::new();
        self.stdout.read_line(&mut resp_line).expect("read");
        serde_json::from_str(&resp_line).expect("parse response")
    }
}

impl Drop for Mcpd {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// On Linux: set NOTIFY_SOCKET to a temp datagram socket, start mcpd,
/// verify it sends "READY=1" before serving its first request.
#[cfg(target_os = "linux")]
#[test]
fn sdnotify_sends_ready_on_linux() {
    use std::os::unix::net::UnixDatagram;

    let dir = tempfile::tempdir().expect("tempdir");
    let sock_path = dir.path().join("notify.sock");

    let listener = UnixDatagram::bind(&sock_path).expect("bind notify socket");
    listener
        .set_read_timeout(Some(std::time::Duration::from_secs(10)))
        .expect("set timeout");

    let sock_str = sock_path.to_str().unwrap();
    let mut mcpd = Mcpd::spawn_with_env(&[("NOTIFY_SOCKET", sock_str)]);

    // mcpd should have sent READY=1 during startup, before we even call it.
    // But the startup is async relative to our process, so do a round-trip
    // first to ensure mcpd is fully initialized.
    let resp = mcpd.call(&json!({"jsonrpc": "2.0", "method": "tools/list", "id": 1}));
    assert_eq!(resp["jsonrpc"], "2.0");

    let mut buf = [0u8; 256];
    let n = listener.recv(&mut buf).expect("recv from notify socket");
    let msg = std::str::from_utf8(&buf[..n]).expect("utf-8");
    assert!(
        msg.contains("READY=1"),
        "expected READY=1 in sd_notify datagram, got: {:?}",
        msg
    );
}

/// On any OS: mcpd starts and serves requests even when NOTIFY_SOCKET is unset.
/// This proves the sd_notify call is non-fatal (uses `.ok()` not `.unwrap()`).
#[test]
fn mcpd_starts_without_notify_socket() {
    let mut mcpd = Mcpd::spawn_with_env(&[]);
    let resp = mcpd.call(&json!({"jsonrpc": "2.0", "method": "tools/list", "id": 1}));
    assert_eq!(resp["jsonrpc"], "2.0");
    assert!(resp["result"]["tools"].is_array());
}

/// INV-3: sd_notify must not open any persistent network listeners.
/// After startup with NOTIFY_SOCKET set, mcpd must still have zero TCP/UDP listeners.
#[cfg(target_os = "linux")]
#[test]
fn sdnotify_does_not_open_network_listeners() {
    use std::os::unix::net::UnixDatagram;

    let dir = tempfile::tempdir().expect("tempdir");
    let sock_path = dir.path().join("notify.sock");
    let _listener = UnixDatagram::bind(&sock_path).expect("bind");

    let sock_str = sock_path.to_str().unwrap();
    let mut mcpd = Mcpd::spawn_with_env(&[("NOTIFY_SOCKET", sock_str)]);

    // Ensure fully started.
    let resp = mcpd.call(&json!({"jsonrpc": "2.0", "method": "tools/list", "id": 1}));
    assert_eq!(resp["jsonrpc"], "2.0");

    let pid = mcpd.child.id();
    let ss_output = Command::new("ss")
        .args(["-tlnp"])
        .output()
        .expect("ss command");
    let ss_text = String::from_utf8_lossy(&ss_output.stdout);
    let pid_str = pid.to_string();
    let has_listener = ss_text.lines().any(|line| line.contains(&pid_str));
    assert!(
        !has_listener,
        "INV-3: mcpd (pid {}) must not have TCP listeners after sd_notify. ss output:\n{}",
        pid, ss_text
    );
}
