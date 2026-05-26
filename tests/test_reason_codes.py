"""Tests for the R001-R010 + R-PENDING reason-code taxonomy and its
wiring into operator-console decision-recording paths.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    (tmp_path / "audit").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(json.dumps({"case_no": "TEST-100"}))
    return tmp_path


def _read_events(case_dir: Path) -> list[dict]:
    p = case_dir / "working" / "audit_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# --- Taxonomy ---


def test_taxonomy_has_r001_through_r010_and_r_pending() -> None:
    from dsar_orchestrator.local_broker.reason_codes import REASON_CODES

    expected = {f"R{n:03d}" for n in range(1, 11)} | {"R-PENDING"}
    assert set(REASON_CODES) == expected


def test_each_code_has_label_and_meaning() -> None:
    from dsar_orchestrator.local_broker.reason_codes import REASON_CODES

    for code, entry in REASON_CODES.items():
        assert "label" in entry and entry["label"], f"{code} missing label"
        assert "meaning" in entry and entry["meaning"], f"{code} missing meaning"


# --- Validation ---


def test_validate_unknown_code_raises() -> None:
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    with pytest.raises(ValueError, match="unknown reason_code"):
        validate_reason_code("R999", note="x")


def test_validate_empty_code_raises() -> None:
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    with pytest.raises(ValueError, match="reason_code is required"):
        validate_reason_code("", note="x")


def test_validate_r001_passes_without_note() -> None:
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    validate_reason_code("R001", note="")


def test_validate_r_pending_requires_non_empty_note() -> None:
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    with pytest.raises(ValueError, match="requires a non-empty note"):
        validate_reason_code("R-PENDING", note="")
    with pytest.raises(ValueError, match="requires a non-empty note"):
        validate_reason_code("R-PENDING", note="   ")
    validate_reason_code("R-PENDING", note="Operator unsure — to escalate")


def test_validate_r006_special_category_requires_note() -> None:
    """R006 special-category escalation must carry rationale."""
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    with pytest.raises(ValueError, match="requires a non-empty note"):
        validate_reason_code("R006", note="")
    validate_reason_code("R006", note="Health data referenced; DPO review needed")


# --- Stale R-PENDING detection ---


def test_is_r_pending_stale_inside_window() -> None:
    from dsar_orchestrator.local_broker.reason_codes import is_r_pending_stale

    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    fresh = (now - timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    assert is_r_pending_stale(fresh, now=now) is False


def test_is_r_pending_stale_past_window() -> None:
    from dsar_orchestrator.local_broker.reason_codes import is_r_pending_stale

    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    stale = (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    assert is_r_pending_stale(stale, now=now) is True


def test_is_r_pending_stale_handles_malformed_ts() -> None:
    from dsar_orchestrator.local_broker.reason_codes import is_r_pending_stale

    # Malformed → treat as stale (safer for an escalation signal).
    assert is_r_pending_stale("not-a-date") is True


# --- Wiring into decision-recording sites ---


def test_leak_review_record_decision_requires_reason_code(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import leak_review

    shim = leak_review._CaseShim(case_dir=case_dir)
    with pytest.raises(ValueError, match="reason_code is required"):
        leak_review.record_decision(
            shim,
            doc_ref="doc-001",
            decision="accept_exclude",
            reason_code="",
            note="",
        )


def test_leak_review_record_decision_rejects_unknown_code(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import leak_review

    shim = leak_review._CaseShim(case_dir=case_dir)
    with pytest.raises(ValueError, match="unknown reason_code"):
        leak_review.record_decision(
            shim,
            doc_ref="doc-001",
            decision="accept_exclude",
            reason_code="BOGUS",
            note="",
        )


def test_leak_review_carries_reason_code_into_chain(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import leak_review

    shim = leak_review._CaseShim(case_dir=case_dir)
    leak_review.record_decision(
        shim,
        doc_ref="doc-001",
        decision="accept_exclude",
        reason_code="R010",
        note="forwarded to legal counsel",
    )
    events = _read_events(case_dir)
    assert events[0]["reason_code"] == "R010"
    # Also in the user-visible JSONL
    jsonl = (case_dir / "audit" / "leak_review_decisions.jsonl").read_text()
    row = json.loads(jsonl.strip())
    assert row["reason_code"] == "R010"


def test_unextractable_record_decision_requires_reason_code(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import unextractable

    shim = unextractable._CaseShim(case_dir=case_dir)
    with pytest.raises(ValueError, match="reason_code is required"):
        unextractable.record_decision(
            shim,
            source_path="/data/x.eml",
            decision="accept",
            reason_code="",
            note="",
        )


def test_unextractable_carries_reason_code_into_chain(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import unextractable

    shim = unextractable._CaseShim(case_dir=case_dir)
    unextractable.record_decision(
        shim,
        source_path="/data/x.eml",
        decision="accept",
        reason_code="R009",
        note="extractor lacks codec",
    )
    events = _read_events(case_dir)
    assert events[0]["reason_code"] == "R009"


def test_blocker_toggle_requires_reason_code(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import (
        CaseContext,
        toggle_blocker_resolved,
    )

    ctx = CaseContext(case_dir=case_dir)
    with pytest.raises(ValueError, match="reason_code is required"):
        toggle_blocker_resolved(
            ctx,
            "BLOCK-007",
            resolved=True,
            reason_code="",
            note="",
        )


def test_blocker_toggle_carries_reason_code_into_chain(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import (
        CaseContext,
        toggle_blocker_resolved,
    )

    ctx = CaseContext(case_dir=case_dir)
    toggle_blocker_resolved(
        ctx,
        "BLOCK-007",
        resolved=True,
        reason_code="R007",
        note="redaction verified accurate",
    )
    events = _read_events(case_dir)
    assert events[0]["reason_code"] == "R007"


def test_r_pending_blocker_toggle_requires_note(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import (
        CaseContext,
        toggle_blocker_resolved,
    )

    ctx = CaseContext(case_dir=case_dir)
    with pytest.raises(ValueError, match="requires a non-empty note"):
        toggle_blocker_resolved(
            ctx,
            "BLOCK-007",
            resolved=True,
            reason_code="R-PENDING",
            note="",
        )
