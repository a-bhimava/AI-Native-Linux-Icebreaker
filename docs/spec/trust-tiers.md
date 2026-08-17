# Icebreaker trust-tier extension

Every mcpd tool schema must declare the following `x-icebreaker-trust`
extension. mcpd validates every embedded declaration before it loads
manifests, applies its sandbox, or accepts a JSON-RPC request (R13).
An omitted or malformed declaration is a startup failure.

```json
"x-icebreaker-trust": {
  "tier": 0,
  "reversible": true,
  "requires_hitl": false,
  "requires_cow": false
}
```

`tier` is an integer from `0` through `3`:

- `0`: read-only or no externally visible side effect.
- `1`: low-risk, reversible user-scoped change.
- `2`: system-level change requiring explicit review according to policy.
- `3`: destructive or otherwise critical operation.

`reversible`, `requires_hitl`, and `requires_cow` are booleans describing
the declared operation class. Runtime policy may escalate a specific call
(for example, an out-of-home file operation); it may never lower the
declared safety floor.

This declaration is metadata for review, discovery, and policy consistency.
It does not replace JSON Schema validation (INV-4), Landlock and seccomp
enforcement (INV-5), COW (INV-6), or the Controller's risk classifier.
