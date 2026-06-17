---
name: gemini-schema-transform
description: Gemini response_schema rejects JSON-Schema keywords; the Controller must strip them
metadata:
  type: reference
---
When the Quarantined Brain backend is **Gemini**, the intent JSON Schema is sent as the provider's native `response_schema`, an **OpenAPI subset** that rejects standard JSON-Schema keywords with `ValueError: Unknown field for Schema: <key>`.

`controller/backends/_api_common.py::transform_schema_for_provider` must strip:
- **Metadata (both providers):** `$schema`, `$id`, `$comment`, `title` → `_PROVIDER_STRIP_META`.
- **Gemini-only (gated on `strip_format=True`):** `additionalProperties`, `pattern` → `_GEMINI_ONLY_STRIP_KEYS`. Anthropic's `input_schema` supports these, so they are kept there.
- Plus the numeric/length constraint strips and `format`. `description` is KEPT.
The local `jsonschema` validator still sees the ORIGINAL schema, so stripping is safety-lossless (validator + retry is the floor, INV-2-pluggable).

**Why it bites:** gate G10 MOCKS the SDKs, so it never hits the real parser — only a LIVE call catches it (hence the live Gemini smoke test). `test_schema_transform.py` now asserts these keys are stripped. Also: Gemini model names get retired — `gemini-2.0-flash` 404'd; current default `gemini-2.5-flash`. Use `genai.list_models()` if it breaks. See [[phase2-e2e-vm-setup]].
