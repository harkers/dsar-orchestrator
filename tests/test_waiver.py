"""#110 — Hard-Blocker Waiver workflow (console-side, separate from
Approver's 4 verdicts).

Broker decisions locked (chat synthesis):
- Q1: Keep Approver's verdicts; add separate console-side Waiver workflow
- Q2: Dedicated /waiver page with batched signoff
- Q3: Second console login by DPO to co-sign (DSAR_DPO_TOKEN env shim)
- Q4: waiver:true flag + co_signer field on existing chain (no extra HMAC)
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


def _read_audit_events(case_dir: Path) -> list[dict]:
    events_path = case_dir / "working" / "audit_events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line]


def _read_waivers(case_dir: Path) -> list[dict]:
    p = case_dir / "audit" / "waivers.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


# ---- propose_waiver --------------------------------------------------------


def test_propose_waiver_appends_pending_record(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    result = wv.propose_waiver(
        ctx,
        blocker_ids=["BLK-1", "BLK-2"],
        justification="Operator accepts residual risk pending DPO co-sign.",
        operator_id="stu",
    )

    assert result["state"] == "pending"
    assert result["operator_id"] == "stu"
    assert result["blocker_ids"] == ["BLK-1", "BLK-2"]
    assert result["waiver_id"].startswith("WV-")
    assert result["proposed_event_hash"]

    rows = _read_waivers(case_dir)
    assert len(rows) == 1
    assert rows[0] == result


def test_propose_waiver_emits_chain_event(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    wv.propose_waiver(
        ctx,
        blocker_ids=["BLK-7"],
        justification="ok",
        operator_id="stu",
    )

    events = _read_audit_events(case_dir)
    assert len(events) == 1
    evt = events[0]
    assert evt["event_type"] == "reviewer_decision_made"
    assert evt["stage"] == "waiver_propose"
    assert evt.get("waiver") is True
    assert evt.get("action") == "propose"
    assert evt.get("blocker_ids") == ["BLK-7"]
    assert evt.get("operator_id") == "stu"


def test_propose_waiver_rejects_empty_blocker_ids(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    with pytest.raises(ValueError, match="blocker_ids"):
        wv.propose_waiver(ctx, blocker_ids=[], justification="x", operator_id="stu")


def test_propose_waiver_rejects_empty_justification(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    with pytest.raises(ValueError, match="justification"):
        wv.propose_waiver(ctx, blocker_ids=["BLK-1"], justification="   ", operator_id="stu")


def test_propose_waiver_rejects_empty_operator_id(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    with pytest.raises(ValueError, match="operator_id"):
        wv.propose_waiver(ctx, blocker_ids=["BLK-1"], justification="x", operator_id="")


# ---- co_sign_waiver -------------------------------------------------------


def test_co_sign_waiver_finalises_pending(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    proposed = wv.propose_waiver(
        ctx,
        blocker_ids=["BLK-3"],
        justification="risk accepted",
        operator_id="stu",
    )

    finalised = wv.co_sign_waiver(
        ctx,
        waiver_id=proposed["waiver_id"],
        dpo_id="dpo-jane",
        dpo_note="DPO approves with note for audit trail.",
    )
    assert finalised["state"] == "co_signed"
    assert finalised["dpo_id"] == "dpo-jane"
    assert finalised["dpo_note"]
    assert finalised["co_signed_ts"]
    assert finalised["cosign_event_hash"]
    # propose event hash carried forward as tamper anchor
    assert finalised["proposed_event_hash"] == proposed["proposed_event_hash"]


def test_co_sign_waiver_emits_chain_event_with_elevated_payload(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    proposed = wv.propose_waiver(
        ctx,
        blocker_ids=["BLK-9", "BLK-10"],
        justification="risk accepted",
        operator_id="stu",
    )
    wv.co_sign_waiver(
        ctx,
        waiver_id=proposed["waiver_id"],
        dpo_id="dpo-jane",
        dpo_note="audit trail note",
    )

    events = _read_audit_events(case_dir)
    assert len(events) == 2
    cosign = events[1]
    assert cosign["event_type"] == "reviewer_decision_made"
    assert cosign["stage"] == "waiver_cosign"
    assert cosign.get("waiver") is True
    assert cosign.get("action") == "cosign"
    assert cosign.get("blocker_ids") == ["BLK-9", "BLK-10"]
    assert cosign.get("operator_id") == "stu"
    assert cosign.get("dpo_id") == "dpo-jane"
    assert cosign.get("justification") == "risk accepted"
    assert cosign.get("dpo_note") == "audit trail note"
    # Tamper anchor: cosign references the propose event's canonical hash
    assert cosign.get("original_event_hash") == proposed["proposed_event_hash"]


def test_co_sign_waiver_missing_id_raises(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    with pytest.raises(LookupError, match="not found"):
        wv.co_sign_waiver(ctx, waiver_id="WV-nonexistent", dpo_id="d", dpo_note="n")


def test_co_sign_waiver_already_signed_raises(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    proposed = wv.propose_waiver(ctx, blocker_ids=["BLK-X"], justification="ok", operator_id="stu")
    wv.co_sign_waiver(ctx, waiver_id=proposed["waiver_id"], dpo_id="dpo-a", dpo_note="first")
    with pytest.raises(ValueError, match="already co.?signed"):
        wv.co_sign_waiver(ctx, waiver_id=proposed["waiver_id"], dpo_id="dpo-b", dpo_note="second")


# ---- list helpers --------------------------------------------------------


def test_list_pending_waivers_filters_co_signed(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    p1 = wv.propose_waiver(ctx, blocker_ids=["BLK-1"], justification="x", operator_id="stu")
    p2 = wv.propose_waiver(ctx, blocker_ids=["BLK-2"], justification="y", operator_id="stu")
    wv.co_sign_waiver(ctx, waiver_id=p1["waiver_id"], dpo_id="d", dpo_note="ok")

    pending = wv.list_pending_waivers(ctx)
    assert len(pending) == 1
    assert pending[0]["waiver_id"] == p2["waiver_id"]


def test_list_all_waivers_returns_latest_state_per_id(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext

    ctx = CaseContext(case_dir=case_dir)
    p = wv.propose_waiver(ctx, blocker_ids=["BLK-1"], justification="x", operator_id="stu")
    wv.co_sign_waiver(ctx, waiver_id=p["waiver_id"], dpo_id="d", dpo_note="ok")
    rows = wv.list_all_waivers(ctx)
    assert len(rows) == 1
    assert rows[0]["state"] == "co_signed"


# ---- DSAR_DPO_TOKEN auth shim --------------------------------------------


def test_dpo_token_required_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DSAR_DPO_TOKEN is set, missing/wrong Bearer → reject."""
    from dsar_orchestrator.local_broker.waiver import check_dpo_auth

    monkeypatch.setenv("DSAR_DPO_TOKEN", "secret123")

    assert check_dpo_auth(None) == (False, "missing Authorization header")
    assert check_dpo_auth("") == (False, "missing Authorization header")
    assert check_dpo_auth("Bearer wrong")[0] is False
    assert check_dpo_auth("Bearer secret123") == (True, None)


def test_dpo_token_open_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DSAR_DPO_TOKEN is unset, single-operator mode — always allow."""
    from dsar_orchestrator.local_broker.waiver import check_dpo_auth

    monkeypatch.delenv("DSAR_DPO_TOKEN", raising=False)

    assert check_dpo_auth(None) == (True, None)
    assert check_dpo_auth("Bearer anything") == (True, None)


# ---- route phase gating --------------------------------------------------


def test_waiver_routes_gated_by_release_phase() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    in_redact = {"current_stage": "redaction_running"}
    allowed, _ = is_route_accessible(in_redact, "/waiver")
    assert allowed is False
    allowed, _ = is_route_accessible(in_redact, "/waiver/dpo")
    assert allowed is False

    in_release = {"current_stage": "human_review_pending"}
    allowed, _ = is_route_accessible(in_release, "/waiver")
    assert allowed is True
    allowed, _ = is_route_accessible(in_release, "/waiver/dpo")
    assert allowed is True


# ---- render smoke tests --------------------------------------------------


def test_render_waiver_includes_pending_and_propose_form(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext, render_waiver

    # Plant an approver verdict with an open CRITICAL blocker so the
    # propose form has rows to render.
    (case_dir / "audit" / "approver-decisions.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-27T00:00:00Z",
                "decision": {
                    "verdict": "blocked",
                    "blocking_issues": [
                        {
                            "issue_id": "BLK-99",
                            "severity": "CRITICAL",
                            "summary": "leaked third-party PII not redacted",
                        }
                    ],
                },
            }
        )
        + "\n"
    )

    ctx = CaseContext(case_dir=case_dir)
    wv.propose_waiver(
        ctx,
        blocker_ids=["BLK-42"],
        justification="explicit operator rationale",
        operator_id="stu",
    )
    body = render_waiver(ctx, None)
    assert "Hard-Blocker Waiver" in body
    assert "BLK-42" in body
    assert "BLK-99" in body
    assert "/api/waiver/propose" in body
    assert "DPO co-sign page" in body


def test_render_waiver_dpo_lists_pending_with_cosign_form(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker import waiver as wv
    from dsar_orchestrator.operator_console import CaseContext, render_waiver_dpo

    ctx = CaseContext(case_dir=case_dir)
    proposed = wv.propose_waiver(
        ctx,
        blocker_ids=["BLK-77"],
        justification="rationale",
        operator_id="stu",
    )
    body = render_waiver_dpo(ctx, None)
    assert proposed["waiver_id"] in body
    assert "/api/waiver/cosign" in body
    assert "rationale" in body
