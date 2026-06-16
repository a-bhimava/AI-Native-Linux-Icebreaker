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

    // INV-5: kernel sandbox applied BEFORE accepting any request.
    sandbox::apply()?;

    #[cfg(target_os = "linux")]
    {
        sd_notify::notify(false, &[sd_notify::NotifyState::Ready]).ok();
        info!("sd_notify: READY=1 sent");
    }

    server::run_stdio_server().await
}
