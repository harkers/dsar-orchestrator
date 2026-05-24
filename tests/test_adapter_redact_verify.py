"""Tests for the redact_verify adapter — `adapters.redact_verify`.

Adapter calls ``dsar_redact_verify.core.verify_case`` and inspects the
Verdict object. Verifier is injected so tests are hermetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsar_orchestrator.adapters import redact_verify as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import PipelineHalt


def _make_cfg(case_path: Path, *, enabled: bool = True) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
        redact_verify_enabled=enabled,
    )


class _PassingVerdict:
    all_passed = True
    failed_doc_count = 0
    failed_verifier_summary = ""
    audit_log_path = Path("/tmp/post_bake_findings.jsonl")


class _FailingVerdict:
    all_passed = False
    failed_doc_count = 3
    failed_verifier_summary = "presidio_residual: 3 docs"
    audit_log_path = Path("/tmp/post_bake_findings.jsonl")


# ─── happy path ────────────────────────────────────────────────────


def test_no_op_when_disabled(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()

    def panic(_):
        raise RuntimeError("verifier should not have been called")

    adapter.run_for_case(_make_cfg(case_path, enabled=False), verify_fn=panic)


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
    with pytest.raises(PipelineHalt, match="3 doc"):
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: _FailingVerdict())


def test_halt_message_includes_audit_log_path(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()
    failing_audit = case_path / "working" / "post_bake_findings.jsonl"

    class _FailingV:
        all_passed = False
        failed_doc_count = 3
        failed_verifier_summary = "presidio_residual: 3 docs"
        audit_log_path = failing_audit

    with pytest.raises(PipelineHalt) as ei:
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: _FailingV())
    msg = str(ei.value)
    assert str(failing_audit) in msg
    assert case_path.name in msg


def test_halt_message_includes_resume_hint(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    case_path.mkdir()
    with pytest.raises(PipelineHalt, match="dsar-conductor"):
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: _FailingVerdict())


def test_handles_verdict_missing_optional_fields(tmp_path: Path) -> None:
    """Verdict objects may not always expose failed_doc_count /
    failed_verifier_summary — adapter must tolerate that."""
    case_path = tmp_path / "700100"
    case_path.mkdir()

    class MinimalFailingVerdict:
        all_passed = False

    with pytest.raises(PipelineHalt, match="redact-verify failed"):
        adapter.run_for_case(_make_cfg(case_path), verify_fn=lambda _: MinimalFailingVerdict())
