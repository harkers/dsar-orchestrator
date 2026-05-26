"""Tests for the orchestrator's _run_fitness_preflight (spec §4.4 F)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _seed_canary(corpus_dir: Path) -> None:
    (corpus_dir / "refs").mkdir(parents=True)
    (corpus_dir / "canary_corpus.json").write_text('{"version":1,"refs":["r1"]}', encoding="utf-8")
    (corpus_dir / "truth.json").write_text('{"r1":"biographical"}', encoding="utf-8")
    (corpus_dir / "refs" / "r1.txt").write_text("body\n", encoding="utf-8")


def _seed_case(case_dir: Path, **cfg_overrides) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "case_no": "TEST",
        "case_scope": "x",
        "fitness_check_enabled": True,
        "fitness_check_deployment_id": "test_deploy",
        "fitness_check_max_report_age_days": 30,
    }
    base.update(cfg_overrides)
    (case_dir / "case_config.json").write_text(json.dumps(base), encoding="utf-8")


def _current_prompt_tuple() -> tuple[str, str | None]:
    """Compute the canonical seals for the *installed* durant prompts.
    Tests use these so the preflight's PromptLoader.load() finds matching
    reports."""
    from dsar_pipeline.gates.prompt_loader import PromptLoader

    primary = PromptLoader.load("durant.system").canonical_seal_sha256
    try:
        recheck = PromptLoader.load("durant.recheck.system").canonical_seal_sha256
    except Exception:
        recheck = None
    return primary, recheck


def _current_inference_params_sha(cfg) -> str:
    from dsar_orchestrator.pipeline import _compute_inference_params_sha256

    return _compute_inference_params_sha256(cfg)


def _write_report(
    report_root: Path,
    deployment_id: str,
    *,
    passed: bool,
    age_days: float = 0.0,
    corpus_sha: str | None = None,
    primary_seal: str | None = None,
    recheck_seal: str | None = None,
    model_alias: str = "claude-opus-4-7@anthropic",
    inference_params_sha: str | None = None,
) -> Path:
    deploy = report_root / deployment_id
    deploy.mkdir(parents=True, exist_ok=True)
    gen_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    safe_name = gen_at.replace(":", "_")
    body = {
        "report_id": "abc",
        "generated_at": gen_at,
        "deployment_id": deployment_id,
        "model_alias": model_alias,
        "primary_prompt_seal_sha256": primary_seal,
        "recheck_prompt_seal_sha256": recheck_seal,
        "inference_params_sha256": inference_params_sha,
        "passed": passed,
        "fails": (
            []
            if passed
            else [
                {
                    "code": "fn_wilson_upper_above_threshold",
                    "kind": "model",
                    "detail": "test",
                }
            ]
        ),
        "live_corpus_sha256": corpus_sha,
    }
    path = deploy / f"{safe_name}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_preflight_halts_when_no_report(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    monkeypatch.setenv("DSAR_CANARY_PATH_OVERRIDE", str(canary))

    from dsar_orchestrator.audit import PipelineAuditor
    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="no fitness report"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_halts_when_report_stale(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case, fitness_check_max_report_age_days=7)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    from dsar_pipeline.canary_corpus import compute_corpus_sha256

    live_sha = compute_corpus_sha256(canary)
    primary, recheck = _current_prompt_tuple()
    report_root = tmp_path / "reports"
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    monkeypatch.setenv("DSAR_CANARY_PATH_OVERRIDE", str(canary))

    from dsar_orchestrator.audit import PipelineAuditor
    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    inf_sha = _current_inference_params_sha(cfg)
    _write_report(
        report_root,
        "test_deploy",
        passed=True,
        age_days=14.0,
        corpus_sha=live_sha,
        primary_seal=primary,
        recheck_seal=recheck,
        inference_params_sha=inf_sha,
    )
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="stale"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_halts_when_report_failing(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    from dsar_pipeline.canary_corpus import compute_corpus_sha256

    live_sha = compute_corpus_sha256(canary)
    primary, recheck = _current_prompt_tuple()
    report_root = tmp_path / "reports"
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    monkeypatch.setenv("DSAR_CANARY_PATH_OVERRIDE", str(canary))

    from dsar_orchestrator.audit import PipelineAuditor
    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    inf_sha = _current_inference_params_sha(cfg)
    _write_report(
        report_root,
        "test_deploy",
        passed=False,
        age_days=1.0,
        corpus_sha=live_sha,
        primary_seal=primary,
        recheck_seal=recheck,
        inference_params_sha=inf_sha,
    )
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="fitness failed|fn_wilson"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_halts_on_corpus_drift(tmp_path, monkeypatch):
    """Report's live_corpus_sha256 != current live corpus sha → halt."""
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    primary, recheck = _current_prompt_tuple()
    report_root = tmp_path / "reports"
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    monkeypatch.setenv("DSAR_CANARY_PATH_OVERRIDE", str(canary))

    from dsar_orchestrator.audit import PipelineAuditor
    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    inf_sha = _current_inference_params_sha(cfg)
    # Plant a passing report with a wrong corpus sha.
    _write_report(
        report_root,
        "test_deploy",
        passed=True,
        age_days=1.0,
        corpus_sha="0" * 64,
        primary_seal=primary,
        recheck_seal=recheck,
        inference_params_sha=inf_sha,
    )
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="drift|corpus_sha"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_passes_when_fresh_passing_matching(tmp_path, monkeypatch):
    """Happy path: fresh, passing, corpus_sha matches → no exception."""
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    from dsar_pipeline.canary_corpus import compute_corpus_sha256

    live_sha = compute_corpus_sha256(canary)
    primary, recheck = _current_prompt_tuple()
    report_root = tmp_path / "reports"
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    monkeypatch.setenv("DSAR_CANARY_PATH_OVERRIDE", str(canary))

    from dsar_orchestrator.audit import PipelineAuditor
    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.pipeline import _run_fitness_preflight

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    inf_sha = _current_inference_params_sha(cfg)
    _write_report(
        report_root,
        "test_deploy",
        passed=True,
        age_days=1.0,
        corpus_sha=live_sha,
        primary_seal=primary,
        recheck_seal=recheck,
        inference_params_sha=inf_sha,
    )
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    _run_fitness_preflight(cfg, auditor)  # must not raise


def test_preflight_force_skip_writes_audit_row_and_proceeds(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case, force_skip_fitness_reason="operator pilot run")
    canary = tmp_path / "canary"
    _seed_canary(canary)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))

    from dsar_orchestrator.audit import PipelineAuditor
    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.pipeline import _run_fitness_preflight

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    _run_fitness_preflight(cfg, auditor)  # must not raise

    skip_audit = case / "case_audit" / "skip_fitness.json"
    assert skip_audit.is_file()
    rec = json.loads(skip_audit.read_text(encoding="utf-8"))
    assert rec["reason"] == "operator pilot run"
    assert "os_user" in rec
    assert "hostname" in rec
    assert "timestamp" in rec
    assert "fitness_tuple" in rec


def test_preflight_skipped_when_disabled(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case, fitness_check_enabled=False)

    from dsar_orchestrator.audit import PipelineAuditor
    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.pipeline import _run_fitness_preflight

    cfg = load_case_config("TEST", case_root=case)
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    _run_fitness_preflight(cfg, auditor)  # must be no-op
