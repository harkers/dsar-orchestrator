"""Tests for the dsar-context-classify-mini CLI promotion (#111 sub-4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


@pytest.fixture
def case_root(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    (tmp_path / "audit").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps(
            {
                "full_name": "Jane Doe",
                "email": "jane@example.com",
                "subject_protected_phrases": ["Jane's Org", "Project Alpha"],
            }
        )
    )
    return tmp_path


def _write_inputs(case_root: Path, refs: list[tuple[str, str]]) -> None:
    register = []
    for ref, body in refs:
        text_file = case_root / "working" / f"{ref}.txt"
        text_file.write_text(body, encoding="utf-8")
        register.append({"ref": ref, "path": f"/src/{ref}.eml", "text_file": str(text_file)})
    (case_root / "working" / "register.json").write_text(json.dumps(register))
    with (case_root / "working" / "ingested_items.jsonl").open("w") as f:
        for r in register:
            f.write(json.dumps({"source_location": {"path": r["path"]}}) + "\n")


def _fake_full_response() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "durant_verdict": "biographical",
                            "durant_rationale": "Subject named throughout.",
                            "primary_classification": "communication",
                            "is_about_requester": "yes",
                            "confidence": 0.85,
                            "requester_role": "subject",
                            "evidence_snippet": "RE: your contract",
                            "recommended_action": "disclose",
                            "rationale": "ok",
                        }
                    )
                }
            }
        ]
    }


# --- module ---


def test_module_importable_and_allowed_lists() -> None:
    import dsar_orchestrator.local_broker.context_classify_mini as mod

    assert hasattr(mod, "run")
    assert hasattr(mod, "main")
    assert hasattr(mod, "classify_one")
    assert hasattr(mod, "_coerce_parsed")
    assert "biographical" in mod.ALLOWED_DURANT
    assert "communication" in mod.ALLOWED_CLASSIFICATIONS
    assert "yes" in mod.ALLOWED_IS_ABOUT


def test_case_root_resolution(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker.context_classify_mini import _resolve_case_root

    assert _resolve_case_root(tmp_path) == tmp_path
    monkeypatch.setenv("DSAR_CASE_ROOT", str(tmp_path))
    assert _resolve_case_root(None) == tmp_path
    monkeypatch.delenv("DSAR_CASE_ROOT")
    monkeypatch.chdir(tmp_path)
    assert _resolve_case_root(None) == tmp_path


# --- _coerce_parsed ---


def test_coerce_clamps_confidence_to_unit_interval() -> None:
    from dsar_orchestrator.local_broker.context_classify_mini import _coerce_parsed

    assert _coerce_parsed({"confidence": 1.5})["confidence"] == 1.0
    assert _coerce_parsed({"confidence": -0.3})["confidence"] == 0.0
    assert _coerce_parsed({"confidence": "not a number"})["confidence"] == 0.0


def test_coerce_unknown_durant_falls_back_to_ambiguous() -> None:
    from dsar_orchestrator.local_broker.context_classify_mini import _coerce_parsed

    out = _coerce_parsed({"durant_verdict": "definitely_yes"})
    assert out["durant_verdict"] == "ambiguous"


def test_coerce_unknown_primary_classification_falls_back_to_other() -> None:
    from dsar_orchestrator.local_broker.context_classify_mini import _coerce_parsed

    out = _coerce_parsed({"primary_classification": "haiku"})
    assert out["primary_classification"] == "other"


def test_coerce_unknown_is_about_falls_back_to_unclear() -> None:
    from dsar_orchestrator.local_broker.context_classify_mini import _coerce_parsed

    out = _coerce_parsed({"is_about_requester": "definitely"})
    assert out["is_about_requester"] == "unclear"


def test_coerce_truncates_long_fields() -> None:
    from dsar_orchestrator.local_broker.context_classify_mini import _coerce_parsed

    long = "x" * 1000
    out = _coerce_parsed(
        {
            "durant_rationale": long,
            "evidence_snippet": long,
            "rationale": long,
            "requester_role": long,
            "recommended_action": long,
        }
    )
    assert len(out["durant_rationale"]) == 600
    assert len(out["evidence_snippet"]) == 300
    assert len(out["rationale"]) == 500
    assert len(out["requester_role"]) == 32
    assert len(out["recommended_action"]) == 32


# --- classify_one ---


def test_classify_one_happy_path(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import context_classify_mini

    monkeypatch.setattr(context_classify_mini, "_post", lambda *_a, **_k: _fake_full_response())
    result = context_classify_mini.classify_one(
        case_id="CASE-100",
        doc_ref="doc-001",
        text="body about Jane Doe",
        subject_summary="name='Jane Doe'",
    )
    assert result["durant_verdict"] == "biographical"
    assert result["primary_classification"] == "communication"
    assert result["confidence"] == 0.85
    assert "error_state" not in result


def test_classify_one_empty_content_returns_error_row(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import context_classify_mini

    monkeypatch.setattr(
        context_classify_mini,
        "_post",
        lambda *_a, **_k: {"choices": [{"message": {"content": ""}}]},
    )
    result = context_classify_mini.classify_one(
        case_id="X", doc_ref="d1", text="body", subject_summary="x"
    )
    assert result["error_state"] == "empty_response"
    assert result["durant_verdict"] == "ambiguous"
    assert result["recommended_action"] == "escalate"


def test_classify_one_network_error_returns_error_row(monkeypatch) -> None:
    import urllib.error

    from dsar_orchestrator.local_broker import context_classify_mini

    def fake(*_a, **_k):
        raise urllib.error.URLError("broker down")

    monkeypatch.setattr(context_classify_mini, "_post", fake)
    result = context_classify_mini.classify_one(
        case_id="X", doc_ref="d1", text="body", subject_summary="x"
    )
    assert result["error_state"] == "model_unreachable"
    assert result["recommended_action"] == "escalate"


# --- run() end-to-end ---


def test_run_processes_all_and_skips_resumed(case_root: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import context_classify_mini

    _write_inputs(case_root, [("doc-001", "b1"), ("doc-002", "b2")])
    # Pre-populate doc-001 as done
    out = case_root / "working" / "context_classifications.jsonl"
    out.write_text(json.dumps({"doc_ref": "doc-001", "durant_verdict": "biographical"}) + "\n")

    monkeypatch.setattr(context_classify_mini, "_post", lambda *_a, **_k: _fake_full_response())
    rc = context_classify_mini.run(case_root)
    assert rc == 0
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    refs = {r["doc_ref"] for r in rows}
    assert refs == {"doc-001", "doc-002"}


def test_run_returns_1_on_missing_inputs(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.context_classify_mini import run

    assert run(case_root) == 1
