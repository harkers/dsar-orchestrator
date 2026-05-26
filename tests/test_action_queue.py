"""Tests for the operator-console action queue: collects pending
decisions from blockers / leak-review / unextractable; scores them
with risk + SLA + stage-position + fatigue + diversity; exposes a
"Next Best Review" pick.
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
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps(
            {
                "case_no": "TEST-100",
                "request_received_date": "2026-05-10",
            }
        )
    )
    return tmp_path


def _state(stage: str) -> dict:
    return {"current_stage": stage, "history": []}


def _write_approver_verdict(case_dir: Path, blocking_issues: list[dict]) -> None:
    """Write a synthetic approver verdict with the given blockers."""
    p = case_dir / "audit" / "approver-decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "ts": "2026-05-25T12:00:00Z",
                "decision": {
                    "verdict": "REJECT",
                    "blocking_issues": blocking_issues,
                },
            }
        )
        + "\n"
    )


def _write_failed_redaction(case_dir: Path, ref: str) -> None:
    p = case_dir / "working" / "redaction_decisions.jsonl"
    line = json.dumps({"doc_ref": ref, "filename": f"{ref}.msg", "status": "failed"})
    with p.open("a") as f:
        f.write(line + "\n")


def _write_unextractable(case_dir: Path, source_path: str) -> None:
    p = case_dir / "working" / "agent01_input.jsonl"
    with p.open("a") as f:
        f.write(json.dumps({"path": source_path}) + "\n")
    # ingested_items.jsonl stays empty → diff puts source_path in pending


# --- collect_pending_actions ---


def test_collect_returns_unresolved_blockers(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import collect_pending_actions

    _write_approver_verdict(
        case_dir,
        [
            {
                "issue_id": "BLOCK-001",
                "issue": "Identity verification missing",
                "severity": "CRITICAL",
            },
            {"issue_id": "BLOCK-002", "issue": "Statutory deadline at risk", "severity": "HIGH"},
        ],
    )
    items = collect_pending_actions(case_dir, _state("release_gate_running"))
    blocker_items = [i for i in items if i.kind == "blocker"]
    assert len(blocker_items) == 2
    assert {i.item_id for i in blocker_items} == {"BLOCK-001", "BLOCK-002"}


def test_collect_returns_failed_leak_redactions(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import collect_pending_actions

    _write_failed_redaction(case_dir, "doc-001")
    _write_failed_redaction(case_dir, "doc-002")
    items = collect_pending_actions(case_dir, _state("redaction_running"))
    leak_items = [i for i in items if i.kind == "leak_review"]
    assert len(leak_items) == 2
    assert {i.item_id for i in leak_items} == {"doc-001", "doc-002"}


def test_collect_returns_pending_unextractable(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import collect_pending_actions

    _write_unextractable(case_dir, "/data/broken.eml")
    items = collect_pending_actions(case_dir, _state("ingestion_qc_running"))
    unext_items = [i for i in items if i.kind == "unextractable"]
    assert len(unext_items) == 1
    assert unext_items[0].item_id == "/data/broken.eml"


def test_collect_filters_out_resolved_blockers(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import collect_pending_actions

    _write_approver_verdict(
        case_dir,
        [{"issue_id": "BLOCK-001", "issue": "Test", "severity": "HIGH"}],
    )
    state_path = case_dir / "audit" / "operator_console_state.json"
    state_path.write_text(
        json.dumps({"resolved_blockers": {"BLOCK-001": {"resolved_at": "2026-05-25T10:00:00Z"}}})
    )
    items = collect_pending_actions(case_dir, _state("release_gate_running"))
    assert [i for i in items if i.kind == "blocker"] == []


def test_collect_filters_out_future_phase_items(case_dir: Path) -> None:
    """While in discovery, blocker items (release phase) shouldn't appear."""
    from dsar_orchestrator.local_broker.action_queue import collect_pending_actions

    _write_approver_verdict(
        case_dir,
        [{"issue_id": "BLOCK-001", "issue": "Test", "severity": "HIGH"}],
    )
    items = collect_pending_actions(case_dir, _state("context_running"))
    assert [i for i in items if i.kind == "blocker"] == []


# --- score_action ---


def test_score_higher_risk_ranks_higher(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import ActionItem, score_action

    state = _state("release_gate_running")
    crit = ActionItem(
        kind="blocker",
        item_id="A",
        label="critical",
        risk=10,
        stage="release_gate_running",
        age_hours=1.0,
        detail_url="/blockers",
    )
    low = ActionItem(
        kind="blocker",
        item_id="B",
        label="low",
        risk=2,
        stage="release_gate_running",
        age_hours=1.0,
        detail_url="/blockers",
    )
    s_crit = score_action(crit, state, case_dir, recent_decisions=[])
    s_low = score_action(low, state, case_dir, recent_decisions=[])
    assert s_crit.score > s_low.score


def test_score_closer_sla_ranks_higher(tmp_path: Path) -> None:
    """Same risk; one case is 28 days into SLA window, one is 5 days in."""
    from dsar_orchestrator.local_broker.action_queue import ActionItem, score_action

    near_case = tmp_path / "near"
    near_case.mkdir()
    (near_case / "working").mkdir()
    (near_case / "working" / "data_subject.json").write_text(
        json.dumps(
            {
                "case_no": "NEAR",
                "request_received_date": (datetime.now(UTC) - timedelta(days=28))
                .date()
                .isoformat(),
            }
        )
    )
    far_case = tmp_path / "far"
    far_case.mkdir()
    (far_case / "working").mkdir()
    (far_case / "working" / "data_subject.json").write_text(
        json.dumps(
            {
                "case_no": "FAR",
                "request_received_date": (datetime.now(UTC) - timedelta(days=5)).date().isoformat(),
            }
        )
    )

    state = _state("release_gate_running")
    item = lambda: ActionItem(
        kind="blocker",
        item_id="X",
        label="x",
        risk=5,
        stage="release_gate_running",
        age_hours=1.0,
        detail_url="/blockers",
    )
    s_near = score_action(item(), state, near_case, recent_decisions=[])
    s_far = score_action(item(), state, far_case, recent_decisions=[])
    assert s_near.score > s_far.score
    assert s_near.breakdown["sla_days_remaining"] < s_far.breakdown["sla_days_remaining"]


def test_score_fatigue_penalty_after_same_kind_streak(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import ActionItem, score_action

    state = _state("release_gate_running")
    item = ActionItem(
        kind="blocker",
        item_id="X",
        label="x",
        risk=5,
        stage="release_gate_running",
        age_hours=1.0,
        detail_url="/blockers",
    )
    fresh = score_action(item, state, case_dir, recent_decisions=[])
    streak = score_action(item, state, case_dir, recent_decisions=["blocker", "blocker", "blocker"])
    assert streak.score < fresh.score
    assert streak.breakdown["fatigue_penalty"] > 0


def test_score_diversity_bonus_for_off_kind_after_streak(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import ActionItem, score_action

    state = _state("release_gate_running")
    off_kind = ActionItem(
        kind="leak_review",
        item_id="L",
        label="leak",
        risk=5,
        stage="redaction_running",
        age_hours=1.0,
        detail_url="/leak-review",
    )
    s = score_action(off_kind, state, case_dir, recent_decisions=["blocker", "blocker", "blocker"])
    assert s.breakdown["diversity_bonus"] > 0


# --- next_best_review ---


def test_next_best_review_returns_top_scored(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import next_best_review

    _write_approver_verdict(
        case_dir,
        [
            {"issue_id": "B1", "issue": "low", "severity": "LOW"},
            {"issue_id": "B2", "issue": "crit", "severity": "CRITICAL"},
        ],
    )
    pick = next_best_review(case_dir, _state("release_gate_running"))
    assert pick is not None
    assert pick.item.item_id == "B2"  # critical beats low


def test_next_best_review_returns_none_when_no_items(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import next_best_review

    assert next_best_review(case_dir, _state("intake_created")) is None


# --- breakdown shape (for UI rendering) ---


def test_breakdown_includes_all_five_factors(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.action_queue import ActionItem, score_action

    item = ActionItem(
        kind="blocker",
        item_id="X",
        label="x",
        risk=5,
        stage="release_gate_running",
        age_hours=1.0,
        detail_url="/blockers",
    )
    s = score_action(item, _state("release_gate_running"), case_dir, recent_decisions=["blocker"])
    for key in (
        "risk",
        "sla_days_remaining",
        "stage_position",
        "fatigue_penalty",
        "diversity_bonus",
    ):
        assert key in s.breakdown, f"{key} missing from breakdown"
