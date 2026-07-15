//! v6.9 Scope O Layer 2 Part B — impl.kind dispatchers.
//!
//! Each impl.kind exports one `dispatch(manifest, params) -> Result<Value>`
//! function. The dispatcher receives a fully-validated manifest and
//! params (params were checked against `manifest.compiled_schema` by
//! the caller — INV-4). It returns the JSON value the tool would
//! normally return; the caller wraps it into JsonRpcResponse::ok.
//!
//! Failure modes:
//! - **Missing dispatcher**: three of the four Part-B kinds
//!   (fs_write_cow, dbus_call, exec_pipeline) are STUBBED in the
//!   initial cut. They accept validation at load time (so the manifest
//!   registry doesn't silently disable them) but refuse at dispatch
//!   with a clear "not yet implemented" error. This surfaces the
//!   design intent — a manifest declaring `exec_pipeline` will parse
//!   and register, and the operator will get an actionable error the
//!   first time it's invoked, not a random panic.
//! - **impl-specific error** (e.g. fs_read hits a missing file):
//!   bubbles up as `anyhow::Error`; the dispatch caller in server.rs
//!   converts to JSON-RPC -32603.

pub mod fs_read;

use anyhow::{bail, Result};
use serde_json::Value;

use super::{ImplKind, Manifest};


/// Route to the impl.kind's dispatcher. `params` MUST have been
/// validated against `manifest.compiled_schema` by the caller (INV-4).
pub async fn dispatch(manifest: &Manifest, params: &Value) -> Result<Value> {
    match manifest.imp.kind {
        ImplKind::FsRead => fs_read::dispatch(manifest, params).await,
        ImplKind::FsWriteCow => bail!(
            "manifest_loader: impl.kind fs_write_cow is not yet implemented \
             (Layer 2 Part B ships fs_read; fs_write_cow lands in v6.10 alongside \
             the COW dry-run bridge)"
        ),
        ImplKind::DbusCall => bail!(
            "manifest_loader: impl.kind dbus_call is not yet implemented \
             (Layer 2 Part B ships fs_read; dbus_call lands in v6.10)"
        ),
        ImplKind::ExecPipeline => bail!(
            "manifest_loader: impl.kind exec_pipeline is not yet implemented \
             (execve is not in the base seccomp allowlist; widening it is a \
             separate, deliberate decision — deferred beyond Layer 2 Part B)"
        ),
    }
}
