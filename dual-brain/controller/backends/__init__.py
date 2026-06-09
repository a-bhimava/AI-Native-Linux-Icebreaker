"""``BrainBackend`` public surface.

Importing this package does NOT import concrete backend modules
(``anthropic_backend``, ``gemini_backend``, ``llama_local_backend``) —
those self-register via ``@register_backend(...)`` when YOU import them
explicitly. This keeps users on ``backend = "local"`` from needing the
``anthropic`` or ``google-generativeai`` SDKs installed (D7).

Concrete usage pattern (e.g., from M2.12 orchestrator):

    from controller.backends import make_backend
    from controller.config import load
    import controller.backends.anthropic_backend  # registers "anthropic"
    cfg = load()
    backend = make_backend(cfg)
"""

from ._api_common import transform_schema_for_provider
from .auditor import OutboundPayloadAuditor
from .base import (
    MAX_RETRY_HARD_CAP,
    BrainBackend,
    BrainConfigError,
    BrainError,
    BrainProviderError,
    BrainResponse,
    BrainSchemaError,
    BrainSecurityError,
    BrainTruncationError,
    RequestEnvelope,
)
from .registry import (
    make_backend,
    register_backend,
    registered_backends,
)
from .sanitize import (
    SECRET_PATTERNS,
    KeyRedactionFilter,
    SecretRef,
    install_root_redaction_filter,
    sanitize_exception,
)

__all__ = [
    "MAX_RETRY_HARD_CAP",
    "BrainBackend",
    "BrainConfigError",
    "BrainError",
    "BrainProviderError",
    "BrainResponse",
    "BrainSchemaError",
    "BrainSecurityError",
    "BrainTruncationError",
    "KeyRedactionFilter",
    "OutboundPayloadAuditor",
    "RequestEnvelope",
    "SECRET_PATTERNS",
    "SecretRef",
    "install_root_redaction_filter",
    "make_backend",
    "register_backend",
    "registered_backends",
    "sanitize_exception",
    "transform_schema_for_provider",
]
