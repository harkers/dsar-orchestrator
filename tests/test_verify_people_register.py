"""Tests for dsar-conductor verify --check people-register (P6T5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.verify import verify_people_register

_VALID_THREAT_MODEL = """# Threat model

## Embed endpoint
Local mlx-broker on 127.0.0.1:8090. No public DNS exposure.
Subject embeddings never leave the case directory.

## Isolation posture
Per-engagement sparse-bundle mount at /Volumes/<client>/.
Local sockets only, no shared state.

## Denylist scope
Per-case third_party_denylist.json operator-curated via /people-register.
Never shared across engagements.

## Per-engagement data flow
Ingest -> people-register -> redact -> bake -> export.
Everything stays inside the sparse bundle.

## Subject identifier handling
data_subject.json holds full_name + aliases + emails.
Subject denylist suppresses these from every candidate source.
"""


def _seed_case(tmp_path: Path) -> Path:
    working = tmp_path / "working"
    working.mkdir()
    (working / "register.json").write_text(
        json.dumps(
            [
                {
                    "ref": "r-1",
                    "mailbox_owner_email": "alice@example.com",
                    "mailbox_owner_display": "Alice",
                    "source_kind": "exchange",
                    "text_quality": "high",
                }
            ]
        )
    )
    (working / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com", "aliases": []})
    )
    (working / "people_register.json").write_text(
        json.dumps(
            [
                {
                    "canonical_name": "Alice Other",
                    "emails": ["alice@example.com"],
                    "phones": [],
                    "titles": [],
                    "source_refs": ["r-1"],
                    "correlation_ids": [],
                    "mention_count": 1,
                    "distinct_doc_count": 1,
                    "confidence_score": 1.0,
                    "discovered_by": "exchange",
                    "is_data_subject": False,
                    "is_subject_confidence": 0.0,
                    "subject_centricity_score": 0.0,
                    "text_quality_summary": "high",
                },
                {
                    "canonical_name": "Subject A",
                    "emails": ["s@x.com"],
                    "phones": [],
                    "titles": [],
                    "source_refs": ["r-1"],
                    "correlation_ids": [],
                    "mention_count": 1,
                    "distinct_doc_count": 1,
                    "confidence_score": 1.0,
                    "discovered_by": "exchange",
                    "is_data_subject": True,
                    "is_subject_confidence": 1.0,
                    "subject_centricity_score": 0.0,
                    "text_quality_summary": "high",
                },
            ]
        )
    )
    (working / "threat_model.md").write_text(_VALID_THREAT_MODEL)
    return tmp_path


def test_verify_returns_ok_on_valid_case(tmp_path: Path) -> None:
    case = _seed_case(tmp_path)
    result = verify_people_register(case)
    assert result["ok"] is True
    assert "Source strategy" in result["message"]
    assert "1 subject cluster" in result["message"]
    assert "0 SubjectInDenylistError" in result["message"]


def test_verify_details_carries_structured_fields(tmp_path: Path) -> None:
    case = _seed_case(tmp_path)
    result = verify_people_register(case)
    details = result["details"]
    assert details["third_party_clusters"] == 1
    assert details["subject_clusters"] == 1
    assert details["subject_in_denylist_errors"] == 0
    assert details["manual_queue_refs"] == 0
    assert details["source_strategy"] in {"exchange_nested", "exchange_flat", None}


def test_verify_returns_failure_on_missing_threat_model(tmp_path: Path) -> None:
    case = _seed_case(tmp_path)
    (case / "working" / "threat_model.md").unlink()
    result = verify_people_register(case)
    assert result["ok"] is False
    assert "ThreatModelMissingError" in result["message"]


def test_verify_returns_failure_on_empty_ingest(tmp_path: Path) -> None:
    """No register.json -> EmptyIngestError or PeopleRegisterEmptyError."""
    working = tmp_path / "working"
    working.mkdir()
    (working / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com", "aliases": []})
    )
    (working / "people_register.json").write_text("[]")
    (working / "threat_model.md").write_text(_VALID_THREAT_MODEL)
    result = verify_people_register(tmp_path)
    assert result["ok"] is False
    assert (
        "EmptyIngestError" in result["message"] or "PeopleRegisterEmptyError" in result["message"]
    )


def test_verify_counts_manual_queue_refs(tmp_path: Path) -> None:
    case = _seed_case(tmp_path)
    (case / "working" / "manual_preprocessing_queue.jsonl").write_text(
        json.dumps({"ref": "r-1", "reason": "ocr_failure"})
        + "\n"
        + json.dumps({"ref": "r-2", "reason": "ocr_failure"})
        + "\n"
    )
    result = verify_people_register(case)
    assert result["details"]["manual_queue_refs"] == 2


def test_verify_detects_subject_in_denylist_error(tmp_path: Path) -> None:
    """Signed cache with result='error' surfaces as subject_in_denylist_errors=1."""
    case = _seed_case(tmp_path)
    (case / "working" / ".subject_protection_cache.json").write_text(
        json.dumps(
            {
                "data": {
                    "key": "abc",
                    "result": "error",
                    "error": "subject matches denylist entry",
                },
                "signature": "x" * 64,
            }
        )
    )
    result = verify_people_register(case)
    assert result["details"]["subject_in_denylist_errors"] == 1


def test_verify_subcommand_choices_includes_people_register() -> None:
    """The CLI parser accepts --check people-register."""
    from dsar_orchestrator.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["verify", "--case", "demo", "--check", "people-register"])
    assert args.check == "people-register"


def test_verify_cli_dispatches_people_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """End-to-end: verify --check people-register prints ✓/✗ line."""
    from dsar_orchestrator.cli import main as cli_main

    case = _seed_case(tmp_path)
    rc = cli_main(
        [
            "verify",
            "--case",
            case.name,
            "--case-root",
            str(case),
            "--check",
            "people-register",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "✓" in out
    assert "Source strategy" in out
