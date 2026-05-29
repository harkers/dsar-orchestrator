"""Tests for the dsar-durant-pass CLI promotion (#111 sub-2). Broker-free
unit tests cover classify_one (happy + error paths), resume cleanup of
errored rows, case-root resolution, and run() end-to-end with broker
monkeypatched.
"""

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
                "aliases": ["J. Doe"],
            }
        )
    )
    return tmp_path


def _write_register_and_ingested(case_root: Path, refs: list[tuple[str, str]]) -> None:
    """refs: list of (ref, body_text). Writes register + ingested + per-doc txt."""
    register = []
    for ref, body in refs:
        path = str(case_root / f"src_{ref}.eml")
        text_file = case_root / "working" / f"{ref}.txt"
        text_file.write_text(body, encoding="utf-8")
        register.append({"ref": ref, "path": path, "text_file": str(text_file)})
    (case_root / "working" / "register.json").write_text(json.dumps(register))
    with (case_root / "working" / "ingested_items.jsonl").open("w") as f:
        for r in register:
            f.write(json.dumps({"source_location": {"path": r["path"]}}) + "\n")


# --- module ---


def test_module_importable() -> None:
    import dsar_orchestrator.local_broker.durant_pass as mod

    assert hasattr(mod, "run")
    assert hasattr(mod, "main")
    assert hasattr(mod, "classify_one")
    # The inline DURANT_SYSTEM_PROMPT constant was removed; the prompt is now
    # loaded from the toolkit's sealed registry (durant.system v1.1.0) so each
    # verdict row carries verifiable provenance.
    assert mod._DURANT_PROMPT_ID == "durant.system"
    assert mod._durant_asset().body.strip()
    assert mod.VALID_VERDICTS == ("biographical", "work_context_only", "ambiguous")


def test_case_root_resolution(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker.durant_pass import _resolve_case_root

    assert _resolve_case_root(tmp_path) == tmp_path
    monkeypatch.setenv("DSAR_CASE_ROOT", str(tmp_path))
    assert _resolve_case_root(None) == tmp_path
    monkeypatch.delenv("DSAR_CASE_ROOT")
    monkeypatch.chdir(tmp_path)
    assert _resolve_case_root(None) == tmp_path


def test_strip_fences_handles_codeblocks() -> None:
    from dsar_orchestrator.local_broker.durant_pass import _strip_fences

    assert _strip_fences('```json\n{"x": 1}\n```') == '{"x": 1}'
    assert _strip_fences('{"x": 1}') == '{"x": 1}'
    assert _strip_fences('```\n{"x": 1}\n```') == '{"x": 1}'


# --- classify_one ---


def test_classify_one_happy_path(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_pass

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "durant_verdict": "biographical",
                                "rationale": "Subject is the focus.",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_pass, "_post", fake_post)
    result = durant_pass.classify_one(
        case_id="CASE-100",
        doc_ref="doc-001",
        text="some body about Jane",
        subject_summary="name='Jane Doe'",
    )
    assert result["durant_verdict"] == "biographical"
    assert result["rationale"] == "Subject is the focus."
    assert "error_state" not in result


def test_classify_one_invalid_verdict_falls_back_to_ambiguous(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_pass

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"durant_verdict": "absolutely_yes", "rationale": "x"}
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_pass, "_post", fake_post)
    result = durant_pass.classify_one(
        case_id="CASE-100",
        doc_ref="doc-001",
        text="body",
        subject_summary="x",
    )
    assert result["durant_verdict"] == "ambiguous"


def test_classify_one_empty_content(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_pass

    def fake_post(*_args, **_kwargs) -> dict:
        return {"choices": [{"message": {"content": ""}}]}

    monkeypatch.setattr(durant_pass, "_post", fake_post)
    result = durant_pass.classify_one(
        case_id="CASE-100", doc_ref="doc-001", text="body", subject_summary="x"
    )
    assert result["durant_verdict"] == "ambiguous"
    assert result["error_state"] == "empty_response"


def test_classify_one_bad_json(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_pass

    def fake_post(*_args, **_kwargs) -> dict:
        return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setattr(durant_pass, "_post", fake_post)
    result = durant_pass.classify_one(
        case_id="CASE-100", doc_ref="doc-001", text="body", subject_summary="x"
    )
    assert result["durant_verdict"] == "ambiguous"
    assert result["error_state"] == "schema_validation_failed"


def test_classify_one_network_error(monkeypatch) -> None:
    import urllib.error

    from dsar_orchestrator.local_broker import durant_pass

    def fake_post(*_args, **_kwargs):
        raise urllib.error.URLError("broker down")

    monkeypatch.setattr(durant_pass, "_post", fake_post)
    result = durant_pass.classify_one(
        case_id="CASE-100", doc_ref="doc-001", text="body", subject_summary="x"
    )
    assert result["durant_verdict"] == "ambiguous"
    assert result["error_state"] == "model_unreachable"


# --- _load_completed_refs (resume cleanup) ---


def test_resume_drops_errored_rows(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_pass import _load_completed_refs

    output = case_root / "working" / "durant_verdicts.jsonl"
    rows = [
        {"doc_ref": "good-1", "durant_verdict": "biographical"},
        {"doc_ref": "bad-1", "durant_verdict": "ambiguous", "error_state": "model_unreachable"},
        {"doc_ref": "good-2", "durant_verdict": "work_context_only"},
        {"doc_ref": "bad-2", "durant_verdict": "ambiguous", "error_state": "empty_response"},
    ]
    output.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    done = _load_completed_refs(output)
    assert done == {"good-1", "good-2"}
    # Errored rows dropped from the file too
    surviving = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    assert {r["doc_ref"] for r in surviving} == {"good-1", "good-2"}


def test_resume_no_output_file_returns_empty(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_pass import _load_completed_refs

    output = case_root / "working" / "durant_verdicts.jsonl"
    assert _load_completed_refs(output) == set()


# --- run() end-to-end ---


def test_run_processes_all_unclassified_docs(case_root: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_pass

    _write_register_and_ingested(
        case_root,
        [("doc-001", "body one"), ("doc-002", "body two")],
    )

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"durant_verdict": "biographical", "rationale": "ok"})
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_pass, "_post", fake_post)
    rc = durant_pass.run(case_root)
    assert rc == 0
    output = case_root / "working" / "durant_verdicts.jsonl"
    rows = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    assert {r["doc_ref"] for r in rows} == {"doc-001", "doc-002"}
    assert all(r["durant_verdict"] == "biographical" for r in rows)


def test_run_skips_already_classified(case_root: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_pass

    _write_register_and_ingested(
        case_root,
        [("doc-001", "body one"), ("doc-002", "body two")],
    )
    # Pre-populate doc-001 as done
    output = case_root / "working" / "durant_verdicts.jsonl"
    output.write_text(
        json.dumps({"doc_ref": "doc-001", "durant_verdict": "biographical", "rationale": "prior"})
        + "\n"
    )

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"durant_verdict": "work_context_only", "rationale": "new"}
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_pass, "_post", fake_post)
    durant_pass.run(case_root)
    rows = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    by_ref = {r["doc_ref"]: r for r in rows}
    # doc-001 stays as 'biographical' (skipped); doc-002 is newly classified
    assert by_ref["doc-001"]["rationale"] == "prior"
    assert by_ref["doc-002"]["durant_verdict"] == "work_context_only"


def test_run_returns_1_when_inputs_missing(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_pass import run

    # No register / ingested files yet
    assert run(case_root) == 1
