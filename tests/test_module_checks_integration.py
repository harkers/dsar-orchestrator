"""Tests for pipeline._check_module_work — the integration that calls
the in-process agent and writes audit rows / halts on critical."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import PipelineHalt
from dsar_orchestrator.pipeline import _check_module_work


def _make_cfg(case_path: Path, case_no: str = "300500") -> CaseConfig:
    return CaseConfig(
        case_no=case_no,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
    )


def _read_module_checks(audit_root: Path, case_no: str) -> list[dict]:
    p = audit_root / case_no / "module_checks.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_check_work_writes_audit_row_on_ok(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    case_path = tmp_path / "300500"
    (case_path / "source").mkdir(parents=True)
    (case_path / "working").mkdir()
    (case_path / "redacted").mkdir()
    (case_path / "output").mkdir()
    # Seed a valid register so ingest agent reports ok
    (case_path / "source" / "doc.txt").write_text("x")
    (case_path / "working" / "register.json").write_text(
        json.dumps(
            {
                "case_no": "300500",
                "refs": [{"ref": "doc-0001", "text_path": "source/doc.txt"}],
                "upstream_hash": "h",
            }
        )
    )

    _check_module_work(_make_cfg(case_path), "ingest")

    rows = _read_module_checks(tmp_path / ".dsar-audit", "300500")
    assert len(rows) == 1
    assert rows[0]["sub_stage"] == "ingest"
    assert rows[0]["ok"] is True
    assert rows[0]["severity"] == "info"
    assert "schema_version" in rows[0]


def test_check_work_writes_row_then_raises_on_critical(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    case_path = tmp_path / "300500"
    case_path.mkdir()
    # No register.json → ingest agent returns critical
    with pytest.raises(PipelineHalt, match="critical issue"):
        _check_module_work(_make_cfg(case_path), "ingest")

    # Row was written BEFORE the halt
    rows = _read_module_checks(tmp_path / ".dsar-audit", "300500")
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert rows[0]["severity"] == "critical"


def test_check_work_warning_writes_row_but_does_not_halt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    case_path = tmp_path / "300500"
    (case_path / "source").mkdir(parents=True)
    (case_path / "working").mkdir()
    (case_path / "source" / "doc.txt").write_text("x")
    # register.json without upstream_hash → warning
    (case_path / "working" / "register.json").write_text(
        json.dumps(
            {
                "case_no": "300500",
                "refs": [{"ref": "doc-0001", "text_path": "source/doc.txt"}],
            }
        )
    )

    # Should NOT raise
    _check_module_work(_make_cfg(case_path), "ingest")
    rows = _read_module_checks(tmp_path / ".dsar-audit", "300500")
    assert rows[0]["severity"] == "warning"
    assert rows[0]["ok"] is False


def test_check_work_appends_across_multiple_calls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    case_path = tmp_path / "300500"
    case_path.mkdir()
    cfg = _make_cfg(case_path)

    # Each call records a row — ingest will be critical (no register),
    # but the audit log accrues regardless.
    import contextlib

    for sub in ("ingest", "embed", "detect_2_1_to_2_4"):
        with contextlib.suppress(PipelineHalt):
            _check_module_work(cfg, sub)

    rows = _read_module_checks(tmp_path / ".dsar-audit", "300500")
    sub_stages = [r["sub_stage"] for r in rows]
    assert sub_stages == ["ingest", "embed", "detect_2_1_to_2_4"]
