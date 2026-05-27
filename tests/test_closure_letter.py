"""#118 — closure-letter auto-regeneration on metric change.

``draft_letter`` now sources its canonical six funnel counts from
``metrics.recompute_funnel`` instead of duplicating the live-recompute
locally. A new "Live funnel snapshot" mini-table renders the six
metrics-canonical counts at the top of the letter, and a "Corpus scale
processed" section reads ``audit/corpus_metrics.json`` via
``metrics.read_metrics_snapshot`` (use whatever's there; do NOT recompute
on every render).
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
        json.dumps(
            {
                "case_id": "CASE01",
                "full_name": "Jane Test",
                "email": "jane@example.com",
            }
        )
    )
    (case / "working" / "case_context.json").write_text(
        json.dumps({"controller": "Acme Inc", "request_date": "2026-04-01"})
    )
    return case


def _write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _ctx(case_dir: Path):
    from dsar_orchestrator.local_broker.closure_letter import _CaseShim

    return _CaseShim(case_dir=case_dir)


# --------------------------------------------------------------------------
# Funnel — letter sources from metrics.recompute_funnel
# --------------------------------------------------------------------------


def test_draft_letter_renders_live_funnel_snapshot_table(case_dir: Path) -> None:
    """A 'Live funnel snapshot' table renders the six canonical counts
    from metrics.recompute_funnel."""
    from dsar_orchestrator.local_broker.closure_letter import draft_letter

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
    _write_jsonl(
        case_dir / "working" / "redaction_decisions.jsonl",
        [
            {"doc_ref": "a", "status": "redacted"},
            {"doc_ref": "b", "status": "redacted"},
        ],
    )

    body = draft_letter(_ctx(case_dir))

    assert "Live funnel snapshot" in body
    # Six canonical labels in the snapshot table
    for label in ("Ingested", "In scope", "Redacted", "Leak-excluded", "QA decided", "Final"):
        assert label in body, f"missing snapshot label {label!r}"


def test_draft_letter_snapshot_reflects_added_decisions(case_dir: Path) -> None:
    """Re-rendering the letter after a new decision lands picks up the
    new numbers — the funnel is live, not baked in."""
    from dsar_orchestrator.local_broker.closure_letter import draft_letter

    _write_jsonl(
        case_dir / "working" / "redaction_decisions.jsonl",
        [{"doc_ref": "a", "status": "redacted"}],
    )
    body1 = draft_letter(_ctx(case_dir))
    assert "**1**" in body1 or "| 1 |" in body1 or " 1 " in body1

    _write_jsonl(
        case_dir / "working" / "redaction_decisions.jsonl",
        [
            {"doc_ref": "a", "status": "redacted"},
            {"doc_ref": "b", "status": "redacted"},
            {"doc_ref": "c", "status": "redacted"},
        ],
    )
    body2 = draft_letter(_ctx(case_dir))
    # The snapshot table shows 'Redacted | 3' on its own row.
    assert "| Redacted | **3** |" in body2


def test_draft_letter_calls_recompute_funnel(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """draft_letter routes through metrics.recompute_funnel so the
    persisted audit/corpus_metrics.json is refreshed alongside the
    letter render."""
    from dsar_orchestrator.local_broker import closure_letter, metrics

    calls = []
    real = metrics.recompute_funnel

    def spy(cd: Path) -> dict:
        calls.append(cd)
        return real(cd)

    monkeypatch.setattr(metrics, "recompute_funnel", spy)
    # closure_letter imports recompute_funnel by name inside draft_letter
    # at call time; patch at module level so any late import picks it up.
    monkeypatch.setattr(
        "dsar_orchestrator.local_broker.metrics.recompute_funnel",
        spy,
        raising=True,
    )

    closure_letter.draft_letter(_ctx(case_dir))
    assert calls, "draft_letter should call metrics.recompute_funnel"
    assert calls[0] == case_dir


# --------------------------------------------------------------------------
# Corpus scale section — from audit/corpus_metrics.json snapshot only
# --------------------------------------------------------------------------


def test_draft_letter_renders_corpus_scale_when_snapshot_present(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.closure_letter import draft_letter

    # Pre-seed the snapshot — recompute_funnel will update funnel but
    # leave scale alone (verified in #117 tests).
    (case_dir / "audit" / "corpus_metrics.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "funnel": {"ingested": 0},
                "scale": {
                    "word_count": 124000,
                    "doc_count": 412,
                    "computed_at": "2026-05-01T00:00:00Z",
                },
                "computed_at": "2026-05-01T00:00:00Z",
            }
        )
    )
    body = draft_letter(_ctx(case_dir))
    assert "Corpus scale processed" in body
    assert "124,000" in body or "124000" in body
    assert "412" in body


def test_draft_letter_omits_corpus_scale_when_snapshot_missing(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.closure_letter import draft_letter

    # No audit/corpus_metrics.json exists yet beyond what recompute_funnel
    # writes during draft_letter, and recompute_funnel never populates
    # 'scale'. Section should be omitted.
    body = draft_letter(_ctx(case_dir))
    assert "Corpus scale processed" not in body


def test_draft_letter_omits_corpus_scale_when_snapshot_has_no_scale(case_dir: Path) -> None:
    """Snapshot exists with funnel only — scale section must still be
    omitted gracefully."""
    from dsar_orchestrator.local_broker.closure_letter import draft_letter

    (case_dir / "audit" / "corpus_metrics.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "funnel": {"ingested": 5},
                "computed_at": "2026-05-01T00:00:00Z",
            }
        )
    )
    body = draft_letter(_ctx(case_dir))
    assert "Corpus scale processed" not in body
