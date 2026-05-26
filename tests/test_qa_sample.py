"""Tests for the 30-doc QA sampling flow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    (tmp_path / "audit").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(json.dumps({"case_no": "TEST-100"}))
    return tmp_path


def _write_redacted(case_dir: Path, ref: str, entity_count: int) -> None:
    """Seed redaction_decisions.jsonl + <ref>_tags.json for one doc."""
    p = case_dir / "working" / "redaction_decisions.jsonl"
    with p.open("a") as f:
        f.write(
            json.dumps(
                {
                    "doc_ref": ref,
                    "filename": f"{ref}.msg",
                    "status": "redacted",
                    "redaction_count": entity_count,
                }
            )
            + "\n"
        )
    (case_dir / "working" / f"{ref}_tags.json").write_text(
        json.dumps({"entity_count": entity_count, "redact_count": entity_count})
    )


def _seed_corpus(case_dir: Path, n: int) -> None:
    """Write n redacted docs with varying entity counts."""
    for i in range(n):
        _write_redacted(case_dir, f"doc-{i:04d}", entity_count=(i * 7) % 100)


# --- sample_for_qa ---


def test_sample_size_default_30_for_large_corpus(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import sample_for_qa

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    assert len(sample) == 30


def test_sample_size_smaller_corpus_returns_all_docs(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import sample_for_qa

    _seed_corpus(case_dir, 10)
    sample = sample_for_qa(case_dir)
    assert len(sample) == 10


def test_sample_stratified_into_buckets(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import sample_for_qa

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    buckets = {"high": 0, "medium": 0, "random": 0}
    for d in sample:
        assert d["bucket"] in buckets
        buckets[d["bucket"]] += 1
    # 30 docs split per chat-jury synthesis: 10 high + 10 medium + 10 random
    assert buckets["high"] == 10
    assert buckets["medium"] == 10
    assert buckets["random"] == 10


def test_high_bucket_has_higher_avg_entity_count_than_medium(
    case_dir: Path,
) -> None:
    from dsar_orchestrator.local_broker.qa_sample import sample_for_qa

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    high = [d for d in sample if d["bucket"] == "high"]
    med = [d for d in sample if d["bucket"] == "medium"]
    assert sum(d["entity_count"] for d in high) / 10 > sum(d["entity_count"] for d in med) / 10


def test_sample_persists_and_reads_back_same(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import sample_for_qa

    _seed_corpus(case_dir, 200)
    first = sample_for_qa(case_dir)
    second = sample_for_qa(case_dir)
    assert [d["doc_ref"] for d in first] == [d["doc_ref"] for d in second]
    assert (case_dir / "audit" / "qa_sample.jsonl").exists()


def test_resample_with_force_resets(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import sample_for_qa

    _seed_corpus(case_dir, 200)
    first = sample_for_qa(case_dir)
    # Add more docs after sampling; without force, sample stays the same
    for i in range(200, 400):
        _write_redacted(case_dir, f"doc-{i:04d}", entity_count=(i * 7) % 100)
    same = sample_for_qa(case_dir)
    assert [d["doc_ref"] for d in first] == [d["doc_ref"] for d in same]
    # With force, sample is regenerated against full corpus
    new = sample_for_qa(case_dir, force=True)
    assert {d["doc_ref"] for d in new} != {d["doc_ref"] for d in first}


def test_sample_only_includes_redacted_status(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import sample_for_qa

    _seed_corpus(case_dir, 100)
    # Add a failed doc — must not appear in sample
    p = case_dir / "working" / "redaction_decisions.jsonl"
    with p.open("a") as f:
        f.write(
            json.dumps({"doc_ref": "doc-failed", "filename": "x.msg", "status": "failed"}) + "\n"
        )
    sample = sample_for_qa(case_dir)
    assert "doc-failed" not in [d["doc_ref"] for d in sample]


# --- record_qa_decision ---


def test_record_qa_decision_requires_reason_code(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import (
        record_qa_decision,
        sample_for_qa,
    )

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    target_ref = sample[0]["doc_ref"]
    with pytest.raises(ValueError, match="reason_code is required"):
        record_qa_decision(
            case_dir,
            doc_ref=target_ref,
            decision="approve",
            reason_code="",
            note="",
        )


def test_record_qa_decision_rejects_unknown_decision(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import (
        record_qa_decision,
        sample_for_qa,
    )

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    target_ref = sample[0]["doc_ref"]
    with pytest.raises(ValueError, match="unknown qa decision"):
        record_qa_decision(
            case_dir,
            doc_ref=target_ref,
            decision="bogus",
            reason_code="R007",
            note="",
        )


def test_record_qa_decision_persists_and_emits_chain(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import (
        record_qa_decision,
        sample_for_qa,
    )

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    target_ref = sample[0]["doc_ref"]
    record_qa_decision(
        case_dir,
        doc_ref=target_ref,
        decision="approve",
        reason_code="R007",
        note="looks clean",
    )
    decisions_path = case_dir / "audit" / "qa_decisions.jsonl"
    assert decisions_path.exists()
    row = json.loads(decisions_path.read_text().strip())
    assert row["doc_ref"] == target_ref
    assert row["decision"] == "approve"
    assert row["reason_code"] == "R007"
    events = json.loads((case_dir / "working" / "audit_events.jsonl").read_text().strip())
    assert events["stage"] == "qa_sample"
    assert events["decision"] == "approve"


# --- qa_sample_complete ---


def test_qa_sample_complete_false_when_no_sample(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import qa_sample_complete

    assert qa_sample_complete(case_dir) is False


def test_qa_sample_complete_false_when_some_pending(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import (
        qa_sample_complete,
        record_qa_decision,
        sample_for_qa,
    )

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    for d in sample[:15]:  # decide only half
        record_qa_decision(
            case_dir,
            doc_ref=d["doc_ref"],
            decision="approve",
            reason_code="R007",
            note="",
        )
    assert qa_sample_complete(case_dir) is False


def test_qa_sample_complete_true_when_all_decided(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.qa_sample import (
        qa_sample_complete,
        record_qa_decision,
        sample_for_qa,
    )

    _seed_corpus(case_dir, 200)
    sample = sample_for_qa(case_dir)
    for d in sample:
        record_qa_decision(
            case_dir,
            doc_ref=d["doc_ref"],
            decision="approve",
            reason_code="R007",
            note="",
        )
    assert qa_sample_complete(case_dir) is True


def test_list_qa_sample_with_decisions_status(case_dir: Path) -> None:
    """Each sample row carries the latest decision status for the UI."""
    from dsar_orchestrator.local_broker.qa_sample import (
        list_qa_sample,
        record_qa_decision,
        sample_for_qa,
    )

    _seed_corpus(case_dir, 200)
    sample_for_qa(case_dir)
    rows = list_qa_sample(case_dir)
    assert all(r["decision"] == "pending" for r in rows)
    target_ref = rows[0]["doc_ref"]
    record_qa_decision(
        case_dir,
        doc_ref=target_ref,
        decision="approve",
        reason_code="R007",
        note="",
    )
    rows2 = list_qa_sample(case_dir)
    by_ref = {r["doc_ref"]: r for r in rows2}
    assert by_ref[target_ref]["decision"] == "approve"
    assert by_ref[target_ref]["reason_code"] == "R007"
