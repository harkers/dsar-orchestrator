"""#115 — ambiguous-flag review screen (cluster-mode + per-instance expand).

Operator clusters entities with ``redact == 'flag'`` by ``(text,
classification)`` across all ``<case>/working/<ref>_tags.json`` files and
applies a single verdict (``redact`` / ``preserve`` / ``escalate``) to
every instance in the cluster. One audit-chain event per cluster
decision; one row per decision in ``audit/flag_review_decisions.jsonl``.
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


def _write_tag_file(case_dir: Path, ref: str, entities: list[dict], **top_level) -> None:
    payload = {
        "ref": ref,
        "filename": top_level.get("filename", f"{ref}.docx"),
        "entity_count": len(entities),
        "redact_count": sum(1 for e in entities if e.get("redact") is True),
        "flag_count": sum(1 for e in entities if e.get("redact") == "flag"),
        "entities": entities,
    }
    payload.update(top_level)
    (case_dir / "working" / f"{ref}_tags.json").write_text(json.dumps(payload))


def _read_tag_file(case_dir: Path, ref: str) -> dict:
    return json.loads((case_dir / "working" / f"{ref}_tags.json").read_text())


# --------------------------------------------------------------------------
# cluster_flags
# --------------------------------------------------------------------------


def test_cluster_flags_groups_by_text_and_classification(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import cluster_flags

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 10,
                "end": 14,
                "classification": "organisation",
                "redact": "flag",
            },
            {
                "text": "Bob",
                "start": 20,
                "end": 23,
                "classification": "third_party",
                "redact": True,
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Acme",
                "start": 5,
                "end": 9,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )

    clusters = cluster_flags(case_dir)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["text"] == "Acme"
    assert c["classification"] == "organisation"
    assert c["instance_count"] == 2
    assert sorted(c["doc_refs"]) == ["doc_a", "doc_b"]


def test_cluster_flags_ignores_non_flag_entries(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import cluster_flags

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {"text": "Bob", "start": 0, "end": 3, "classification": "third_party", "redact": True},
            {
                "text": "Jane",
                "start": 5,
                "end": 9,
                "classification": "data_subject",
                "redact": False,
            },
        ],
    )
    assert cluster_flags(case_dir) == []


def test_cluster_flags_sorted_by_instance_count_desc(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import cluster_flags

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Rare",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
            {
                "text": "Common",
                "start": 5,
                "end": 11,
                "classification": "organisation",
                "redact": "flag",
            },
            {
                "text": "Common",
                "start": 20,
                "end": 26,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Common",
                "start": 0,
                "end": 6,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )

    clusters = cluster_flags(case_dir)
    assert [c["text"] for c in clusters] == ["Common", "Rare"]
    assert clusters[0]["instance_count"] == 3
    assert clusters[1]["instance_count"] == 1


def test_cluster_flags_same_text_different_classification_are_distinct(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import cluster_flags

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 0,
                "end": 5,
                "classification": "third_party",
                "redact": "flag",
            },
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    clusters = cluster_flags(case_dir)
    assert len(clusters) == 2
    classes = {c["classification"] for c in clusters}
    assert classes == {"third_party", "organisation"}


def test_cluster_flags_returns_empty_when_no_tags(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import cluster_flags

    assert cluster_flags(case_dir) == []


def test_cluster_flags_skips_corrupt_tag_files(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import cluster_flags

    (case_dir / "working" / "doc_bad_tags.json").write_text("not json {{")
    _write_tag_file(
        case_dir,
        "doc_good",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    clusters = cluster_flags(case_dir)
    assert len(clusters) == 1
    assert clusters[0]["text"] == "Acme"


def test_cluster_flags_filters_decided_clusters(case_dir: Path) -> None:
    """Clusters that already have a final decision (redact/preserve) drop
    out because the tag entries have been rewritten away from 'flag'.
    Escalated clusters with the tag still 'flag' remain visible but are
    annotated with their last decision."""
    from dsar_orchestrator.local_broker.flag_review import cluster_flags, decide_cluster

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Globex",
                "start": 0,
                "end": 6,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )

    # Resolve Acme as redact — its entry is rewritten; cluster disappears.
    decide_cluster(
        case_dir,
        text="Acme",
        classification="organisation",
        verdict="redact",
        reason_code="R001",
        note="",
        operator_id="op1",
    )
    clusters = cluster_flags(case_dir)
    assert [c["text"] for c in clusters] == ["Globex"]


# --------------------------------------------------------------------------
# decide_cluster — propagation + chain emit + JSONL append
# --------------------------------------------------------------------------


def test_decide_cluster_redact_rewrites_all_instances(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_cluster

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
            {
                "text": "Acme",
                "start": 50,
                "end": 54,
                "classification": "organisation",
                "redact": "flag",
            },
            {
                "text": "Bob",
                "start": 20,
                "end": 23,
                "classification": "third_party",
                "redact": True,
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Acme",
                "start": 5,
                "end": 9,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )

    decide_cluster(
        case_dir,
        text="Acme",
        classification="organisation",
        verdict="redact",
        reason_code="R001",
        note="confirmed third party",
        operator_id="op1",
    )

    a = _read_tag_file(case_dir, "doc_a")
    b = _read_tag_file(case_dir, "doc_b")
    acme_entries_a = [e for e in a["entities"] if e["text"] == "Acme"]
    acme_entries_b = [e for e in b["entities"] if e["text"] == "Acme"]
    assert all(e["redact"] is True for e in acme_entries_a)
    assert all(e["redact"] is True for e in acme_entries_b)
    # Unrelated entries untouched
    bob = next(e for e in a["entities"] if e["text"] == "Bob")
    assert bob["redact"] is True
    assert bob["classification"] == "third_party"


def test_decide_cluster_preserve_sets_redact_false(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_cluster

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    decide_cluster(
        case_dir,
        text="Acme",
        classification="organisation",
        verdict="preserve",
        reason_code="R002",
        note="company name, not personal data",
        operator_id="op1",
    )
    a = _read_tag_file(case_dir, "doc_a")
    assert a["entities"][0]["redact"] is False


def test_decide_cluster_escalate_leaves_tag_unchanged(case_dir: Path) -> None:
    """Escalation defers the decision — tag entries keep 'flag' so the
    cluster stays visible. Chain event still emitted."""
    from dsar_orchestrator.local_broker.flag_review import decide_cluster

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    decide_cluster(
        case_dir,
        text="Acme",
        classification="organisation",
        verdict="escalate",
        reason_code="R009",
        note="needs DPO review",
        operator_id="op1",
    )
    a = _read_tag_file(case_dir, "doc_a")
    assert a["entities"][0]["redact"] == "flag"


def test_decide_cluster_rejects_unknown_verdict(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_cluster

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    with pytest.raises(ValueError, match="unknown verdict"):
        decide_cluster(
            case_dir,
            text="Acme",
            classification="organisation",
            verdict="bogus",
            reason_code="R001",
            note="",
            operator_id="op1",
        )


def test_decide_cluster_writes_decisions_jsonl(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_cluster

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    decide_cluster(
        case_dir,
        text="Acme",
        classification="organisation",
        verdict="redact",
        reason_code="R001",
        note="confirmed third party",
        operator_id="op1",
    )

    rows = [
        json.loads(line)
        for line in (case_dir / "audit" / "flag_review_decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    r = rows[0]
    assert r["text"] == "Acme"
    assert r["classification"] == "organisation"
    assert r["verdict"] == "redact"
    assert r["reason_code"] == "R001"
    assert r["note"] == "confirmed third party"
    assert r["operator_id"] == "op1"
    assert r["doc_refs"] == ["doc_a"]
    assert r["instance_count"] == 1
    assert "ts" in r


def test_decide_cluster_emits_one_chain_event(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_cluster

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
            {
                "text": "Acme",
                "start": 50,
                "end": 54,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Acme",
                "start": 5,
                "end": 9,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    decide_cluster(
        case_dir,
        text="Acme",
        classification="organisation",
        verdict="redact",
        reason_code="R001",
        note="",
        operator_id="op1",
    )
    events = [
        json.loads(line)
        for line in (case_dir / "working" / "audit_events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    flag_events = [
        e
        for e in events
        if e.get("stage") == "flag_review" and e.get("event_type") == "reviewer_decision_made"
    ]
    assert len(flag_events) == 1
    ev = flag_events[0]
    assert ev["text"] == "Acme"
    assert ev["classification"] == "organisation"
    assert ev["verdict"] == "redact"
    assert ev["instance_count"] == 3
    assert sorted(ev["doc_refs"]) == ["doc_a", "doc_b"]


def test_decide_cluster_jsonl_failure_emits_compensating_event(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chain event lands but JSONL append fails ⇒ compensating
    FAILURE_RECORDED event with original_event_hash. Mirrors #114."""
    from dsar_orchestrator.local_broker import flag_review

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )

    real_open = Path.open

    def boom(self: Path, *args, **kwargs):
        if self.name == "flag_review_decisions.jsonl" and "a" in (
            args[0] if args else kwargs.get("mode", "")
        ):
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", boom)

    with pytest.raises(OSError, match="disk full"):
        flag_review.decide_cluster(
            case_dir,
            text="Acme",
            classification="organisation",
            verdict="redact",
            reason_code="R001",
            note="",
            operator_id="op1",
        )

    monkeypatch.undo()
    events = [
        json.loads(line)
        for line in (case_dir / "working" / "audit_events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    failure_events = [e for e in events if e["event_type"] == "failure_recorded"]
    assert len(failure_events) == 1
    f = failure_events[0]
    assert f["phase"] == "post-chain-jsonl-write"
    assert f["original_event_hash"]
    assert f["error_type"] == "OSError"


def test_decide_cluster_tag_write_failure_emits_partial_compensation(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chain event lands → first tag rewrite succeeds → second rewrite
    raises. Must emit a compensating FAILURE_RECORDED event naming the
    rewritten and failed paths so audit_verify can see the half-applied
    state. Re-raises the OSError."""
    from dsar_orchestrator.local_broker import flag_review

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Acme",
                "start": 5,
                "end": 9,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )

    real_replace = Path.replace
    calls = {"n": 0}

    def boom(self: Path, target):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        flag_review.decide_cluster(
            case_dir,
            text="Acme",
            classification="organisation",
            verdict="redact",
            reason_code="R001",
            note="",
            operator_id="op1",
        )
    monkeypatch.undo()

    events = [
        json.loads(line)
        for line in (case_dir / "working" / "audit_events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    failure_events = [e for e in events if e["event_type"] == "failure_recorded"]
    assert len(failure_events) == 1
    f = failure_events[0]
    assert f["phase"] == "tag-file-rewrite-partial"
    assert f["original_event_hash"]
    assert f["error_type"] == "OSError"
    assert f["verdict"] == "redact"
    # Exactly one path succeeded before the failure
    assert len(f["rewritten_so_far"]) == 1


# --------------------------------------------------------------------------
# Route gating + /flag-review render + /api/flag-review/decide
# --------------------------------------------------------------------------


def test_flag_review_route_gated_by_redact_phase() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    pre_redact = {"current_stage": "context_running"}
    allowed, _ = is_route_accessible(pre_redact, "/flag-review")
    assert allowed is False

    in_redact = {"current_stage": "redaction_qc_a_running"}
    allowed, _ = is_route_accessible(in_redact, "/flag-review")
    assert allowed is True


def test_render_flag_review_lists_clusters(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import CaseContext, render_flag_review

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Acme",
                "start": 0,
                "end": 4,
                "classification": "organisation",
                "redact": "flag",
            },
            {
                "text": "Acme",
                "start": 50,
                "end": 54,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Globex",
                "start": 0,
                "end": 6,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )

    ctx = CaseContext(case_dir=case_dir)
    body = render_flag_review(ctx, None)
    assert "Acme" in body
    assert "Globex" in body
    # Cluster count is exposed
    assert "2" in body  # Acme has 2 instances
    # Form action for the decide endpoint
    assert "/api/flag-review/decide" in body


def test_render_flag_review_empty_state(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import CaseContext, render_flag_review

    ctx = CaseContext(case_dir=case_dir)
    body = render_flag_review(ctx, None)
    assert "no" in body.lower() or "0" in body or "empty" in body.lower()
