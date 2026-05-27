"""Smoke tests for the /qa-walkthrough operator feature.

Coverage:
- ``build_sample`` picks docs only from the redacted/exported set
- ``build_sample`` is reproducible given a seed
- ``progress`` tracks approved/declined/pending using qa_decisions.jsonl
- ``render_qa_walkthrough`` renders the build form when no sample exists
- ``render_qa_walkthrough`` renders the per-doc page when a sample exists
- ``render_qa_walkthrough`` renders a summary when all docs are decided
- Routes ``/qa-walkthrough`` and ``/qa-walkthrough/<idx>`` are gated to the redact phase
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    case = tmp_path / "case01"
    (case / "working").mkdir(parents=True)
    (case / "audit").mkdir(parents=True)
    (case / "working" / "data_subject.json").write_text(
        json.dumps({"case_id": "CASE01", "full_name": "Jane Test"})
    )
    return case


def _seed_register(case_dir: Path, refs_with_status: list[tuple[str, str]]) -> None:
    reg = [
        {
            "ref": ref,
            "filename": f"{ref}.txt",
            "extension": ".txt",
            "status": status,
        }
        for ref, status in refs_with_status
    ]
    (case_dir / "working" / "register.json").write_text(json.dumps(reg))


def test_build_sample_only_picks_redacted_or_exported(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_walkthrough import build_sample

    _seed_register(
        case_dir,
        [
            ("a", "redacted"),
            ("b", "redacted"),
            ("c", "exported"),
            ("d", "out_of_scope"),
            ("e", "failed"),
        ],
    )
    refs = build_sample(case_dir, size=10, seed=1)
    assert set(refs) == {"a", "b", "c"}


def test_build_sample_is_reproducible_with_seed(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_walkthrough import build_sample

    _seed_register(case_dir, [(f"ref-{i:03d}", "redacted") for i in range(50)])
    a = build_sample(case_dir, size=10, seed=42)
    b = build_sample(case_dir, size=10, seed=42)
    assert a == b
    c = build_sample(case_dir, size=10, seed=43)
    assert a != c


def test_progress_tracks_decisions(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_walkthrough import build_sample, progress

    _seed_register(case_dir, [(f"ref-{i:03d}", "redacted") for i in range(5)])
    refs = build_sample(case_dir, size=5, seed=1)
    p = progress(case_dir)
    assert p == {
        "total": 5,
        "approved": 0,
        "declined": 0,
        "pending": 5,
        "next_pending_idx": 0,
    }

    qa_path = case_dir / "audit" / "qa_decisions.jsonl"
    qa_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {"doc_ref": refs[0], "decision": "approve", "ts": "2026-05-27T12:00:00Z"}
                ),
                json.dumps(
                    {
                        "doc_ref": refs[1],
                        "decision": "request_reredaction",
                        "ts": "2026-05-27T12:01:00Z",
                    }
                ),
            ]
        )
        + "\n"
    )
    p2 = progress(case_dir)
    assert p2["approved"] == 1
    assert p2["declined"] == 1
    assert p2["pending"] == 3
    assert p2["next_pending_idx"] == 2


def test_render_no_sample_shows_build_form(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import CaseContext, render_qa_walkthrough

    _seed_register(case_dir, [(f"ref-{i:03d}", "redacted") for i in range(10)])
    body = render_qa_walkthrough(CaseContext(case_dir=case_dir), None, None)
    assert "Build sample" in body
    assert "/api/qa-walkthrough/build" in body
    # The eligible pool count is surfaced so the operator knows the universe
    assert "10" in body


def test_render_with_sample_shows_per_doc_page(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_walkthrough import build_sample
    from dsar_orchestrator.operator_console import CaseContext, render_qa_walkthrough

    _seed_register(case_dir, [(f"ref-{i:03d}", "redacted") for i in range(5)])
    (case_dir / "working" / "ref-000.txt").write_text("Hello world from ref-000.")
    build_sample(case_dir, size=5, seed=1)
    body = render_qa_walkthrough(CaseContext(case_dir=case_dir), 0, None)
    # Two-pane scaffold
    assert "pane-original" in body
    assert "pane-redacted" in body
    # Approve form posts to the right endpoint
    assert "/api/qa-walkthrough/decide" in body
    assert "Approve" in body
    assert "Decline" in body


def test_render_complete_shows_summary(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_walkthrough import build_sample
    from dsar_orchestrator.operator_console import CaseContext, render_qa_walkthrough

    _seed_register(case_dir, [(f"ref-{i:03d}", "redacted") for i in range(3)])
    refs = build_sample(case_dir, size=3, seed=1)
    qa_path = case_dir / "audit" / "qa_decisions.jsonl"
    qa_path.write_text(
        "\n".join(
            json.dumps({"doc_ref": r, "decision": "approve", "ts": "2026-05-27T12:00:00Z"})
            for r in refs
        )
        + "\n"
    )
    body = render_qa_walkthrough(CaseContext(case_dir=case_dir), None, None)
    assert "complete" in body.lower()
    # All refs surfaced
    for r in refs:
        assert r in body


def test_qa_walkthrough_route_gated_by_redact_phase() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    pre_redact = {"current_stage": "context_running"}
    allowed, _ = is_route_accessible(pre_redact, "/qa-walkthrough")
    assert allowed is False
    allowed2, _ = is_route_accessible(pre_redact, "/qa-walkthrough/0")
    assert allowed2 is False

    in_redact = {"current_stage": "redaction_qc_a_running"}
    assert is_route_accessible(in_redact, "/qa-walkthrough")[0] is True
    assert is_route_accessible(in_redact, "/qa-walkthrough/0")[0] is True
