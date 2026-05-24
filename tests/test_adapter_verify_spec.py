"""Tests for the verify_spec adapter — `adapters.verify_spec`.

Adapter calls ``dsar_pipeline.verify_spec.verify_for_conductor`` and inspects the
Verdict object. Verifier is injected so tests are hermetic.

verify_spec is always-on (no enable flag like verify_pdf) — the operator's
only way to skip it is `--from bake` or later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsar_orchestrator.adapters import verify_spec as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import PipelineHalt


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
    )


class _PassingVerdict:
    all_passed = True
    failed_doc_count = 0
    failed_verifier_summary = ""
    audit_log_path = Path("/tmp/verify_spec_findings.jsonl")


class _FailingVerdict:
    all_passed = False
    failed_doc_count = 2
    failed_verifier_summary = "C1 unmatched_high_evidence: 2 refs"
    audit_log_path = Path("/tmp/verify_spec_findings.jsonl")


# ─── happy path ────────────────────────────────────────────────────


def test_completes_silently_when_verdict_passes(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()
    adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: _PassingVerdict())


def test_verifier_receives_case_path(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()
    captured = {}

    def capturing(cp):
        captured["case_path"] = cp
        return _PassingVerdict()

    adapter.run_for_case(_make_cfg(case_path), verify_fn=capturing)
    assert captured["case_path"] == case_path


# ─── failure path ──────────────────────────────────────────────────


def test_halts_pipeline_when_verdict_fails(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()
    with pytest.raises(PipelineHalt, match="2 doc"):
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: _FailingVerdict())


def test_halt_message_includes_audit_log_path(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()
    failing_audit = case_path / "working" / "verify_spec_findings.jsonl"

    class _FailingV:
        all_passed = False
        failed_doc_count = 2
        failed_verifier_summary = "C1 unmatched_high_evidence: 2 refs"
        audit_log_path = failing_audit

    with pytest.raises(PipelineHalt) as ei:
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: _FailingV())
    msg = str(ei.value)
    assert str(failing_audit) in msg
    assert case_path.name in msg


def test_halt_message_includes_resume_hint(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()
    with pytest.raises(PipelineHalt, match="dsar-conductor.*--from redact"):
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: _FailingVerdict())


def test_handles_verdict_missing_optional_fields(tmp_path: Path) -> None:
    """Verdict objects may not always expose failed_doc_count /
    failed_verifier_summary — adapter must tolerate that. audit_log_path
    is mandatory (per v5.0 Phase 1b lesson — no getattr fallback)."""
    case_path = tmp_path / "700100"
    case_path.mkdir()

    class MinimalFailingVerdict:
        all_passed = False
        audit_log_path = case_path / "working" / "verify_spec_findings.jsonl"

    with pytest.raises(PipelineHalt, match="spec-verify failed"):
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: MinimalFailingVerdict())
