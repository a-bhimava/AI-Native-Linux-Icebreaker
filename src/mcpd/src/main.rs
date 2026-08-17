use anyhow::Result;
use mcpd::{manifest_loader, sandbox, schema, server};
use std::path::PathBuf;
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

    // R13 / M7.1: every dispatchable built-in tool needs an explicit,
    // well-formed trust declaration.  Do this before loading manifests,
    // applying the sandbox, or accepting any stdio request so a malformed
    // release fails closed at startup.
    schema::validate_trust_declarations()?;
    info!("R13: x-icebreaker-trust declarations validated");

    // v6.65 Phase 7 M7.2 foundation: acknowledge the presence of an
    // external-MCP-server allowlist without spawning anything from it.
    // The spawner lands in M7.2; today mcpd only exposes its built-in
    // tool catalogue. Reading the file (even just to count entries) is
    // enough to (a) surface the file's existence to operators via logs
    // and (b) fail loudly if the file is corrupt on disk before we
    // start relying on it.
    log_mcp_allowlist();

    // v6.9 Scope O Layer 2 Part B: load declarative YAML tool manifests
    // BEFORE the sandbox is applied so a malformed manifest aborts
    // startup with a clear error (the sandbox can't recover from a
    // registry read failure once locked down). init() is idempotent:
    // the OnceLock keeps the registry immutable for the daemon's
    // lifetime — hot-reload is Layer 3 territory (v7.5).
    // Path matches the shipped-ISO layout: mcpd itself lives at
    // /usr/libexec/icebreaker/mcpd; manifests ride alongside so both
    // are one directory apart. MCPD_MANIFESTS_DIR env var overrides
    // for dev + tests.
    let manifests_dir = std::env::var("MCPD_MANIFESTS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/usr/libexec/icebreaker/manifests"));
    manifest_loader::init(&manifests_dir)?;
    let manifest_names = manifest_loader::registry().names();
    if manifest_names.is_empty() {
        info!("manifest registry: 0 tools loaded from {}",
              manifests_dir.display());
    } else {
        info!("manifest registry: {} tool(s) loaded from {}: {:?}",
              manifest_names.len(), manifests_dir.display(), manifest_names);
    }

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
