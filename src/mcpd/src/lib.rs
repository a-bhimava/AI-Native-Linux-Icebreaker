//! mcpd — library crate.
//!
//! The Phase 1 binary at `src/main.rs` is a thin shell around this library:
//! it boots tracing, applies the kernel sandbox, then hands control to
//! `server::run_stdio_server()`.
//!
//! The library exists so that `fuzz/` (cargo-fuzz target on
//! `tools::fs::validate`) and any future out-of-tree consumers can link
//! against mcpd's internals without depending on the binary entry point.
//! No public surface is added beyond what `pub` already declared in the
//! individual modules.

pub mod audit;
pub mod manifest_loader;
pub mod sandbox;
pub mod schema;
pub mod server;
pub mod tools;
