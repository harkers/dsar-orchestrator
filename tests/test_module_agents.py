"""Tests for the per-stage module-agent validation contract.

After every module step the orchestrator invokes
``dsar_pipeline.module_agents.<sub_stage>.check_work(case_path)`` to
validate the work. This test suite covers the four behaviours:

1. No agent shipped → record an "info" no_agent row, continue
2. Agent missing the contract (no check_work) → halt
3. Agent returns ok=True → record an "info" row, continue
4. Agent returns ok=False severity=critical → halt the pipeline
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import PipelineHalt
from dsar_orchestrator.pipeline import _check_module_work


def _make_cfg(case_no: str, audit_root: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_no,
        case_path=audit_root / case_no,
        case_scope="x",
        subject_identifier=SubjectIdentifier(primary_name="t"),
    )


def _install_agent(monkeypatch, sub_stage: str, check_fn) -> None:
    """Register a fake dsar_pipeline.module_agents.<sub_stage> module
    in sys.modules with the given check_work callable (or no callable
    if check_fn is None)."""
    # Ensure parent packages exist
    if "dsar_pipeline" not in sys.modules:
        pkg = types.ModuleType("dsar_pipeline")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "dsar_pipeline", pkg)
    if "dsar_pipeline.module_agents" not in sys.modules:
        pkg = types.ModuleType("dsar_pipeline.module_agents")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "dsar_pipeline.module_agents", pkg)
    mod = types.ModuleType(f"dsar_pipeline.module_agents.{sub_stage}")
    if check_fn is not None:
        mod.check_work = check_fn
    monkeypatch.setitem(sys.modules, f"dsar_pipeline.module_agents.{sub_stage}", mod)


def _read_module_checks(audit_root: Path, case_no: str) -> list[dict]:
    p = audit_root / case_no / "module_checks.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# ─── 1. No agent → record + continue ──────────────────────────────


def test_no_agent_records_info_row(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _make_cfg("300100", tmp_path / ".dsar-audit")
    # No agent installed for "embed" — should record + continue
    _check_module_work(cfg, "embed")

    rows = _read_module_checks(tmp_path / ".dsar-audit", "300100")
    assert len(rows) == 1
    assert rows[0]["sub_stage"] == "embed"
    assert rows[0]["outcome"] == "no_agent"
    assert rows[0]["severity"] == "info"
    assert "schema_version" in rows[0]


# ─── 2. Partial agent (no check_work) → halt ───────────────────────


def test_partial_agent_raises_pipeline_halt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _make_cfg("300100", tmp_path / ".dsar-audit")
    _install_agent(monkeypatch, "embed", check_fn=None)
    with pytest.raises(PipelineHalt, match=r"missing `check_work"):
        _check_module_work(cfg, "embed")


# ─── 3. Agent OK → record + continue ───────────────────────────────


def test_agent_ok_records_passing_row(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _make_cfg("300100", tmp_path / ".dsar-audit")

    class Result:
        ok = True
        severity = "info"
        findings: list[str] = []
        recommendation = ""

    def check_work(case_path):
        return Result()

    _install_agent(monkeypatch, "embed", check_work)
    _check_module_work(cfg, "embed")

    rows = _read_module_checks(tmp_path / ".dsar-audit", "300100")
    assert len(rows) == 1
    assert rows[0]["sub_stage"] == "embed"
    assert rows[0]["ok"] is True
    assert rows[0]["severity"] == "info"


# ─── 4. Agent critical → halt ──────────────────────────────────────


def test_agent_critical_halts_pipeline(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _make_cfg("300100", tmp_path / ".dsar-audit")

    class Result:
        ok = False
        severity = "critical"
        findings = ["entity offset mismatch on ref 0042"]
        recommendation = "rerun dsar-embed --case 300100 --if-exists overwrite"

    def check_work(case_path):
        return Result()

    _install_agent(monkeypatch, "embed", check_work)
    with pytest.raises(PipelineHalt, match="critical issue"):
        _check_module_work(cfg, "embed")

    # The audit row gets written BEFORE the halt is raised
    rows = _read_module_checks(tmp_path / ".dsar-audit", "300100")
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert rows[0]["severity"] == "critical"
    assert rows[0]["findings"] == ["entity offset mismatch on ref 0042"]


# ─── 5. Agent warning → continue (does not halt) ───────────────────


def test_agent_warning_does_not_halt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _make_cfg("300100", tmp_path / ".dsar-audit")

    class Result:
        ok = False
        severity = "warning"
        findings = ["dim is 1024 but model_revision is unknown"]
        recommendation = "verify TEI model pin"

    def check_work(case_path):
        return Result()

    _install_agent(monkeypatch, "embed", check_work)
    # Should NOT raise
    _check_module_work(cfg, "embed")

    rows = _read_module_checks(tmp_path / ".dsar-audit", "300100")
    assert rows[0]["severity"] == "warning"
    assert rows[0]["ok"] is False


# ─── 6. Audit log is append-only across multiple checks ───────────


def test_module_checks_log_is_append_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _make_cfg("300100", tmp_path / ".dsar-audit")
    _check_module_work(cfg, "ingest")
    _check_module_work(cfg, "embed")
    _check_module_work(cfg, "detect_2_1_to_2_4")

    rows = _read_module_checks(tmp_path / ".dsar-audit", "300100")
    assert [r["sub_stage"] for r in rows] == ["ingest", "embed", "detect_2_1_to_2_4"]
