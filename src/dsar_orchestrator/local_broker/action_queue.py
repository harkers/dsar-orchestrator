"""Operator-console action queue.

Collects pending decisions from blockers, leak-review, and
unextractable into a single queue, scores each with five factors
(risk, SLA proximity, stage-position, operator fatigue, category
diversity), and exposes ``next_best_review()`` for the landing-page
banner.

Per the v3 jury synthesis (chat juror, sharpest point): "pipeline
integrity beats speed". The queue MUST NOT surface items from phases
the case hasn't reached — stage-rail enforcement (#106) filters those
out at the collection stage, not the scoring stage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

log = logging.getLogger("action-queue")

# UK GDPR Art 15: 1 month from receipt. Treat as 30 days for SLA math.
_SLA_DAYS = 30

# Score weights — five-factor formula collapsed from the original 10.
_W_RISK = 0.40
_W_SLA = 0.30
_W_STAGE = 0.15
_W_FATIGUE = 0.10  # subtracted
_W_DIVERSITY = 0.05  # added

# How many recent decisions count toward fatigue / diversity.
_RECENT_WINDOW = 3

# Severity → 0-10 risk score for approver blockers.
_SEVERITY_RISK = {
    "CRITICAL": 10,
    "HIGH": 7,
    "MEDIUM": 4,
    "LOW": 2,
    "INFO": 1,
}


@dataclass(frozen=True)
class ActionItem:
    kind: str  # "blocker" | "leak_review" | "unextractable"
    item_id: str
    label: str
    risk: int  # 0-10
    stage: str  # stage key the item belongs to
    age_hours: float
    detail_url: str


@dataclass(frozen=True)
class ScoredAction:
    item: ActionItem
    score: float
    breakdown: dict = field(default_factory=dict)


def _phase_index_for_stage(stage: str) -> int:
    """0-based phase index for a stage. Imported lazily to avoid a
    circular import with operator_console."""
    from dsar_orchestrator.operator_console import PHASES

    for i, phase in enumerate(PHASES):
        if stage in phase["stages"]:
            return i
    return 0


def _current_phase_index(state: dict) -> int:
    from dsar_orchestrator.operator_console import current_phase_key, PHASES

    cur_key = current_phase_key(state)
    for i, phase in enumerate(PHASES):
        if phase["key"] == cur_key:
            return i
    return 0


def _sla_days_remaining(case_dir: Path) -> int | None:
    """Days remaining until the statutory deadline, or None if no
    request_received_date in data_subject.json."""
    ds = case_dir / "working" / "data_subject.json"
    if not ds.exists():
        return None
    try:
        data = json.loads(ds.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("request_received_date") or data.get("received_date")
    if not raw:
        return None
    try:
        received = date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    elapsed = (datetime.now(UTC).date() - received).days
    return _SLA_DAYS - elapsed


def _collect_blockers(case_dir: Path) -> list[ActionItem]:
    """Pull unresolved blockers from the latest approver verdict."""
    from dsar_orchestrator.operator_console import (
        latest_approver_verdict,
        CaseContext,
    )

    ctx = CaseContext(case_dir=case_dir)
    last = latest_approver_verdict(ctx)
    if not last:
        return []
    blocking = last.get("decision", {}).get("blocking_issues", []) or []
    state_path = case_dir / "audit" / "operator_console_state.json"
    resolved: dict = {}
    if state_path.exists():
        try:
            resolved = json.loads(state_path.read_text(encoding="utf-8")).get(
                "resolved_blockers", {}
            )
        except (OSError, json.JSONDecodeError):
            resolved = {}
    out: list[ActionItem] = []
    for b in blocking:
        bid = b.get("issue_id", "")
        if not bid or bid in resolved:
            continue
        risk = _SEVERITY_RISK.get(b.get("severity", "").upper(), 5)
        out.append(
            ActionItem(
                kind="blocker",
                item_id=bid,
                label=b.get("issue", bid),
                risk=risk,
                stage="release_gate_running",
                age_hours=_age_hours(last.get("ts", "")),
                detail_url="/blockers",
            )
        )
    return out


def _collect_leak_failures(case_dir: Path) -> list[ActionItem]:
    p = case_dir / "working" / "redaction_decisions.jsonl"
    if not p.exists():
        return []
    decisions_path = case_dir / "audit" / "leak_review_decisions.jsonl"
    decided: set[str] = set()
    if decisions_path.exists():
        for line in decisions_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed JSONL row: %s", exc)
                continue
            if row.get("decision") not in (None, "pending") and row.get("doc_ref"):
                decided.add(row["doc_ref"])
    out: list[ActionItem] = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") != "failed":
            continue
        ref = row.get("doc_ref", "")
        if not ref or ref in decided:
            continue
        out.append(
            ActionItem(
                kind="leak_review",
                item_id=ref,
                label=row.get("filename", ref),
                risk=6,  # leak failures are HIGH-ish by default
                stage="redaction_running",
                age_hours=0.0,
                detail_url="/leak-review",
            )
        )
    return out


def _collect_unextractable(case_dir: Path) -> list[ActionItem]:
    input_path = case_dir / "working" / "agent01_input.jsonl"
    if not input_path.exists():
        return []
    ingested_path = case_dir / "working" / "ingested_items.jsonl"
    ingested: set[str] = set()
    if ingested_path.exists():
        for line in ingested_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed JSONL row: %s", exc)
                continue
            p = row.get("source_location", {}).get("path")
            if p:
                ingested.add(p)
    decisions_path = case_dir / "audit" / "unextractable_decisions.jsonl"
    decided: set[str] = set()
    if decisions_path.exists():
        for line in decisions_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed JSONL row: %s", exc)
                continue
            if row.get("decision") not in (None, "pending") and row.get("source_path"):
                decided.add(row["source_path"])
    out: list[ActionItem] = []
    for line in input_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = row.get("path", "")
        if not path or path in ingested or path in decided:
            continue
        out.append(
            ActionItem(
                kind="unextractable",
                item_id=path,
                label=Path(path).name,
                risk=4,
                stage="ingestion_qc_running",
                age_hours=0.0,
                detail_url="/unextractable",
            )
        )
    return out


def _age_hours(iso_ts: str) -> float:
    if not iso_ts:
        return 0.0
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - ts).total_seconds() / 3600.0)


def collect_pending_actions(case_dir: Path, state: dict) -> list[ActionItem]:
    """Pull pending actions from all sources, filtered to phases that
    the case has reached."""
    cur_phase_idx = _current_phase_index(state)
    raw = (
        _collect_blockers(case_dir)
        + _collect_leak_failures(case_dir)
        + _collect_unextractable(case_dir)
    )
    return [item for item in raw if _phase_index_for_stage(item.stage) <= cur_phase_idx]


def score_action(
    item: ActionItem,
    state: dict,
    case_dir: Path,
    *,
    recent_decisions: list[str],
) -> ScoredAction:
    """Compute the weighted priority score for one item.

    ``recent_decisions`` is a list of ``kind`` strings — most recent
    first — used for fatigue penalty and diversity bonus.
    """
    risk_norm = item.risk / 10.0
    sla_remaining = _sla_days_remaining(case_dir)
    if sla_remaining is None:
        sla_norm = 0.5  # neutral if no SLA data
    else:
        # Smaller = more urgent. Map 30 days → 0, 0 days → 1, < 0 → 1.
        sla_norm = max(0.0, min(1.0, 1.0 - (sla_remaining / _SLA_DAYS)))

    cur_phase_idx = _current_phase_index(state)
    item_phase_idx = _phase_index_for_stage(item.stage)
    # Earlier phases score higher (operator should clear those first)
    if cur_phase_idx == 0:
        stage_norm = 1.0
    else:
        stage_norm = max(0.0, 1.0 - (item_phase_idx / cur_phase_idx))

    recent = recent_decisions[:_RECENT_WINDOW]
    fatigue_penalty = recent.count(item.kind) / _RECENT_WINDOW if recent else 0.0
    diversity_bonus = 0.0
    if recent and all(k == recent[0] for k in recent) and recent[0] != item.kind:
        diversity_bonus = 1.0

    score = (
        _W_RISK * risk_norm
        + _W_SLA * sla_norm
        + _W_STAGE * stage_norm
        - _W_FATIGUE * fatigue_penalty
        + _W_DIVERSITY * diversity_bonus
    )
    return ScoredAction(
        item=item,
        score=score,
        breakdown={
            "risk": item.risk,
            "sla_days_remaining": sla_remaining,
            "stage_position": item_phase_idx,
            "fatigue_penalty": fatigue_penalty,
            "diversity_bonus": diversity_bonus,
        },
    )


def _recent_decision_kinds(case_dir: Path, *, limit: int = _RECENT_WINDOW) -> list[str]:
    """Read the last ``limit`` operator decisions from the hash-chained
    audit events. Returns kinds (stage values) most-recent first.
    """
    events_path = case_dir / "working" / "audit_events.jsonl"
    if not events_path.exists():
        return []
    lines = events_path.read_text().splitlines()
    out: list[str] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("skipping malformed audit_events row: %s", exc)
            continue
        if ev.get("event_type") == "reviewer_decision_made":
            stage = ev.get("stage", "")
            if stage:
                out.append(stage)
    return out


def scored_queue(case_dir: Path, state: dict) -> list[ScoredAction]:
    """Collect + score + sort descending by score."""
    items = collect_pending_actions(case_dir, state)
    recent = _recent_decision_kinds(case_dir)
    scored = [score_action(item, state, case_dir, recent_decisions=recent) for item in items]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def next_best_review(case_dir: Path, state: dict) -> ScoredAction | None:
    """Top-scored pending item, or None if the queue is empty."""
    queue = scored_queue(case_dir, state)
    return queue[0] if queue else None
