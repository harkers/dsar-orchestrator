"""Tests for the dsar_approver CLI promotion (#111 sub-1). Broker-free:
covers schema validation, audit-log path resolution, CLI argparse
plumbing, and the selftest's rejection-of-empty-evidence contract via
broker monkeypatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


# --- Schema validation ---


def test_module_importable() -> None:
    import dsar_orchestrator.local_broker.dsar_approver as mod

    assert hasattr(mod, "review")
    assert hasattr(mod, "main")
    assert hasattr(mod, "OUTPUT_SCHEMA")


def test_validator_accepts_minimal_valid_decision() -> None:
    from dsar_orchestrator.local_broker.dsar_approver import _VALIDATOR

    valid = {
        "case_id": "TEST-100",
        "decision": "REJECT",
        "risk_level": "HIGH",
        "summary": "missing evidence",
        "reviewed_areas": [{"area": "scope", "status": "NOT_PROVIDED", "notes": "n/a"}],
        "blocking_issues": [],
        "conditions": [],
        "escalations": [],
        "release_safety_checks": {
            k: "UNKNOWN"
            for k in [
                "irreversible_redaction_confirmed",
                "redaction_codes_confirmed",
                "metadata_removed",
                "comments_removed",
                "tracked_changes_removed",
                "hidden_layers_removed",
                "hidden_spreadsheet_tabs_checked",
                "ocr_text_checked",
                "attachments_checked",
                "embedded_objects_checked",
            ]
        },
        "recommended_next_step": "address gaps",
        "recommended_reviewer": "Privacy Lead",
        "approval_notes": [],
    }
    _VALIDATOR.validate(valid)


def test_validator_rejects_bad_decision_enum() -> None:
    from dsar_orchestrator.local_broker.dsar_approver import _VALIDATOR
    from jsonschema import ValidationError

    invalid = {
        "case_id": "X",
        "decision": "MAYBE",  # not in enum
        "risk_level": "HIGH",
        "summary": "",
        "reviewed_areas": [],
        "blocking_issues": [],
        "conditions": [],
        "escalations": [],
        "release_safety_checks": {
            k: "UNKNOWN"
            for k in [
                "irreversible_redaction_confirmed",
                "redaction_codes_confirmed",
                "metadata_removed",
                "comments_removed",
                "tracked_changes_removed",
                "hidden_layers_removed",
                "hidden_spreadsheet_tabs_checked",
                "ocr_text_checked",
                "attachments_checked",
                "embedded_objects_checked",
            ]
        },
        "recommended_next_step": "",
        "recommended_reviewer": "",
        "approval_notes": [],
    }
    with pytest.raises(ValidationError):
        _VALIDATOR.validate(invalid)


# --- Case-root resolution ---


def test_case_root_explicit_wins(tmp_path: Path) -> None:
    from dsar_orchestrator.local_broker.dsar_approver import _resolve_case_root

    assert _resolve_case_root(tmp_path) == tmp_path


def test_case_root_env_fallback(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker.dsar_approver import _resolve_case_root

    monkeypatch.setenv("DSAR_CASE_ROOT", str(tmp_path))
    assert _resolve_case_root(None) == tmp_path


def test_case_root_cwd_fallback(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker.dsar_approver import _resolve_case_root

    monkeypatch.delenv("DSAR_CASE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _resolve_case_root(None) == tmp_path


def test_audit_log_path_lives_under_case_root(tmp_path: Path) -> None:
    from dsar_orchestrator.local_broker.dsar_approver import _audit_log_path

    assert _audit_log_path(tmp_path) == tmp_path / "audit" / "approver-decisions.jsonl"


# --- review() with broker monkeypatched ---


def test_review_appends_to_audit_log_under_case_root(tmp_path: Path, monkeypatch) -> None:
    """review() should validate the broker output and append a row to
    <case_root>/audit/approver-decisions.jsonl."""
    from dsar_orchestrator.local_broker import dsar_approver

    fake_decision = {
        "case_id": "TEST-100",
        "decision": "REJECT",
        "risk_level": "HIGH",
        "summary": "missing evidence",
        "reviewed_areas": [{"area": "scope", "status": "NOT_PROVIDED", "notes": "n/a"}],
        "blocking_issues": [],
        "conditions": [],
        "escalations": [],
        "release_safety_checks": {
            k: "UNKNOWN"
            for k in [
                "irreversible_redaction_confirmed",
                "redaction_codes_confirmed",
                "metadata_removed",
                "comments_removed",
                "tracked_changes_removed",
                "hidden_layers_removed",
                "hidden_spreadsheet_tabs_checked",
                "ocr_text_checked",
                "attachments_checked",
                "embedded_objects_checked",
            ]
        },
        "recommended_next_step": "address gaps",
        "recommended_reviewer": "Privacy Lead",
        "approval_notes": [],
    }

    def fake_broker(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {"content": json.dumps(fake_decision), "reasoning": ""},
                    "finish_reason": "stop",
                }
            ],
            "model": "chat",
        }

    monkeypatch.setattr(dsar_approver, "_call_broker", fake_broker)
    decision = dsar_approver.review("TEST-100", {}, case_root=tmp_path)
    assert decision["decision"] == "REJECT"
    audit_path = tmp_path / "audit" / "approver-decisions.jsonl"
    assert audit_path.exists()
    row = json.loads(audit_path.read_text().strip())
    assert row["case_id"] == "TEST-100"
    assert row["decision"] == fake_decision


def test_review_raises_on_invalid_json_from_broker(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import dsar_approver

    def fake_broker(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {"content": "this is not json", "reasoning": ""},
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(dsar_approver, "_call_broker", fake_broker)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        dsar_approver.review("TEST-100", {}, case_root=tmp_path)


def test_review_raises_on_schema_failure(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import dsar_approver

    def fake_broker(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"case_id": "X", "decision": "MAYBE"}),
                        "reasoning": "",
                    },
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(dsar_approver, "_call_broker", fake_broker)
    with pytest.raises(RuntimeError, match="schema validation"):
        dsar_approver.review("TEST-100", {}, case_root=tmp_path)


def test_review_raises_on_empty_broker_content(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import dsar_approver

    def fake_broker(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {"content": "", "reasoning": ""},
                    "finish_reason": "length",
                }
            ]
        }

    monkeypatch.setattr(dsar_approver, "_call_broker", fake_broker)
    with pytest.raises(RuntimeError, match="no content"):
        dsar_approver.review("TEST-100", {}, case_root=tmp_path)


# --- CLI ---


def test_cli_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dsar_orchestrator.local_broker.dsar_approver", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "dsar-approver" in result.stdout
    assert "--case-root" in result.stdout


def test_cli_missing_case_id_exits_nonzero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dsar_orchestrator.local_broker.dsar_approver"],
        capture_output=True,
        text=True,
        check=False,
        input="",
        env={**dict(os.environ), "DSAR_CASE_ROOT": "/tmp"},
    )
    assert result.returncode != 0
    assert "case_id" in result.stderr
