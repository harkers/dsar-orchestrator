"""#115b — per-instance expand on the ambiguous-flag review screen.

For the rare cluster where the same ``(text, classification)`` carries
different meaning across docs (e.g. "Smith" as the requester in some
documents and an unrelated third party in others), the operator needs
to triage each instance individually rather than apply one cluster-wide
verdict.

``list_cluster_instances`` enumerates the matching entries with their
snippets. ``decide_instance`` records a per-instance verdict; same
chain-event + JSONL + compensating-failure machinery as the cluster
path, just keyed on ``(doc_ref, start, end)``.
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
# list_cluster_instances
# --------------------------------------------------------------------------


def test_list_cluster_instances_returns_each_match_sorted(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import list_cluster_instances

    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Smith",
                "start": 30,
                "end": 35,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 50,
                "end": 55,
                "classification": "third_party",
                "redact": "flag",
            },
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )

    instances = list_cluster_instances(case_dir, text="Smith", classification="third_party")
    assert [(i["doc_ref"], i["start"]) for i in instances] == [
        ("doc_a", 10),
        ("doc_a", 50),
        ("doc_b", 30),
    ]
    # filename surfaces for the operator
    assert all(i["filename"] for i in instances)


def test_list_cluster_instances_ignores_non_flag_and_other_clusters(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import list_cluster_instances

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            # Wrong text
            {
                "text": "Jones",
                "start": 0,
                "end": 5,
                "classification": "third_party",
                "redact": "flag",
            },
            # Wrong classification
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "organisation",
                "redact": "flag",
            },
            # Wrong redact state
            {
                "text": "Smith",
                "start": 20,
                "end": 25,
                "classification": "third_party",
                "redact": True,
            },
            # Match
            {
                "text": "Smith",
                "start": 30,
                "end": 35,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    instances = list_cluster_instances(case_dir, text="Smith", classification="third_party")
    assert len(instances) == 1
    assert instances[0]["start"] == 30


def test_list_cluster_instances_includes_snippet_when_text_file_present(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import list_cluster_instances

    # Tag at chars 14-19 of "Hello there, Smith and friends, today" → "Smith"
    text = "Hello there, Smith and friends, today"
    (case_dir / "working" / "doc_a.txt").write_text(text)
    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 13,
                "end": 18,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    instances = list_cluster_instances(case_dir, text="Smith", classification="third_party")
    assert len(instances) == 1
    inst = instances[0]
    # Snippet shows context around the entity
    assert inst["snippet_before"].endswith(", ")
    assert inst["snippet_after"].startswith(" and")


def test_list_cluster_instances_handles_missing_text_file(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import list_cluster_instances

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
        ],
    )
    instances = list_cluster_instances(case_dir, text="Smith", classification="third_party")
    assert len(instances) == 1
    # Snippets default to empty strings when the text file is missing
    assert instances[0]["snippet_before"] == ""
    assert instances[0]["snippet_after"] == ""


# --------------------------------------------------------------------------
# decide_instance — propagation + chain emit + JSONL append
# --------------------------------------------------------------------------


def test_decide_instance_redact_rewrites_only_that_instance(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_instance

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
            {
                "text": "Smith",
                "start": 50,
                "end": 55,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Smith",
                "start": 5,
                "end": 10,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )

    decide_instance(
        case_dir,
        doc_ref="doc_a",
        start=10,
        end=15,
        text="Smith",
        classification="third_party",
        verdict="redact",
        reason_code="R001",
        note="",
        operator_id="op1",
    )

    a = _read_tag_file(case_dir, "doc_a")
    b = _read_tag_file(case_dir, "doc_b")

    e1 = next(e for e in a["entities"] if e["start"] == 10)
    e2 = next(e for e in a["entities"] if e["start"] == 50)
    e3 = next(e for e in b["entities"] if e["start"] == 5)

    assert e1["redact"] is True
    assert e2["redact"] == "flag"  # untouched
    assert e3["redact"] == "flag"  # different doc, untouched


def test_decide_instance_preserve_sets_false(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_instance

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    decide_instance(
        case_dir,
        doc_ref="doc_a",
        start=10,
        end=15,
        text="Smith",
        classification="third_party",
        verdict="preserve",
        reason_code="R002",
        note="",
        operator_id="op1",
    )
    a = _read_tag_file(case_dir, "doc_a")
    assert a["entities"][0]["redact"] is False


def test_decide_instance_escalate_leaves_flag(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_instance

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    decide_instance(
        case_dir,
        doc_ref="doc_a",
        start=10,
        end=15,
        text="Smith",
        classification="third_party",
        verdict="escalate",
        reason_code="R009",
        note="needs DPO review",
        operator_id="op1",
    )
    a = _read_tag_file(case_dir, "doc_a")
    assert a["entities"][0]["redact"] == "flag"


def test_decide_instance_rejects_unknown_verdict(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_instance

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    with pytest.raises(ValueError, match="unknown verdict"):
        decide_instance(
            case_dir,
            doc_ref="doc_a",
            start=10,
            end=15,
            text="Smith",
            classification="third_party",
            verdict="bogus",
            reason_code="R001",
            note="",
            operator_id="op1",
        )


def test_decide_instance_rejects_path_traversal_doc_ref(case_dir: Path) -> None:
    """``doc_ref`` is operator-supplied via HTTP form. Reject path
    segments and leading dots so a malicious payload can't reach a tag
    file outside ``case_dir/working/``."""
    from dsar_orchestrator.local_broker.flag_review import decide_instance

    for bad in ("../etc/passwd", "foo/bar", ".hidden", "", "a\\b"):
        with pytest.raises(ValueError, match="invalid doc_ref"):
            decide_instance(
                case_dir,
                doc_ref=bad,
                start=0,
                end=1,
                text="X",
                classification="organisation",
                verdict="redact",
                reason_code="R001",
                note="",
                operator_id="op1",
            )


def test_decide_instance_raises_when_no_match(case_dir: Path) -> None:
    """If the (doc_ref, start, end) doesn't point at a flag entity with
    the named (text, classification), the call must raise — the operator
    is acting on a stale page and we should not silently no-op."""
    from dsar_orchestrator.local_broker.flag_review import decide_instance

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    with pytest.raises(ValueError, match="no matching flag entity"):
        decide_instance(
            case_dir,
            doc_ref="doc_a",
            start=99,
            end=104,
            text="Smith",
            classification="third_party",
            verdict="redact",
            reason_code="R001",
            note="",
            operator_id="op1",
        )


def test_decide_instance_emits_chain_event_and_jsonl(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.flag_review import decide_instance

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    decide_instance(
        case_dir,
        doc_ref="doc_a",
        start=10,
        end=15,
        text="Smith",
        classification="third_party",
        verdict="redact",
        reason_code="R001",
        note="this instance is a third party",
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
    assert ev["scope"] == "instance"
    assert ev["doc_ref"] == "doc_a"
    assert ev["start"] == 10
    assert ev["end"] == 15
    assert ev["text"] == "Smith"
    assert ev["verdict"] == "redact"

    rows = [
        json.loads(line)
        for line in (case_dir / "audit" / "flag_review_decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    r = rows[0]
    assert r["scope"] == "instance"
    assert r["doc_ref"] == "doc_a"
    assert r["start"] == 10
    assert r["end"] == 15
    assert r["verdict"] == "redact"
    assert r["reason_code"] == "R001"
    assert r["operator_id"] == "op1"


def test_decide_instance_jsonl_failure_emits_compensating_event(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsar_orchestrator.local_broker import flag_review

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
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
        flag_review.decide_instance(
            case_dir,
            doc_ref="doc_a",
            start=10,
            end=15,
            text="Smith",
            classification="third_party",
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
    assert f["scope"] == "instance"
    assert f["doc_ref"] == "doc_a"
    assert f["start"] == 10


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def test_flag_review_cluster_route_gated_by_redact_phase() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    pre_redact = {"current_stage": "context_running"}
    allowed, _ = is_route_accessible(pre_redact, "/flag-review/cluster")
    assert allowed is False

    in_redact = {"current_stage": "redaction_qc_a_running"}
    allowed, _ = is_route_accessible(in_redact, "/flag-review/cluster")
    assert allowed is True


def test_render_flag_review_cluster_shows_instances(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import CaseContext, render_flag_review_cluster

    (case_dir / "working" / "doc_a.txt").write_text("Email from Smith about the project")
    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 11,
                "end": 16,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {
                "text": "Smith",
                "start": 5,
                "end": 10,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )

    ctx = CaseContext(case_dir=case_dir)
    body = render_flag_review_cluster(
        ctx, text="Smith", classification="third_party", action_result=None
    )
    # Both refs appear
    assert "doc_a" in body
    assert "doc_b" in body
    # Each instance posts to the per-instance endpoint
    assert body.count("/api/flag-review/decide-instance") >= 2
    # Cluster summary at top
    assert "Smith" in body
    assert "third_party" in body


def test_render_flag_review_cluster_empty_message(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import CaseContext, render_flag_review_cluster

    ctx = CaseContext(case_dir=case_dir)
    body = render_flag_review_cluster(
        ctx, text="Nothing", classification="organisation", action_result=None
    )
    assert "no" in body.lower() or "empty" in body.lower()


def test_main_flag_review_links_to_cluster_expand(case_dir: Path) -> None:
    """The cluster cards on /flag-review include a link to the per-instance
    expand view, with the cluster's text + classification in the query string."""
    from dsar_orchestrator.operator_console import CaseContext, render_flag_review

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Smith",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": "flag",
            },
        ],
    )
    ctx = CaseContext(case_dir=case_dir)
    body = render_flag_review(ctx, None)
    assert "/flag-review/cluster" in body
