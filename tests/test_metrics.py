"""#117 — live metric refresh on decision change.

``metrics.recompute_funnel(case_dir)`` is the single source of truth for
the document-flow funnel. It's cheap (<100ms) — re-reads the relevant
JSONLs on every call — and is invoked from each ``*_decide`` POST route
after the chain emit so the funnel always reflects the current operator
decision state. It also persists a snapshot to
``audit/corpus_metrics.json`` so external tools (closure letter, audit
verifier, dashboards) can read the latest numbers without recomputing.
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


def _write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# --------------------------------------------------------------------------
# recompute_funnel — fundamental shape and edge cases
# --------------------------------------------------------------------------


def test_recompute_funnel_empty_case_returns_zero_counts(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    funnel = recompute_funnel(case_dir)
    for key in ("ingested", "in_scope", "redacted", "leak_excluded", "qa_decided", "final"):
        assert key in funnel, f"missing key {key!r}"
        assert funnel[key] == 0


def test_recompute_funnel_counts_ingested_items(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(
        case_dir / "working" / "ingested_items.jsonl",
        [{"ref": "a"}, {"ref": "b"}, {"ref": "c"}],
    )
    funnel = recompute_funnel(case_dir)
    assert funnel["ingested"] == 3


def test_recompute_funnel_in_scope_from_durant_biographical(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(
        case_dir / "working" / "durant_verdicts.jsonl",
        [
            {"doc_ref": "a", "durant_verdict": "biographical"},
            {"doc_ref": "b", "durant_verdict": "biographical"},
            {"doc_ref": "c", "durant_verdict": "work_context_only"},
            {"doc_ref": "d", "durant_verdict": "out_of_scope"},
        ],
    )
    funnel = recompute_funnel(case_dir)
    assert funnel["in_scope"] == 2


def test_recompute_funnel_counts_redacted(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(
        case_dir / "working" / "redaction_decisions.jsonl",
        [
            {"doc_ref": "a", "status": "redacted"},
            {"doc_ref": "b", "status": "redacted"},
            {"doc_ref": "c", "status": "failed"},
        ],
    )
    funnel = recompute_funnel(case_dir)
    assert funnel["redacted"] == 2


def test_recompute_funnel_counts_leak_excluded(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(
        case_dir / "audit" / "leak_review_decisions.jsonl",
        [
            {"doc_ref": "a", "decision": "accept_exclude"},
            {"doc_ref": "b", "decision": "accept_exclude"},
            {"doc_ref": "c", "decision": "include_with_note"},
            {"doc_ref": "d", "decision": "retried_ok"},
        ],
    )
    funnel = recompute_funnel(case_dir)
    assert funnel["leak_excluded"] == 2


def test_recompute_funnel_counts_qa_decided_excludes_pending(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(
        case_dir / "audit" / "qa_decisions.jsonl",
        [
            {"doc_ref": "a", "decision": "approve"},
            {"doc_ref": "b", "decision": "request_reredaction"},
            {"doc_ref": "c", "decision": "mark_false_positive"},
            {"doc_ref": "d", "decision": "pending"},
        ],
    )
    funnel = recompute_funnel(case_dir)
    assert funnel["qa_decided"] == 3


def test_recompute_funnel_final_subtracts_leak_excluded_from_redacted(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(
        case_dir / "working" / "redaction_decisions.jsonl",
        [
            {"doc_ref": "a", "status": "redacted"},
            {"doc_ref": "b", "status": "redacted"},
            {"doc_ref": "c", "status": "redacted"},
            {"doc_ref": "d", "status": "redacted"},
        ],
    )
    _write_jsonl(
        case_dir / "audit" / "leak_review_decisions.jsonl",
        [
            {"doc_ref": "a", "decision": "accept_exclude"},
        ],
    )
    funnel = recompute_funnel(case_dir)
    assert funnel["final"] == 3


def test_recompute_funnel_final_floors_at_zero(case_dir: Path) -> None:
    """If leak_excluded somehow exceeds redacted (data corruption, manual
    edits), ``final`` must clamp to 0 rather than going negative."""
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(
        case_dir / "working" / "redaction_decisions.jsonl",
        [{"doc_ref": "a", "status": "redacted"}],
    )
    _write_jsonl(
        case_dir / "audit" / "leak_review_decisions.jsonl",
        [
            {"doc_ref": "a", "decision": "accept_exclude"},
            {"doc_ref": "b", "decision": "accept_exclude"},
        ],
    )
    funnel = recompute_funnel(case_dir)
    assert funnel["final"] == 0


def test_recompute_funnel_skips_malformed_lines(case_dir: Path) -> None:
    """A corrupt line in one of the JSONLs must not crash the recompute —
    the route can't risk failing on a stray bad row."""
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    path = case_dir / "working" / "redaction_decisions.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"doc_ref": "a", "status": "redacted"}) + "\n")
        f.write("not json {{{ broken\n")
        f.write(json.dumps({"doc_ref": "c", "status": "redacted"}) + "\n")
    funnel = recompute_funnel(case_dir)
    assert funnel["redacted"] == 2


# --------------------------------------------------------------------------
# Snapshot persistence to audit/corpus_metrics.json
# --------------------------------------------------------------------------


def test_recompute_funnel_writes_snapshot_atomically(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(case_dir / "working" / "ingested_items.jsonl", [{"ref": "a"}])
    recompute_funnel(case_dir)
    snap = json.loads((case_dir / "audit" / "corpus_metrics.json").read_text())
    assert snap["funnel"]["ingested"] == 1
    assert "computed_at" in snap
    assert snap["schema_version"] == 1


def test_recompute_funnel_snapshot_updates_on_re_call(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    _write_jsonl(case_dir / "working" / "ingested_items.jsonl", [{"ref": "a"}])
    recompute_funnel(case_dir)
    snap1 = json.loads((case_dir / "audit" / "corpus_metrics.json").read_text())
    assert snap1["funnel"]["ingested"] == 1

    _write_jsonl(
        case_dir / "working" / "ingested_items.jsonl",
        [{"ref": "a"}, {"ref": "b"}],
    )
    recompute_funnel(case_dir)
    snap2 = json.loads((case_dir / "audit" / "corpus_metrics.json").read_text())
    assert snap2["funnel"]["ingested"] == 2


def test_recompute_funnel_preserves_scale_section_on_funnel_update(case_dir: Path) -> None:
    """If a corpus_metrics.json already has a 'scale' section from a prior
    ``recompute_corpus_scale``, a ``recompute_funnel`` call must not wipe
    it. The two recomputes update independent sub-trees."""
    from dsar_orchestrator.local_broker.metrics import recompute_funnel

    existing = {
        "schema_version": 1,
        "funnel": {"ingested": 0},
        "scale": {"word_count": 12345, "computed_at": "2026-05-01T00:00:00Z"},
        "computed_at": "2026-05-01T00:00:00Z",
    }
    (case_dir / "audit" / "corpus_metrics.json").write_text(json.dumps(existing))

    recompute_funnel(case_dir)
    snap = json.loads((case_dir / "audit" / "corpus_metrics.json").read_text())
    assert snap["scale"]["word_count"] == 12345


# --------------------------------------------------------------------------
# recompute_corpus_scale — lazy, heavier
# --------------------------------------------------------------------------


def test_recompute_corpus_scale_word_count_from_txt(case_dir: Path) -> None:
    """Scale recompute sums word counts from working/<ref>.txt files."""
    from dsar_orchestrator.local_broker.metrics import recompute_corpus_scale

    (case_dir / "working" / "a.txt").write_text("hello world from doc a")
    (case_dir / "working" / "b.txt").write_text("another doc")
    scale = recompute_corpus_scale(case_dir)
    assert scale["word_count"] == 7  # 5 + 2
    assert scale["doc_count"] == 2


def test_recompute_corpus_scale_persists_to_snapshot(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import recompute_corpus_scale

    (case_dir / "working" / "a.txt").write_text("hello world")
    recompute_corpus_scale(case_dir)
    snap = json.loads((case_dir / "audit" / "corpus_metrics.json").read_text())
    assert snap["scale"]["word_count"] == 2
    assert snap["scale"]["doc_count"] == 1


def test_recompute_corpus_scale_handles_unreadable_files(case_dir: Path) -> None:
    """A file that can't be read (encoding issue, race deletion) must be
    skipped, not crash the recompute."""
    from dsar_orchestrator.local_broker.metrics import recompute_corpus_scale

    (case_dir / "working" / "a.txt").write_text("hello world")
    # Binary content as a .txt with bytes that round-trip via errors='replace'
    (case_dir / "working" / "b.txt").write_bytes(b"\xff\xfe\xfd bad bytes")
    scale = recompute_corpus_scale(case_dir)
    assert scale["doc_count"] == 2
    assert scale["word_count"] >= 2


# --------------------------------------------------------------------------
# read_metrics_snapshot — non-recomputing read
# --------------------------------------------------------------------------


def test_read_metrics_snapshot_returns_none_when_missing(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import read_metrics_snapshot

    assert read_metrics_snapshot(case_dir) is None


def test_read_metrics_snapshot_returns_dict_when_present(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.metrics import read_metrics_snapshot, recompute_funnel

    _write_jsonl(case_dir / "working" / "ingested_items.jsonl", [{"ref": "a"}])
    recompute_funnel(case_dir)
    snap = read_metrics_snapshot(case_dir)
    assert snap is not None
    assert snap["funnel"]["ingested"] == 1


# --------------------------------------------------------------------------
# Decision-route wiring — failure containment + live update
# --------------------------------------------------------------------------


def test_decision_route_recompute_failure_does_not_block_decision(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``recompute_funnel`` raises (disk full, etc), the operator
    decision must still succeed — metrics are best-effort. The console
    wraps the recompute call in try/except and logs failures."""
    from dsar_orchestrator.local_broker import qa_sample

    def boom(case_dir, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "dsar_orchestrator.local_broker.metrics.recompute_funnel", boom, raising=True
    )

    # qa decision route should swallow the recompute failure via the
    # wrapper added in operator_console.py
    from dsar_orchestrator.operator_console import _safe_recompute_funnel

    # Should not raise.
    _safe_recompute_funnel(case_dir)


def test_safe_recompute_funnel_returns_dict_on_success(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import _safe_recompute_funnel

    _write_jsonl(case_dir / "working" / "ingested_items.jsonl", [{"ref": "a"}])
    funnel = _safe_recompute_funnel(case_dir)
    assert funnel is not None
    assert funnel["ingested"] == 1


# --------------------------------------------------------------------------
# Landing-page widget renders the live funnel
# --------------------------------------------------------------------------


def test_landing_page_renders_live_funnel_widget(case_dir: Path) -> None:
    from dsar_orchestrator.operator_console import CaseContext, render_landing

    _write_jsonl(
        case_dir / "working" / "ingested_items.jsonl",
        [{"ref": "a"}, {"ref": "b"}, {"ref": "c"}],
    )
    _write_jsonl(
        case_dir / "working" / "durant_verdicts.jsonl",
        [
            {"doc_ref": "a", "durant_verdict": "biographical"},
            {"doc_ref": "b", "durant_verdict": "biographical"},
        ],
    )
    ctx = CaseContext(case_dir=case_dir)
    state = {"current_stage": "context_running"}
    body = render_landing(ctx, state, None)

    # Widget heading
    assert "Live funnel" in body or "live funnel" in body.lower()
    # Both numbers surface somewhere in the rendered HTML
    assert "3" in body  # ingested
    assert "2" in body  # in_scope
