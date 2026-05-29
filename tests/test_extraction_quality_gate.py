"""Tests for the spec §2.4 extraction-quality soft/hard gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dsar_orchestrator.exceptions import (
    EmptyIngestError,
    ExtractionQualityCatastrophicError,
)
from dsar_orchestrator.pipeline import _check_extraction_quality


@dataclass
class _StubAuditor:
    notes: list[tuple[str, str]] = field(default_factory=list)
    stages_run: list[str] = field(default_factory=list)
    case_no: str = "case-001"

    def note(self, stage, message):
        self.notes.append((stage, message))

    def write(self, *a, **kw):
        pass


def _make_cfg(case_path: Path):
    from dsar_orchestrator.config import CaseConfig

    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        fitness_check_enabled=False,
    )


def _seed_register(case_path: Path, refs: list[dict]) -> None:
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    (working / "register.json").write_text(json.dumps(refs))


def test_zero_refs_raises_empty_ingest(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "register.json").write_text("[]")
    cfg = _make_cfg(tmp_path)
    with pytest.raises(EmptyIngestError, match="0 refs"):
        _check_extraction_quality(cfg, _StubAuditor())


def test_missing_register_raises_empty_ingest(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    with pytest.raises(EmptyIngestError):
        _check_extraction_quality(cfg, _StubAuditor())


def test_low_ocr_failure_passes_silently(tmp_path: Path) -> None:
    """0% OCR failure — clean pass with an OK note."""
    refs = [{"ref": f"r-{i}", "text_quality": "high"} for i in range(10)]
    _seed_register(tmp_path, refs)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _check_extraction_quality(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)


def test_5_percent_ocr_failure_passes(tmp_path: Path) -> None:
    """5% (1/20) < 10% threshold — pass."""
    refs = [{"ref": f"r-{i}", "text_quality": "high"} for i in range(19)] + [
        {"ref": "r-19", "text_quality": "ocr_failure"}
    ]
    _seed_register(tmp_path, refs)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _check_extraction_quality(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)


def test_15_percent_ocr_failure_emits_warning_and_passes(tmp_path: Path) -> None:
    """15% (3/20) > 10% threshold — soft warning, pipeline continues."""
    refs = [{"ref": f"r-{i}", "text_quality": "high"} for i in range(17)] + [
        {"ref": f"r-{i}", "text_quality": "ocr_failure"} for i in range(17, 20)
    ]
    _seed_register(tmp_path, refs)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _check_extraction_quality(cfg, auditor)  # must not raise
    assert any("WARN" in m for _, m in auditor.notes)
    # Audit event MUST be written (spec §2.4 EXTRACTION_QUALITY_GATE_WARNING
    # is mandatory on the soft-gate path — unconditional assertion).
    audit_path = tmp_path / "working" / "audit_events.jsonl"
    assert audit_path.exists(), "audit_events.jsonl was not created"
    events = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    warn_events = [
        e
        for e in events
        if e.get("event_type") == "extraction_quality_gate_warning" or "ocr_failure_rate" in e
    ]
    assert warn_events, f"no EXTRACTION_QUALITY_GATE_WARNING event in {events}"
    evt = warn_events[0]
    assert evt["ocr_failure_rate"] == pytest.approx(0.15)
    assert evt["refs_total"] == 20
    assert evt["soft_gate_threshold"] == 0.10
    assert evt["hard_halt_threshold"] == 0.50


def test_60_percent_ocr_failure_raises_catastrophic(tmp_path: Path) -> None:
    """60% > 50% threshold — hard halt."""
    refs = [{"ref": f"r-{i}", "text_quality": "high"} for i in range(8)] + [
        {"ref": f"r-{i}", "text_quality": "ocr_failure"} for i in range(8, 20)
    ]
    _seed_register(tmp_path, refs)
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ExtractionQualityCatastrophicError, match="60.0%"):
        _check_extraction_quality(cfg, _StubAuditor())


def test_boundary_50_percent_passes(tmp_path: Path) -> None:
    """Exactly 50% — does NOT trigger hard halt (spec says > 50%, not >=).
    DOES trigger soft warning since > 10%."""
    refs = [{"ref": f"r-{i}", "text_quality": "high"} for i in range(10)] + [
        {"ref": f"r-{i}", "text_quality": "ocr_failure"} for i in range(10, 20)
    ]
    _seed_register(tmp_path, refs)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _check_extraction_quality(cfg, auditor)  # must not raise


def test_boundary_10_percent_does_not_trigger_warning(tmp_path: Path) -> None:
    """Exactly 10% — spec says > 10%, not >=. No warning."""
    refs = [{"ref": f"r-{i}", "text_quality": "high"} for i in range(9)] + [
        {"ref": "r-9", "text_quality": "ocr_failure"}
    ]
    _seed_register(tmp_path, refs)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _check_extraction_quality(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)
    assert not any("WARN" in m for _, m in auditor.notes)


def test_unknown_text_quality_does_not_count_as_failure(tmp_path: Path) -> None:
    """text_quality='unknown' is NOT ocr_failure — should NOT count toward the rate."""
    refs = [{"ref": f"r-{i}", "text_quality": "unknown"} for i in range(15)] + [
        {"ref": f"r-{i}", "text_quality": "high"} for i in range(15, 20)
    ]
    _seed_register(tmp_path, refs)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _check_extraction_quality(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)
