/// process.rs — Read-only process inspection tools.
///
/// Reads from /proc only. No signals sent, no processes killed.
/// Tier 0 — auto-execute, no HITL prompt needed.
///
/// Parsing is split from IO so the /proc/PID/stat and /proc/PID/status parsers
/// are unit-testable on any OS.
use anyhow::Result;
use serde_json::{json, Value};
use std::fs;

pub async fn list() -> Result<Value> {
    let mut procs = Vec::new();

    let proc_dir = fs::read_dir("/proc")?;
    for entry in proc_dir {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy();

        // Only numeric directories are PIDs
        let pid: u32 = match name_str.parse() {
            Ok(n) => n,
            Err(_) => continue,
        };

        if let Ok(info) = read_proc_info(pid) {
            procs.push(info);
        }
    }

    procs.sort_by(|a, b| {
        let a_cpu = a["cpu_percent"].as_f64().unwrap_or(0.0);
        let b_cpu = b["cpu_percent"].as_f64().unwrap_or(0.0);
        b_cpu.partial_cmp(&a_cpu).unwrap_or(std::cmp::Ordering::Equal)
    });

    Ok(json!({
        "count": procs.len(),
        "processes": procs,
    }))
}

pub async fn inspect(pid: u32) -> Result<Value> {
    let info = read_proc_info(pid)?;
    let cmdline = read_cmdline(pid)?;
    let status = read_status(pid)?;
    let fds = count_fds(pid);

    Ok(json!({
        "pid": pid,
        "info": info,
        "cmdline": cmdline,
        "status": status,
        "open_fds": fds,
    }))
}

// ── IO wrappers ───────────────────────────────────────────────────────────────

fn read_proc_info(pid: u32) -> Result<Value> {
    let stat_content = fs::read_to_string(format!("/proc/{}/stat", pid))?;
    let uptime_content = fs::read_to_string("/proc/uptime").unwrap_or_else(|_| "1.0".into());
    let page_size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) } as u64;
    let ticks_per_sec = unsafe { libc::sysconf(libc::_SC_CLK_TCK) } as f64;
    parse_proc_info(pid, &stat_content, &uptime_content, page_size, ticks_per_sec)
}

fn read_cmdline(pid: u32) -> Result<String> {
    let bytes = fs::read(format!("/proc/{}/cmdline", pid))?;
    Ok(parse_cmdline(&bytes))
}

fn read_status(pid: u32) -> Result<Value> {
    let content = fs::read_to_string(format!("/proc/{}/status", pid))?;
    Ok(parse_status(&content))
}

fn count_fds(pid: u32) -> usize {
    fs::read_dir(format!("/proc/{}/fd", pid))
        .map(|entries| entries.count())
        .unwrap_or(0)
}

// ── Pure parsers (testable on any OS) ─────────────────────────────────────────

/// Parse /proc/PID/stat. The `comm` field is wrapped in parens and may itself
/// contain spaces and parens — that's why we anchor on the FIRST '(' and the
/// LAST ')' rather than splitting whitespace from the start.
///
/// `uptime_content` is /proc/uptime's content; `page_size` and `ticks_per_sec`
/// come from sysconf — passed in so the parser stays pure.
fn parse_proc_info(
    pid: u32,
    stat_content: &str,
    uptime_content: &str,
    page_size: u64,
    ticks_per_sec: f64,
) -> Result<Value> {
    let comm_start = stat_content.find('(').ok_or_else(|| anyhow::anyhow!("Malformed stat: no '('"))?;
    let comm_end = stat_content.rfind(')').ok_or_else(|| anyhow::anyhow!("Malformed stat: no ')'"))?;
    if comm_end <= comm_start {
        anyhow::bail!("Malformed stat: ')' before '('");
    }
    let comm = &stat_content[comm_start + 1..comm_end];

    // After the closing paren there's a space, then space-separated fields.
    let after = stat_content.get(comm_end + 2..).unwrap_or("");
    let rest: Vec<&str> = after.split_whitespace().collect();

    let state = rest.first().copied().unwrap_or("?");
    let ppid: u32 = rest.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    // utime is field index 11 from `rest` (which starts after the `state` field
    // we already extracted as rest[0]). See man 5 proc, section /proc/PID/stat.
    let utime: u64 = rest.get(11).and_then(|s| s.parse().ok()).unwrap_or(0);
    let stime: u64 = rest.get(12).and_then(|s| s.parse().ok()).unwrap_or(0);
    let vsize_bytes: u64 = rest.get(20).and_then(|s| s.parse().ok()).unwrap_or(0);
    let rss_pages: i64 = rest.get(21).and_then(|s| s.parse().ok()).unwrap_or(0);

    let rss_mb = (rss_pages.max(0) as u64 * page_size) / 1_048_576;
    let vsize_mb = vsize_bytes / 1_048_576;

    let uptime_secs: f64 = uptime_content
        .split_whitespace()
        .next()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1.0);
    let uptime_secs = uptime_secs.max(1.0); // avoid div-by-zero
    let total_ticks = (utime + stime) as f64;
    let cpu_pct = (total_ticks / ticks_per_sec / uptime_secs * 100.0 * 10.0).round() / 10.0;

    Ok(json!({
        "pid": pid,
        "name": comm,
        "state": state,
        "ppid": ppid,
        "cpu_percent": cpu_pct,
        "rss_mb": rss_mb,
        "vsize_mb": vsize_mb,
    }))
}

fn parse_cmdline(bytes: &[u8]) -> String {
    bytes
        .split(|&b| b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect::<Vec<_>>()
        .join(" ")
}

fn parse_status(content: &str) -> Value {
    let mut map = serde_json::Map::new();
    for line in content.lines() {
        let parts: Vec<&str> = line.splitn(2, ':').collect();
        if parts.len() == 2 {
            map.insert(
                parts[0].trim().to_lowercase(),
                Value::String(parts[1].trim().to_string()),
            );
        }
    }
    Value::Object(map)
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // A typical /proc/PID/stat line. Fields per `man 5 proc`:
    // pid (comm) state ppid pgrp session tty_nr tpgid flags minflt cminflt
    // majflt cmajflt utime stime cutime cstime priority nice num_threads
    // itrealvalue starttime vsize rss ...
    fn simple_stat() -> &'static str {
        "1234 (bash) S 1 1234 1234 0 -1 4194304 1000 100 0 0 \
         5000 2000 10 5 20 0 1 0 100000 1048576 256 18446744073709551615 \
         1 1 0 0 0 0 0 0 65536 0 0 0 0 17 0 0 0 0 0 0\n"
    }

    #[test]
    fn parse_proc_info_basic() {
        let v = parse_proc_info(1234, simple_stat(), "100.0 50.0\n", 4096, 100.0).unwrap();
        assert_eq!(v["pid"], 1234);
        assert_eq!(v["name"], "bash");
        assert_eq!(v["state"], "S");
        assert_eq!(v["ppid"], 1);
        // utime=5000, stime=2000 → 7000 ticks; 100 ticks/sec; 100 sec uptime → 70%
        assert_eq!(v["cpu_percent"], 70.0);
    }

    #[test]
    fn parse_proc_info_comm_with_spaces_and_parens() {
        // Process named "my (proc) name" — contains both spaces and parens.
        let stat = "5 (my (proc) name) R 1 5 5 0 -1 0 0 0 0 0 \
                    100 50 0 0 20 0 1 0 200 4096 32 0 \
                    0 0 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0\n";
        let v = parse_proc_info(5, stat, "1000.0\n", 4096, 100.0).unwrap();
        assert_eq!(v["name"], "my (proc) name");
        assert_eq!(v["state"], "R");
    }

    #[test]
    fn parse_proc_info_no_open_paren_errors() {
        assert!(parse_proc_info(1, "1234 nosuchcomm S\n", "1.0", 4096, 100.0).is_err());
    }

    #[test]
    fn parse_proc_info_no_close_paren_errors() {
        assert!(parse_proc_info(1, "1234 (bash S 1\n", "1.0", 4096, 100.0).is_err());
    }

    #[test]
    fn parse_proc_info_div_zero_uptime_protected() {
        let v = parse_proc_info(1234, simple_stat(), "0.0", 4096, 100.0).unwrap();
        // Uptime clamped to 1.0; should not panic, value is finite
        assert!(v["cpu_percent"].as_f64().unwrap().is_finite());
    }

    #[test]
    fn parse_proc_info_empty_uptime_uses_default() {
        let v = parse_proc_info(1234, simple_stat(), "", 4096, 100.0).unwrap();
        assert!(v["cpu_percent"].as_f64().unwrap().is_finite());
    }

    #[test]
    fn parse_cmdline_normal() {
        let bytes = b"/usr/bin/python3\0script.py\0--flag\0";
        assert_eq!(parse_cmdline(bytes), "/usr/bin/python3 script.py --flag");
    }

    #[test]
    fn parse_cmdline_empty() {
        assert_eq!(parse_cmdline(b""), "");
    }

    #[test]
    fn parse_cmdline_kernel_thread() {
        // Kernel threads typically have empty cmdline
        assert_eq!(parse_cmdline(b"\0"), "");
    }

    #[test]
    fn parse_cmdline_no_trailing_null() {
        let bytes = b"foo\0bar";
        assert_eq!(parse_cmdline(bytes), "foo bar");
    }

    #[test]
    fn parse_status_normal() {
        let s = "Name:\tbash\n\
                 Umask:\t0022\n\
                 State:\tS (sleeping)\n\
                 Tgid:\t1234\n";
        let v = parse_status(s);
        assert_eq!(v["name"], "bash");
        assert_eq!(v["state"], "S (sleeping)");
        assert_eq!(v["tgid"], "1234");
    }

    #[test]
    fn parse_status_empty() {
        let v = parse_status("");
        assert!(v.as_object().unwrap().is_empty());
    }

    #[test]
    fn parse_status_keys_lowercased() {
        // Keys are uppercased in /proc/PID/status; we lowercase them for consistency.
        let s = "Name:\tinit\n";
        let v = parse_status(s);
        assert!(v.get("name").is_some());
        assert!(v.get("Name").is_none());
    }

    #[test]
    fn parse_status_handles_lines_without_colon() {
        let s = "Name:\tbash\nweird-line-no-colon\nState:\tR\n";
        let v = parse_status(s);
        assert_eq!(v["name"], "bash");
        assert_eq!(v["state"], "R");
    }
}
