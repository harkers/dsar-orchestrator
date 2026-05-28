"""Tests for the /people-register decision-writer (denylist + subject-side edits)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.local_broker.people_register_console import cluster_id
from dsar_orchestrator.local_broker.people_register_decisions import (
    DecisionError,
    record_decision,
    write_denylist,
)


def _seed_register(tmp_path: Path, clusters: list[dict]) -> Path:
    working = tmp_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    (working / "people_register.json").write_text(json.dumps(clusters))
    (working / "data_subject.json").write_text(
        json.dumps(
            {
                "full_name": "Subject A",
                "email": "subject.a@example.com",
                "aliases": [],
                "additional_emails": [],
                "subject_protected_phrases": [],
            }
        )
    )
    return tmp_path


def _cluster(name: str, **kw) -> dict:
    base = {
        "canonical_name": name,
        "emails": [],
        "phones": [],
        "titles": [],
        "source_refs": [f"r-{name}"],
        "correlation_ids": [],
        "mention_count": 1,
        "distinct_doc_count": 1,
        "confidence_score": 1.0,
        "discovered_by": "exchange_flat",
        "is_data_subject": False,
        "is_subject_confidence": 0.0,
        "subject_centricity_score": 0.0,
        "text_quality_summary": "unknown",
    }
    base.update(kw)
    return base


# ---- record_decision ----


def test_accept_as_third_party_adds_to_denylist(tmp_path: Path) -> None:
    case = _seed_register(tmp_path, [_cluster("Alice Other")])
    cid = cluster_id(_cluster("Alice Other"))
    record_decision(
        case_dir=case,
        cluster_id=cid,
        action="accept_as_third_party",
        operator_id="op-1",
        controller="ProPharma",
        note="external contact",
    )
    denylist = json.loads((case / "working" / "third_party_denylist.json").read_text())
    assert denylist["schema_version"] == 1
    assert denylist["controller"] == "ProPharma"
    assert denylist["operator_id"] == "op-1"
    assert len(denylist["entries"]) == 1
    entry = denylist["entries"][0]
    assert entry["canonical_name"] == "Alice Other"
    assert entry["redact"] is True
    assert entry["operator_note"] == "external contact"
    assert entry["people_register_cluster_id"] == cid


def test_preserve_records_entry_with_redact_false(tmp_path: Path) -> None:
    """preserve = "do not redact" — entry written with redact=False."""
    case = _seed_register(tmp_path, [_cluster("ProPharma Main Line", phones=["+44 1748 828800"])])
    cid = cluster_id(_cluster("ProPharma Main Line"))
    record_decision(
        case_dir=case,
        cluster_id=cid,
        action="preserve",
        operator_id="op-1",
        controller="ProPharma",
        note="controller's published main number",
    )
    denylist = json.loads((case / "working" / "third_party_denylist.json").read_text())
    entry = denylist["entries"][0]
    assert entry["redact"] is False
    assert entry["operator_note"] == "controller's published main number"


def test_mark_subject_alias_moves_cluster_to_subject(tmp_path: Path) -> None:
    """mark_subject_alias: cluster's canonical_name + emails + phones get
    folded into data_subject.json. Cluster's is_data_subject flips to True.
    The cluster does NOT appear in the denylist (it's the subject, not a
    third party)."""
    case = _seed_register(
        tmp_path,
        [_cluster("S. A", emails=["s.a@example.com"], phones=["+44 20 4557 6072"])],
    )
    cid = cluster_id(_cluster("S. A"))
    record_decision(
        case_dir=case,
        cluster_id=cid,
        action="mark_subject_alias",
        operator_id="op-1",
        controller="ProPharma",
        note="operator-confirmed alias",
    )

    # data_subject.json updated
    ds = json.loads((case / "working" / "data_subject.json").read_text())
    assert "S. A" in ds["aliases"]
    assert "s.a@example.com" in ds["additional_emails"]
    # Phones preserved into subject_phones (Phase 3 extension; GLM finding)
    assert "+44 20 4557 6072" in ds.get("subject_phones", [])

    # people_register.json: cluster now is_data_subject=True
    register = json.loads((case / "working" / "people_register.json").read_text())
    sa = next(c for c in register if c["canonical_name"] == "S. A")
    assert sa["is_data_subject"] is True

    # third_party_denylist.json: no entry for the now-subject cluster
    dl_path = case / "working" / "third_party_denylist.json"
    if dl_path.exists():
        denylist = json.loads(dl_path.read_text())
        names = {e["canonical_name"] for e in denylist["entries"]}
        assert "S. A" not in names


def test_merge_with_aliases_cluster(tmp_path: Path) -> None:
    """merge_with: source cluster's identifiers fold into the target
    cluster (emails/phones/titles unioned; source cluster removed)."""
    target = _cluster("Alice Other", emails=["alice@example.com"])
    source = _cluster("A. Other", emails=["alice.other@example.com"])
    case = _seed_register(tmp_path, [target, source])
    source_cid = cluster_id(source)
    target_cid = cluster_id(target)

    record_decision(
        case_dir=case,
        cluster_id=source_cid,
        action="merge_with",
        operator_id="op-1",
        controller="ProPharma",
        merge_target_id=target_cid,
        note="alias",
    )

    register = json.loads((case / "working" / "people_register.json").read_text())
    canonical_names = {c["canonical_name"] for c in register}
    assert "A. Other" not in canonical_names  # source removed
    alice = next(c for c in register if c["canonical_name"] == "Alice Other")
    assert "alice@example.com" in alice["emails"]
    assert "alice.other@example.com" in alice["emails"]


def test_mark_subject_alias_requires_data_subject_json(tmp_path: Path) -> None:
    """mark_subject_alias must refuse if data_subject.json is missing —
    Phase 1 setup must populate the subject record first; we don't want
    to write a half-formed subject file from cluster data alone."""
    working = tmp_path / "working"
    working.mkdir(parents=True)
    cluster = _cluster("Alice Other")
    (working / "people_register.json").write_text(json.dumps([cluster]))
    # NOTE: no data_subject.json
    cid = cluster_id(cluster)
    with pytest.raises(DecisionError, match="data_subject.json"):
        record_decision(
            case_dir=tmp_path,
            cluster_id=cid,
            action="mark_subject_alias",
            operator_id="op-1",
            controller="ProPharma",
        )


def test_unknown_cluster_id_raises(tmp_path: Path) -> None:
    case = _seed_register(tmp_path, [_cluster("Alice")])
    with pytest.raises(DecisionError):
        record_decision(
            case_dir=case,
            cluster_id="0" * 16,
            action="accept_as_third_party",
            operator_id="op-1",
            controller="ProPharma",
        )


def test_invalid_action_raises(tmp_path: Path) -> None:
    case = _seed_register(tmp_path, [_cluster("Alice")])
    cid = cluster_id(_cluster("Alice"))
    with pytest.raises(DecisionError):
        record_decision(
            case_dir=case,
            cluster_id=cid,
            action="nonsense_action",
            operator_id="op-1",
            controller="ProPharma",
        )


def test_merge_without_target_raises(tmp_path: Path) -> None:
    """merge_with requires merge_target_id."""
    case = _seed_register(tmp_path, [_cluster("Alice")])
    cid = cluster_id(_cluster("Alice"))
    with pytest.raises(DecisionError, match="merge_target"):
        record_decision(
            case_dir=case,
            cluster_id=cid,
            action="merge_with",
            operator_id="op-1",
            controller="ProPharma",
        )


def test_repeated_decision_updates_entry(tmp_path: Path) -> None:
    """Operator changes their mind: first accept, then preserve — denylist
    keeps ONE entry per cluster, with the latest verdict."""
    case = _seed_register(tmp_path, [_cluster("Alice Other")])
    cid = cluster_id(_cluster("Alice Other"))
    record_decision(
        case_dir=case,
        cluster_id=cid,
        action="accept_as_third_party",
        operator_id="op-1",
        controller="ProPharma",
    )
    record_decision(
        case_dir=case,
        cluster_id=cid,
        action="preserve",
        operator_id="op-1",
        controller="ProPharma",
        note="oops",
    )
    denylist = json.loads((case / "working" / "third_party_denylist.json").read_text())
    assert len(denylist["entries"]) == 1
    assert denylist["entries"][0]["redact"] is False
    assert denylist["entries"][0]["operator_note"] == "oops"


# ---- write_denylist directly ----


def test_write_denylist_schema_v1(tmp_path: Path) -> None:
    """write_denylist produces a schema-conformant file."""
    case = _seed_register(tmp_path, [])
    entries = [
        {
            "canonical_name": "Alice",
            "redact": True,
            "operator_note": "x",
            "people_register_cluster_id": "abc123",
        },
    ]
    write_denylist(
        case_dir=case,
        controller="ProPharma",
        operator_id="op-1",
        entries=entries,
    )
    out = json.loads((case / "working" / "third_party_denylist.json").read_text())
    assert out["schema_version"] == 1
    assert out["controller"] == "ProPharma"
    assert out["operator_id"] == "op-1"
    assert "populated_at" in out
    # ISO-8601 timestamp with TZ
    assert out["populated_at"].endswith("Z") or "+" in out["populated_at"]
    assert out["entries"] == entries


def test_write_denylist_atomic(tmp_path: Path) -> None:
    """Atomic write via tmp + replace — no .tmp leftover on success."""
    case = _seed_register(tmp_path, [])
    write_denylist(case_dir=case, controller="X", operator_id="op-1", entries=[])
    leftovers = list((case / "working").glob("third_party_denylist.json.tmp*"))
    assert leftovers == []


# ---- HTTP POST smoke test ----


def test_render_people_register_includes_action_buttons(tmp_path: Path) -> None:
    """The render now shows accept_as_third_party / preserve / merge_with /
    mark_subject_alias action buttons on each cluster row."""
    from dsar_orchestrator.operator_console import (
        CaseContext,
        render_people_register,
    )

    _seed_register(tmp_path, [_cluster("Alice Other")])
    ctx = CaseContext(case_dir=tmp_path)
    html_out = render_people_register(ctx, action_result=None)
    assert "accept_as_third_party" in html_out
    assert "preserve" in html_out
    assert "mark_subject_alias" in html_out
    assert "/api/people-register/decide" in html_out
