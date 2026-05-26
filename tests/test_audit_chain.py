"""Tests for the operator console's hash-chain wrapper around
``FileAuditStore.append_event``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    (tmp_path / "audit").mkdir()
    return tmp_path


def _read_events(case_dir: Path) -> list[dict]:
    p = case_dir / "working" / "audit_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _canonical_hash(event: dict) -> str:
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_emit_first_decision_event_has_null_prev_hash(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import emit_decision_event

    emit_decision_event(
        case_dir,
        decision_kind="leak_review",
        payload={"doc_ref": "doc-001", "decision": "accept_exclude", "note": ""},
        case_id="TEST-100",
    )
    events = _read_events(case_dir)
    assert len(events) == 1
    assert events[0]["prev_hash"] is None
    assert events[0]["case_id"] == "TEST-100"
    assert events[0]["agent"] == "operator_console"
    assert events[0]["stage"] == "leak_review"
    assert events[0]["event_type"] == "reviewer_decision_made"
    assert events[0]["doc_ref"] == "doc-001"


def test_subsequent_events_chain_to_previous(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import emit_decision_event

    for i, kind in enumerate(("leak_review", "unextractable", "blocker_toggle")):
        emit_decision_event(
            case_dir,
            decision_kind=kind,
            payload={"i": i, "decision": "ok"},
            case_id="TEST-100",
        )
    events = _read_events(case_dir)
    assert len(events) == 3
    assert events[0]["prev_hash"] is None
    assert events[1]["prev_hash"] == _canonical_hash(events[0])
    assert events[2]["prev_hash"] == _canonical_hash(events[1])


def test_returns_canonical_hash_of_written_event(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import emit_decision_event

    returned = emit_decision_event(
        case_dir,
        decision_kind="leak_review",
        payload={"decision": "accept_exclude"},
        case_id="TEST-100",
    )
    events = _read_events(case_dir)
    assert returned == _canonical_hash(events[0])


def test_case_id_resolved_from_data_subject_json(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import (
        emit_decision_event,
        resolve_case_id,
    )

    (case_dir / "working" / "data_subject.json").write_text(
        json.dumps({"case_no": "301770", "subject_name": "Jane Doe"})
    )
    assert resolve_case_id(case_dir) == "301770"

    emit_decision_event(
        case_dir,
        decision_kind="leak_review",
        payload={"decision": "accept_exclude"},
        case_id=resolve_case_id(case_dir),
    )
    events = _read_events(case_dir)
    assert events[0]["case_id"] == "301770"


def test_case_id_falls_back_to_dir_name(tmp_path: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import resolve_case_id

    case_dir = tmp_path / "case-fallback"
    case_dir.mkdir()
    (case_dir / "working").mkdir()
    assert resolve_case_id(case_dir) == "case-fallback"


def test_case_id_falls_back_when_data_subject_json_malformed(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import resolve_case_id

    (case_dir / "working" / "data_subject.json").write_text("{ not valid json")
    assert resolve_case_id(case_dir) == case_dir.name


def test_case_id_with_null_case_no_falls_back(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import resolve_case_id

    (case_dir / "working" / "data_subject.json").write_text(
        json.dumps({"case_no": None, "subject_name": "Jane"})
    )
    assert resolve_case_id(case_dir) == case_dir.name


def test_emit_failure_blocks_jsonl_write_leak_review(case_dir: Path, monkeypatch) -> None:
    """Chain-first invariant: if emit raises, JSONL row is NOT written."""
    from dsar_orchestrator.local_broker import leak_review

    def broken_emit(*_args, **_kwargs) -> str:
        raise RuntimeError("chain emit failed")

    monkeypatch.setattr("dsar_orchestrator.local_broker.audit_chain.emit_for_case_dir", broken_emit)
    shim = leak_review._CaseShim(case_dir=case_dir)
    with pytest.raises(RuntimeError, match="chain emit failed"):
        leak_review.record_decision(shim, doc_ref="doc-001", decision="accept_exclude", note="")
    jsonl_path = case_dir / "audit" / "leak_review_decisions.jsonl"
    assert not jsonl_path.exists() or jsonl_path.read_text().strip() == ""


def test_blocker_toggle_state_written_after_chain(case_dir: Path) -> None:
    """End-to-end: chain succeeds → state file mutation visible."""
    from dsar_orchestrator.operator_console import (
        CaseContext,
        load_console_state,
        toggle_blocker_resolved,
    )

    (case_dir / "working" / "data_subject.json").write_text(json.dumps({"case_no": "TEST-100"}))
    ctx = CaseContext(case_dir=case_dir)
    toggle_blocker_resolved(ctx, "BLOCK-007", resolved=True, note="ok")
    state = load_console_state(ctx)
    assert "BLOCK-007" in state.get("resolved_blockers", {})
    assert state["resolved_blockers"]["BLOCK-007"]["note"] == "ok"
    events = _read_events(case_dir)
    assert len(events) == 1
    assert events[0]["blocker_id"] == "BLOCK-007"


def test_concurrent_emits_produce_valid_chain(case_dir: Path) -> None:
    import threading

    from dsar_orchestrator.local_broker.audit_chain import emit_decision_event

    def fire(i: int) -> None:
        emit_decision_event(
            case_dir,
            decision_kind="leak_review",
            payload={"i": i, "decision": "accept_exclude"},
            case_id="TEST-100",
        )

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = _read_events(case_dir)
    assert len(events) == 20
    assert events[0]["prev_hash"] is None
    for prev, curr in zip(events, events[1:]):
        assert curr["prev_hash"] == _canonical_hash(prev), (
            f"chain broken between events {prev['event_id']} and {curr['event_id']}"
        )


def test_optional_item_id_carried_through(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.audit_chain import emit_decision_event

    emit_decision_event(
        case_dir,
        decision_kind="leak_review",
        payload={"decision": "accept_exclude"},
        case_id="TEST-100",
        item_id="sha256:deadbeef",
    )
    events = _read_events(case_dir)
    assert events[0]["item_id"] == "sha256:deadbeef"


def test_leak_review_record_decision_emits_chain_event(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import leak_review

    shim = leak_review._CaseShim(case_dir=case_dir)
    (case_dir / "working" / "data_subject.json").write_text(json.dumps({"case_no": "TEST-100"}))
    leak_review.record_decision(
        shim, doc_ref="doc-001", decision="accept_exclude", note="documented exemption"
    )
    events = _read_events(case_dir)
    assert len(events) == 1
    assert events[0]["stage"] == "leak_review"
    assert events[0]["doc_ref"] == "doc-001"
    assert events[0]["decision"] == "accept_exclude"


def test_unextractable_record_decision_emits_chain_event(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import unextractable

    shim = unextractable._CaseShim(case_dir=case_dir)
    (case_dir / "working" / "data_subject.json").write_text(json.dumps({"case_no": "TEST-100"}))
    unextractable.record_decision(
        shim, source_path="/data/missing.eml", decision="accept", note="known broken"
    )
    events = _read_events(case_dir)
    assert len(events) == 1
    assert events[0]["stage"] == "unextractable"
    assert events[0]["source_path"] == "/data/missing.eml"
    assert events[0]["decision"] == "accept"


def test_blocker_toggle_emits_chain_event(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import (
        CaseContext,
        toggle_blocker_resolved,
    )

    (case_dir / "working" / "data_subject.json").write_text(json.dumps({"case_no": "TEST-100"}))
    ctx = CaseContext(case_dir=case_dir)
    toggle_blocker_resolved(ctx, "BLOCK-007", resolved=True, note="verified by DPO")
    events = _read_events(case_dir)
    assert len(events) == 1
    assert events[0]["stage"] == "blocker_toggle"
    assert events[0]["blocker_id"] == "BLOCK-007"
    assert events[0]["resolved"] is True
