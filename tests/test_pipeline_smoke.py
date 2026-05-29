"""Smoke tests for pipeline.run() — covers the toolkit-independent
behaviour: stage planning, --check / --dry-run, config validation
errors. Stages that need toolkit modules are tested separately when
those modules exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.pipeline import STAGE_ORDER, build_stage_plan, run

# ─── stage planning ───


def test_stage_order_includes_all_stages() -> None:
    assert STAGE_ORDER == (
        "ingest",
        "stage_2_parallel",
        "stage_3_parallel",
        "sig_block_discovery",
        "scope_classify",
        "pii_classify",
        "redact",
        "presidio_anonymize",
        "pii_jury_review",
        "verify_spec",
        "bake",
        "verify_pdf",
        "export",
    )


def test_stage_plan_defaults_to_all_enabled(case_root: Path):
    from dsar_orchestrator.config import load_case_config

    cfg = load_case_config("300001", case_root=case_root)
    plan = build_stage_plan(case_root, cfg, None, None, None)
    assert plan.stages == list(STAGE_ORDER)
    assert plan.skipped == []


def test_stage_plan_skips_pii_when_mode_off(case_root: Path, monkeypatch):
    from dsar_orchestrator.config import load_case_config

    monkeypatch.setenv("PII_CLASSIFY_MODE", "off")
    cfg = load_case_config("300001", case_root=case_root)
    plan = build_stage_plan(case_root, cfg, None, None, None)
    assert "pii_classify" not in plan.stages
    assert ("pii_classify", "PII_CLASSIFY_MODE=off") in plan.skipped


def test_stage_plan_skips_verify_when_disabled(case_root: Path, monkeypatch):
    from dsar_orchestrator.config import load_case_config

    monkeypatch.setenv("REDACT_VERIFY_ENABLED", "false")
    cfg = load_case_config("300001", case_root=case_root)
    plan = build_stage_plan(case_root, cfg, None, None, None)
    assert "verify_pdf" not in plan.stages
    assert ("verify_pdf", "REDACT_VERIFY_ENABLED=false") in plan.skipped


def test_stage_plan_from_redact(case_root: Path):
    from dsar_orchestrator.config import load_case_config

    cfg = load_case_config("300001", case_root=case_root)
    plan = build_stage_plan(case_root, cfg, "redact", None, None)
    assert plan.stages[0] == "redact"
    assert "ingest" not in plan.stages
    assert "embed" not in plan.stages


def test_stage_plan_through_scope_classify(case_root: Path):
    from dsar_orchestrator.config import load_case_config

    cfg = load_case_config("300001", case_root=case_root)
    plan = build_stage_plan(case_root, cfg, None, "scope_classify", None)
    assert "scope_classify" in plan.stages
    assert "redact" not in plan.stages
    assert "export" not in plan.stages


def test_stage_plan_only_one(case_root: Path):
    from dsar_orchestrator.config import load_case_config

    cfg = load_case_config("300001", case_root=case_root)
    plan = build_stage_plan(case_root, cfg, None, None, "embed")
    assert plan.stages == ["embed"]


def test_stage_plan_only_rejects_unknown_stage(case_root: Path):
    from dsar_orchestrator.config import load_case_config

    cfg = load_case_config("300001", case_root=case_root)
    with pytest.raises(ValueError, match="Unknown stage"):
        build_stage_plan(case_root, cfg, None, None, "made_up")


def test_plan_render_lists_stages(case_root: Path):
    from dsar_orchestrator.config import load_case_config

    cfg = load_case_config("300001", case_root=case_root)
    plan = build_stage_plan(case_root, cfg, None, None, None)
    out = plan.render()
    assert "Case 300001 resume plan:" in out
    assert "ingest" in out
    assert "export" in out


# ─── --check / --dry-run ───


def test_run_check_does_not_execute_stages(case_root: Path, capsys):
    report = run("300001", case_root=case_root, check=True)
    assert report.case_no == "300001"
    captured = capsys.readouterr()
    assert "resume plan" in captured.out


def test_run_dry_run_does_not_execute_stages(case_root: Path, capsys):
    report = run("300001", case_root=case_root, dry_run=True)
    assert report.case_no == "300001"


# ─── config validation surfaces clearly ───


def test_run_raises_when_phase4_prereqs_missing(tmp_path: Path):
    case_root = tmp_path / "300099"
    case_root.mkdir()
    (case_root / "case_config.json").write_text(
        json.dumps(
            {
                "case_no": "300099",
                "case_scope": "minimal",
                "pii_classify_mode": "shadow",
                # no subject_identifier
            }
        )
    )
    with pytest.raises(ValueError, match="subject_identifier"):
        run("300099", case_root=case_root, check=True)


def test_run_skips_phase4_validation_when_mode_off(tmp_path: Path):
    case_root = tmp_path / "300098"
    case_root.mkdir()
    (case_root / "case_config.json").write_text(
        json.dumps(
            {
                "case_no": "300098",
                "case_scope": "minimal",
                "pii_classify_mode": "off",
            }
        )
    )
    # No raise — pii mode off means subject_identifier is not required
    report = run("300098", case_root=case_root, check=True)
    assert report.case_no == "300098"
