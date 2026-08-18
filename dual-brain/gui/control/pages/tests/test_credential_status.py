"""Regression tests: non-root UI sees provider status, never secret values."""

from __future__ import annotations

from gui.control import status


def test_credential_status_exposes_only_configured_provider_names(tmp_path, monkeypatch) -> None:
    status_file = tmp_path / "credential-status.env"
    status_file.write_text(
        "GEMINI_API_KEY=configured\nOPENAI_API_KEY=not-a-secret\n"
        "ANTHROPIC_API_KEY=sk-should-never-be-read\n"
    )
    monkeypatch.setattr(status, "_CREDENTIAL_STATUS", status_file)

    assert status._read_credential_status() == {"GEMINI_API_KEY"}


def test_collect_does_not_render_a_key_value(tmp_path, monkeypatch) -> None:
    status_file = tmp_path / "credential-status.env"
    status_file.write_text("GEMINI_API_KEY=configured\n")
    monkeypatch.setattr(status, "_CREDENTIAL_STATUS", status_file)
    monkeypatch.setattr(status, "_service_active", lambda _unit: (False, "inactive"))
    monkeypatch.setattr(status, "_socket_status", lambda *_args: "missing")

    keys = {key.env_var: key for key in status.collect().keys}
    assert keys["GEMINI_API_KEY"].configured is True
    assert keys["GEMINI_API_KEY"].masked_value == ""
