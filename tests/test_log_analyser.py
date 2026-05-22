"""Tests for the log analyser sub-package.

LLM calls are mocked via the ``chat_fn`` injection point on
``analyse_case()``; no live mlx-broker needed in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

from dsar_orchestrator.log_analyser import analyse_case
from dsar_orchestrator.log_analyser.client import ChatResponse
from dsar_orchestrator.log_analyser.collectors import (
    basic_stats,
    collect_case_logs,
    summarise_for_prompt,
)
from dsar_orchestrator.log_analyser.core import (
    BLOCK_FLAG_FILENAME,
    clear_block,
    is_blocked,
)
from dsar_orchestrator.log_analyser.schemas import (
    AnalysisFinding,
    AnalysisReport,
    Severity,
)

# ─── helpers ────────────────────────────────────────────────────────


def _make_mock_chat(json_response: str | dict, *, model_alias: str = "tools"):
    """Build a chat_fn that returns the canned JSON response."""
    if isinstance(json_response, dict):
        json_response = json.dumps(json_response)

    def _mock_chat(**kwargs):
        return ChatResponse(
            text=json_response,
            model_alias=model_alias,
            resolved_model=f"mock-{model_alias}",
        )

    return _mock_chat


def _seed_audit_logs(audit_root: Path, case_no: str) -> None:
    """Drop a minimal pipeline.jsonl so the analyser has something to
    chew on."""
    case_dir = audit_root / case_no
    case_dir.mkdir(parents=True, exist_ok=True)
    pipeline_rows = [
        {"event": "stage_start", "stage": "ingest", "ts": "2026-05-22T10:00:00+00:00"},
        {
            "event": "stage_end",
            "stage": "ingest",
            "ts": "2026-05-22T10:00:05+00:00",
            "duration_s": 5.0,
            "outcome": "ok",
        },
        {
            "event": "run_complete",
            "stages_run": ["ingest"],
            "duration_s": 5.0,
            "halted": False,
        },
    ]
    (case_dir / "pipeline.jsonl").write_text("\n".join(json.dumps(r) for r in pipeline_rows) + "\n")


# ─── schemas ────────────────────────────────────────────────────────


def test_severity_numeric_ordering() -> None:
    assert Severity.INFO.numeric < Severity.WARNING.numeric
    assert Severity.WARNING.numeric < Severity.CRITICAL.numeric


def test_finding_round_trip() -> None:
    f = AnalysisFinding(
        severity=Severity.WARNING,
        category="stage_duration_outlier",
        message="ingest took 600s vs median 30s",
        evidence=["case=X, stage=ingest, duration_s=600"],
        recommendation="profile the source/ extractor",
    )
    f2 = AnalysisFinding.from_dict(f.as_dict())
    assert f2.severity == f.severity
    assert f2.category == f.category
    assert f2.evidence == f.evidence


def test_report_has_blocking_issues_only_when_critical() -> None:
    r = AnalysisReport(case_no="X")
    assert r.has_blocking_issues is False
    r.findings.append(AnalysisFinding(Severity.WARNING, "x", "x"))
    assert r.has_blocking_issues is False
    r.findings.append(AnalysisFinding(Severity.CRITICAL, "y", "y"))
    assert r.has_blocking_issues is True


def test_report_markdown_renders_sections() -> None:
    r = AnalysisReport(
        case_no="300100",
        model_alias="tools",
        resolved_model="hermes-4-70b",
        summary="One critical issue.",
        findings=[
            AnalysisFinding(
                Severity.CRITICAL,
                "verify_failure",
                "Phase 6 caught unredacted text",
                evidence=["ref-0042"],
                recommendation="re-run redact",
            )
        ],
    )
    md = r.render_markdown()
    assert "300100" in md
    assert "Critical" in md
    assert "verify_failure" in md
    assert "ref-0042" in md


# ─── collectors ─────────────────────────────────────────────────────


def test_collect_case_logs_returns_all_known(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    logs = collect_case_logs("300100", audit_root=tmp_path)
    assert "pipeline.jsonl" in logs
    assert "scope_rerank.jsonl" in logs  # absent → empty list, not missing
    assert logs["pipeline.jsonl"]
    assert logs["scope_rerank.jsonl"] == []


def test_basic_stats_derives_durations(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    logs = collect_case_logs("300100", audit_root=tmp_path)
    stats = basic_stats(logs)
    assert stats["pipeline_stages_run"] == ["ingest"]
    assert stats["pipeline_durations_s"]["ingest"] == 5.0
    assert stats["pipeline_stages_failed"] == []


def test_summarise_for_prompt_includes_all_known_logs(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    logs = collect_case_logs("300100", audit_root=tmp_path)
    text = summarise_for_prompt("300100", logs)
    assert "# Audit logs for case 300100" in text
    assert "pipeline.jsonl" in text
    assert "scope_rerank.jsonl" in text  # called out as empty


# ─── core: analyse_case with mocked LLM ─────────────────────────────


def test_analyse_case_parses_clean_json(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    mock = _make_mock_chat(
        {
            "summary": "Clean run.",
            "findings": [
                {
                    "severity": "info",
                    "category": "completeness",
                    "message": "Pipeline ran to completion.",
                    "evidence": ["run_complete halted=false"],
                    "recommendation": "",
                }
            ],
        }
    )
    report = analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    assert len(report.findings) == 1
    assert report.findings[0].severity == Severity.INFO
    assert not report.has_blocking_issues


def test_analyse_case_handles_code_fenced_response(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    fenced = (
        "Here is the analysis:\n```json\n"
        + json.dumps({"summary": "ok", "findings": []})
        + "\n```\n"
    )
    mock = _make_mock_chat(fenced)
    report = analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    assert report.summary == "ok"
    assert report.findings == []


def test_analyse_case_critical_writes_block_flag(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    mock = _make_mock_chat(
        {
            "summary": "Has critical issues.",
            "findings": [
                {
                    "severity": "critical",
                    "category": "verify_failure",
                    "message": "Unredacted PII leaked",
                    "evidence": ["redact_verify.jsonl ref=0042"],
                    "recommendation": "rerun redact stage",
                }
            ],
        }
    )
    report = analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    assert report.has_blocking_issues
    flag = tmp_path / "300100" / BLOCK_FLAG_FILENAME
    assert flag.exists()
    assert "BLOCK" in flag.read_text()


def test_analyse_case_clean_removes_existing_block_flag(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    # Pre-create a stale block flag
    case_dir = tmp_path / "300100"
    flag = case_dir / BLOCK_FLAG_FILENAME
    flag.write_text("stale block from prior run")
    mock = _make_mock_chat({"summary": "Clean.", "findings": []})
    analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    assert not flag.exists()


def test_analyse_case_writes_analysis_md(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    mock = _make_mock_chat({"summary": "All good.", "findings": []})
    analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    md = tmp_path / "300100" / "analysis.md"
    assert md.exists()
    assert "300100" in md.read_text()


def test_analyse_case_writes_analysis_jsonl_with_header(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    mock = _make_mock_chat(
        {
            "summary": "ok",
            "findings": [
                {"severity": "warning", "category": "x", "message": "y"},
            ],
        }
    )
    analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    jsonl = tmp_path / "300100" / "analysis.jsonl"
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line]
    assert rows[0]["row_type"] == "header"
    assert rows[0]["schema_version"] == "1.0"
    assert rows[1]["row_type"] == "finding"


def test_analyse_case_handles_non_json_response(tmp_path: Path) -> None:
    """If the LLM produces non-JSON, the analyser falls back to a
    warning-level self-error finding rather than crashing."""
    _seed_audit_logs(tmp_path, "300100")
    mock = _make_mock_chat("not actually json")
    report = analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    assert len(report.findings) == 1
    assert report.findings[0].category == "analyser_self_error"
    assert report.findings[0].severity == Severity.WARNING


def test_no_write_skips_persistence(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    mock = _make_mock_chat({"summary": "ok", "findings": []})
    analyse_case("300100", audit_root=tmp_path, chat_fn=mock, write_outputs=False)
    assert not (tmp_path / "300100" / "analysis.jsonl").exists()
    assert not (tmp_path / "300100" / "analysis.md").exists()


# ─── block flag operations ─────────────────────────────────────────


def test_is_blocked_false_when_no_flag(tmp_path: Path) -> None:
    case_dir = tmp_path / "300100"
    case_dir.mkdir()
    assert is_blocked("300100", audit_root=tmp_path) is False


def test_is_blocked_true_after_critical_finding(tmp_path: Path) -> None:
    _seed_audit_logs(tmp_path, "300100")
    mock = _make_mock_chat(
        {
            "summary": "x",
            "findings": [{"severity": "critical", "category": "x", "message": "x"}],
        }
    )
    analyse_case("300100", audit_root=tmp_path, chat_fn=mock)
    assert is_blocked("300100", audit_root=tmp_path) is True


def test_clear_block_removes_flag(tmp_path: Path) -> None:
    case_dir = tmp_path / "300100"
    case_dir.mkdir()
    (case_dir / BLOCK_FLAG_FILENAME).write_text("blocked")
    clear_block("300100", audit_root=tmp_path)
    assert is_blocked("300100", audit_root=tmp_path) is False


def test_clear_block_is_idempotent(tmp_path: Path) -> None:
    case_dir = tmp_path / "300100"
    case_dir.mkdir()
    # No flag → no error
    clear_block("300100", audit_root=tmp_path)
    assert is_blocked("300100", audit_root=tmp_path) is False
