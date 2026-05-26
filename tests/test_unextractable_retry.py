"""Tests for unextractable.retry_extract — covers the regression where
record_decision started requiring reason_code (post #105) but
retry_extract wasn't updated to pass it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


@pytest.fixture
def case_root(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    (tmp_path / "audit").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps({"case_no": "TEST-100"})
    )
    return tmp_path


def test_retry_extract_success_records_decision_with_reason_code(
    case_root: Path, monkeypatch, tmp_path: Path
) -> None:
    """retry_extract must pass reason_code to record_decision; otherwise it
    crashes with a TypeError post-#105."""
    from dsar_orchestrator.local_broker import unextractable

    src = tmp_path / "doc.eml"
    src.write_text("body")

    class _FakeProvenance:
        sha256 = "deadbeef"
        extension = ".eml"
        size_bytes = 4
        from datetime import UTC, datetime
        ingested_at = datetime.now(UTC)

    class _FakeMetadata:
        extras: dict = {}

    class _FakeDoc:
        provenance = _FakeProvenance()
        metadata = _FakeMetadata()
        def char_count(self): return 100
        def yield_ratio(self): return 1.0

    class _FakeCtxSrc:
        source_kind = "file"
        mailbox_owner_email = None
        mailbox_owner_display = None
        mailbox_owner_slug = None

    def fake_ingest(p):
        return _FakeDoc()

    def fake_parse_source_context(p):
        return _FakeCtxSrc()

    # Monkeypatch the lazy imports inside retry_extract
    from dsar_pipeline import ingest_v3
    monkeypatch.setattr(ingest_v3, "ingest", fake_ingest)
    from dsar_pipeline.ingest_v3 import source_context
    monkeypatch.setattr(source_context, "parse_source_context", fake_parse_source_context)

    shim = unextractable._CaseShim(case_dir=case_root)
    result = unextractable.retry_extract(
        shim, source_path=str(src), case_id="TEST-100"
    )
    assert result["ok"] is True
    # Decision row carries the reason_code
    decisions_path = case_root / "audit" / "unextractable_decisions.jsonl"
    rows = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()]
    assert any(r.get("reason_code") == "R009" for r in rows)
    # Ingested-item row appended
    ing_path = case_root / "working" / "ingested_items.jsonl"
    items = [json.loads(line) for line in ing_path.read_text().splitlines() if line.strip()]
    assert len(items) == 1
    assert items[0]["case_id"] == "TEST-100"


def test_retry_extract_failure_records_decision_with_reason_code(
    case_root: Path, monkeypatch, tmp_path: Path
) -> None:
    """When ingest_v3.ingest raises, the retried_fail decision must also
    carry reason_code so record_decision doesn't crash."""
    from dsar_orchestrator.local_broker import unextractable

    src = tmp_path / "broken.xlsx"
    src.write_text("not really xlsx")

    def fake_ingest(p):
        raise RuntimeError("openpyxl bombed")

    from dsar_pipeline import ingest_v3
    monkeypatch.setattr(ingest_v3, "ingest", fake_ingest)

    shim = unextractable._CaseShim(case_dir=case_root)
    result = unextractable.retry_extract(
        shim, source_path=str(src), case_id="TEST-100"
    )
    assert result["ok"] is False
    decisions_path = case_root / "audit" / "unextractable_decisions.jsonl"
    rows = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()]
    assert any(
        r.get("decision") == "retried_fail" and r.get("reason_code") == "R009"
        for r in rows
    )


def test_retry_extract_missing_source_records_decision_with_reason_code(
    case_root: Path,
) -> None:
    """Missing source file → retried_fail with reason_code; no crash."""
    from dsar_orchestrator.local_broker import unextractable

    shim = unextractable._CaseShim(case_dir=case_root)
    result = unextractable.retry_extract(
        shim, source_path="/nonexistent/path.eml", case_id="TEST-100"
    )
    assert result["ok"] is False
    decisions_path = case_root / "audit" / "unextractable_decisions.jsonl"
    rows = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()]
    assert any(
        r.get("decision") == "retried_fail" and r.get("reason_code") == "R009"
        for r in rows
    )
