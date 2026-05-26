use anyhow::Result;
use tracing::info;

mod server;
mod tools;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            std::env::var("RUST_LOG").unwrap_or_else(|_| "mcpd=info".to_string()),
        )
        .with_writer(std::io::stderr) // logs to stderr; stdout is reserved for JSON-RPC
        .init();

    info!("mcpd starting — stdio JSON-RPC 2.0 server");
    info!("CRITICAL: No network listeners will be opened (INV-3)");

    server::run_stdio_server().await
}
