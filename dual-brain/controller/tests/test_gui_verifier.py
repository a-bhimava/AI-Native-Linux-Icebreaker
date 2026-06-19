"""Tests for GUI verifier schema extensions (PR #26)."""

from __future__ import annotations

import json

import jsonschema
import pytest

from controller.verifier import _VERIFY_SCHEMA


class TestVerifierGuiConcerns:
    def test_accepts_empty_concerns(self):
        data = {"verified": True, "reason": "OK", "concerns": []}
        jsonschema.validate(data, _VERIFY_SCHEMA)

    def test_accepts_valid_concerns(self):
        data = {
            "verified": False,
            "reason": "wrong target",
            "concerns": ["wrong_element", "timing_issue"],
        }
        jsonschema.validate(data, _VERIFY_SCHEMA)

    def test_rejects_invalid_concern(self):
        data = {
            "verified": False,
            "reason": "bad",
            "concerns": ["nonexistent_concern"],
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(data, _VERIFY_SCHEMA)

    def test_accepts_predicted_state(self):
        data = {
            "verified": True,
            "reason": "OK",
            "predicted_state": "button will be pressed",
        }
        jsonschema.validate(data, _VERIFY_SCHEMA)

    def test_backwards_compatible(self):
        data = {"verified": True, "reason": "OK"}
        jsonschema.validate(data, _VERIFY_SCHEMA)
