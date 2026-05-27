"""#114 — compensating FAILURE_RECORDED chain event when JSONL append fails.

Chain-first ordering (PR #104) prevents one direction of drift (no JSONL row
without a chained REVIEWER_DECISION_MADE event). The reverse direction —
chain emit ok then JSONL/state append fails (disk full, EACCES mid-call) —
is closed here by having each operator decision wrap the post-chain write
in try/except and emit a compensating FAILURE_RECORDED event referencing
the original event's canonical hash before re-raising.
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
        json.dumps({"case_id": "CASE01", "full_name": "Test Subject"})
    )
    return case


def _read_audit_events(case_dir: Path) -> list[dict]:
    events_path = case_dir / "working" / "audit_events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line]


def _make_open_blocker(target_path: Path):
    """Return a Path.open replacement that raises OSError for ``target_path``
    opened in append mode, and delegates otherwise."""
    real_open = Path.open

    def blocked_open(self, *args, **kwargs):
        mode = ""
        if args:
            mode = args[0]
        else:
            mode = kwargs.get("mode", "r")
        if self == target_path and "a" in mode:
            raise OSError(28, "No space left on device")
        return real_open(self, *args, **kwargs)

    return blocked_open


def test_leak_review_record_decision_emits_compensating_event_on_jsonl_oserror(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsar_orchestrator.local_broker import leak_review
    from dsar_orchestrator.local_broker.leak_review import _CaseShim

    ctx = _CaseShim(case_dir=case_dir)
    target = case_dir / "audit" / "leak_review_decisions.jsonl"
    monkeypatch.setattr(Path, "open", _make_open_blocker(target))

    with pytest.raises(OSError):
        leak_review.record_decision(
            ctx,
            doc_ref="doc_001",
            decision="accept_exclude",
            reason_code="R008",
            note="leak unrecoverable",
        )

    events = _read_audit_events(case_dir)
    assert len(events) == 2, f"expected decision + compensating event, got {events}"

    decision_evt, failure_evt = events
    assert decision_evt["event_type"] == "reviewer_decision_made"
    assert decision_evt["stage"] == "leak_review"

    assert failure_evt["event_type"] == "failure_recorded"
    assert failure_evt["stage"] == "leak_review"
    assert failure_evt["phase"] == "post-chain-jsonl-write"
    assert failure_evt["error_type"] == "OSError"
    assert "No space left" in failure_evt["error"]
    assert failure_evt["item_id"] == "doc_001"
    # original_event_hash references the just-emitted decision event,
    # whose canonical hash is the prev_hash of the very next event.
    assert failure_evt["original_event_hash"] == failure_evt["prev_hash"]

    # User-visible JSONL file should NOT contain the row (write failed).
    assert not target.exists() or target.read_text() == ""


def test_unextractable_record_decision_emits_compensating_event_on_jsonl_oserror(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsar_orchestrator.local_broker import unextractable
    from dsar_orchestrator.local_broker.unextractable import _CaseShim

    ctx = _CaseShim(case_dir=case_dir)
    target = case_dir / "audit" / "unextractable_decisions.jsonl"
    monkeypatch.setattr(Path, "open", _make_open_blocker(target))

    with pytest.raises(OSError):
        unextractable.record_decision(
            ctx,
            source_path="/in/file.docx",
            decision="reject",
            reason_code="R009",
            note="corrupted file",
        )

    events = _read_audit_events(case_dir)
    assert len(events) == 2
    decision_evt, failure_evt = events
    assert decision_evt["event_type"] == "reviewer_decision_made"
    assert decision_evt["stage"] == "unextractable"

    assert failure_evt["event_type"] == "failure_recorded"
    assert failure_evt["stage"] == "unextractable"
    assert failure_evt["phase"] == "post-chain-jsonl-write"
    assert failure_evt["error_type"] == "OSError"
    assert failure_evt["item_id"] == "/in/file.docx"
    assert failure_evt["original_event_hash"] == failure_evt["prev_hash"]


def test_toggle_blocker_resolved_emits_compensating_event_on_state_write_oserror(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsar_orchestrator.operator_console import (
        CaseContext,
        toggle_blocker_resolved,
    )

    ctx = CaseContext(case_dir=case_dir)
    target = ctx.console_state
    target.parent.mkdir(parents=True, exist_ok=True)

    real_write_text = Path.write_text

    def boom_write(self, *args, **kwargs):
        if self == target:
            raise OSError("Permission denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom_write)

    with pytest.raises(OSError):
        toggle_blocker_resolved(
            ctx,
            "BLK-001",
            resolved=True,
            reason_code="R001",
            note="resolved per operator review",
        )

    events = _read_audit_events(case_dir)
    assert len(events) == 2
    decision_evt, failure_evt = events
    assert decision_evt["event_type"] == "reviewer_decision_made"
    assert decision_evt["stage"] == "blocker_toggle"

    assert failure_evt["event_type"] == "failure_recorded"
    assert failure_evt["stage"] == "blocker_toggle"
    assert failure_evt["phase"] == "post-chain-state-write"
    assert failure_evt["error_type"] == "OSError"
    assert failure_evt["item_id"] == "BLK-001"
    assert failure_evt["original_event_hash"] == failure_evt["prev_hash"]


def test_leak_review_normal_path_emits_no_failure_event(case_dir: Path) -> None:
    """Happy path sanity: only the REVIEWER_DECISION_MADE event lands."""
    from dsar_orchestrator.local_broker import leak_review
    from dsar_orchestrator.local_broker.leak_review import _CaseShim

    ctx = _CaseShim(case_dir=case_dir)
    leak_review.record_decision(
        ctx,
        doc_ref="doc_002",
        decision="include_with_note",
        reason_code="R001",
        note="preserved as data subject's own role description",
    )

    events = _read_audit_events(case_dir)
    assert len(events) == 1
    assert events[0]["event_type"] == "reviewer_decision_made"

    target = case_dir / "audit" / "leak_review_decisions.jsonl"
    assert target.exists()
    lines = [json.loads(line) for line in target.read_text().splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["doc_ref"] == "doc_002"
