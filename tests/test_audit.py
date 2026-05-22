"""Tests for the PipelineAuditor + StageBanner audit emission."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from dsar_orchestrator.audit import PipelineAuditor, StageBanner


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_auditor_creates_audit_dir_mode_0700(audit_root: Path) -> None:
    PipelineAuditor("300001", audit_root=audit_root)
    case_dir = audit_root / "300001"
    assert case_dir.exists()
    assert case_dir.is_dir()
    # Mode check — must be 0700 per the audit privacy posture
    assert (case_dir.stat().st_mode & 0o777) == 0o700


def test_write_appends_jsonl_with_version_fields(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    auditor.write({"event": "stage_start", "stage": "ingest"})
    rows = _read_jsonl(auditor.log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "stage_start"
    assert row["stage"] == "ingest"
    assert row["case"] == "300001"
    assert row["schema_version"] == "1.0"
    assert row["producer_version"].startswith("dsar_orchestrator ")


def test_write_is_append_only(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    auditor.write({"event": "note", "kind": "test", "message": "row 1"})
    auditor.write({"event": "note", "kind": "test", "message": "row 2"})
    auditor.write({"event": "note", "kind": "test", "message": "row 3"})
    rows = _read_jsonl(auditor.log_path)
    assert [r["message"] for r in rows] == ["row 1", "row 2", "row 3"]


def test_note_writes_with_extra_fields(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    auditor.note("test_kind", "hello", extra_field="extra_value")
    row = _read_jsonl(auditor.log_path)[0]
    assert row["kind"] == "test_kind"
    assert row["message"] == "hello"
    assert row["extra_field"] == "extra_value"


def test_mark_skipped_records_reason(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    auditor.mark_skipped("pii_classify", "PII_CLASSIFY_MODE=off")
    assert "pii_classify" in auditor.stages_skipped
    row = _read_jsonl(auditor.log_path)[0]
    assert row["event"] == "stage_skipped"
    assert row["stage"] == "pii_classify"
    assert row["reason"] == "PII_CLASSIFY_MODE=off"


def test_stage_banner_emits_start_and_end_on_success(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    buf = io.StringIO()
    with StageBanner(auditor, "ingest", stream=buf):
        pass
    rows = _read_jsonl(auditor.log_path)
    assert [r["event"] for r in rows] == ["stage_start", "stage_end"]
    assert rows[1]["outcome"] == "ok"
    assert "duration_s" in rows[1]
    assert "start" in buf.getvalue()
    assert "done in" in buf.getvalue()


def test_stage_banner_emits_failed_and_reraises(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    buf = io.StringIO()
    with pytest.raises(RuntimeError, match="boom"):  # noqa: SIM117
        with StageBanner(auditor, "redact", stream=buf):
            raise RuntimeError("boom")
    rows = _read_jsonl(auditor.log_path)
    assert [r["event"] for r in rows] == ["stage_start", "stage_end"]
    assert rows[1]["outcome"] == "failed"
    assert rows[1]["error_type"] == "RuntimeError"
    assert rows[1]["error_message"] == "boom"
    assert "FAILED" in buf.getvalue()


def test_stage_banner_appends_to_stages_run(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    buf = io.StringIO()
    with StageBanner(auditor, "ingest", stream=buf):
        pass
    with StageBanner(auditor, "redact", stream=buf):
        pass
    assert auditor.stages_run == ["ingest", "redact"]


def test_finalise_writes_run_complete_row(audit_root: Path) -> None:
    auditor = PipelineAuditor("300001", audit_root=audit_root)
    auditor.stages_run = ["ingest", "redact"]
    report = auditor.finalise()
    assert report.case_no == "300001"
    assert report.stages_run == ["ingest", "redact"]
    assert report.duration_s >= 0
    rows = _read_jsonl(auditor.log_path)
    last = rows[-1]
    assert last["event"] == "run_complete"
    assert last["stages_run"] == ["ingest", "redact"]
