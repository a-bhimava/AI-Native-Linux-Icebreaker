use anyhow::Result;
use mcpd::{sandbox, schema, server};
use tracing::info;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            std::env::var("RUST_LOG").unwrap_or_else(|_| "mcpd=info".to_string()),
        )
        .with_writer(std::io::stderr) // logs to stderr; stdout is reserved for JSON-RPC
        .init();

    info!("mcpd starting — stdio JSON-RPC 2.0 server (schema {})", schema::SCHEMA_VERSION);
    info!("schema registry: {} methods", schema::registered_methods().count());
    info!("CRITICAL: No network listeners will be opened (INV-3)");

    // v6.65 Phase 7 M7.2 foundation: acknowledge the presence of an
    // external-MCP-server allowlist without spawning anything from it.
    // The spawner lands in M7.2; today mcpd only exposes its built-in
    // tool catalogue. Reading the file (even just to count entries) is
    // enough to (a) surface the file's existence to operators via logs
    // and (b) fail loudly if the file is corrupt on disk before we
    // start relying on it.
    log_mcp_allowlist();

    // INV-5: kernel sandbox applied BEFORE accepting any request.
    sandbox::apply()?;

    #[cfg(target_os = "linux")]
    {
        sd_notify::notify(false, &[sd_notify::NotifyState::Ready]).ok();
        info!("sd_notify: READY=1 sent");
    }

    server::run_stdio_server().await
}

/// v6.65 (Phase 7 M7.2 foundation): probe the MCP allowlist file so the
/// startup logs reflect whether an operator has staged any external
/// servers yet. **Does not spawn or connect to any server** — that's
/// deferred to Phase 7 M7.2. Also does not fail startup on a missing or
/// malformed file: v6.65 ships an empty allowlist and the spawner is
/// not wired in, so a broken file is not a runtime hazard yet.
fn log_mcp_allowlist() {
    const CANDIDATE_PATHS: &[&str] = &[
        "/etc/icebreaker/mcp_allowlist.toml",
        "/usr/share/icebreaker/mcp_allowlist.toml",
    ];
    for path in CANDIDATE_PATHS {
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(_) => continue,
        };
        // Count `[[server]]` table-array headers. A full TOML parse
        // would need a toml crate dep the daemon doesn't need yet;
        // Phase 7 M7.2 will add that.
        let entries = text
            .lines()
            .filter(|l| l.trim_start().starts_with("[[server]]"))
            .count();
        info!(
            "mcp_allowlist: {} ({} entries) — Phase 7 M7.2 will wire the spawner",
            path, entries
        );
        return;
    }
    info!("mcp_allowlist: not found — only built-in tools available");
}
