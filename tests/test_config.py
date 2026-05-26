"""Tests for case-config loading + Phase 4 prereq validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.config import (
    CaseConfig,
    SubjectIdentifier,
    load_case_config,
    validate_phase_4_prereqs,
)


def test_load_case_config_basic(case_root: Path) -> None:
    cfg = load_case_config("300001", case_root=case_root)
    assert cfg.case_no == "300001"
    assert cfg.case_path == case_root
    assert cfg.case_scope.startswith("All personal data about James Carter")
    assert cfg.subject_identifier is not None
    assert cfg.subject_identifier.primary_name == "James Carter"
    assert "J. Carter" in cfg.subject_identifier.aliases


def test_load_case_config_applies_defaults(tmp_path: Path) -> None:
    case_root = tmp_path / "300002"
    case_root.mkdir()
    (case_root / "case_config.json").write_text(
        json.dumps({"case_no": "300002", "case_scope": "minimal"})
    )
    cfg = load_case_config("300002", case_root=case_root)
    assert cfg.rerank_mode == "shadow"
    assert cfg.rerank_threshold == 0.01
    assert cfg.pii_classify_mode == "shadow"
    assert cfg.pii_budget_usd == 10.0
    assert cfg.discovery_enabled is True
    assert cfg.redact_verify_enabled is True


def test_load_case_config_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Case directory not found"):
        load_case_config("nope", case_root=tmp_path / "does-not-exist")


def test_load_case_config_missing_config_raises(tmp_path: Path) -> None:
    case_root = tmp_path / "300003"
    case_root.mkdir()
    with pytest.raises(FileNotFoundError, match="No case_config.json"):
        load_case_config("300003", case_root=case_root)


def test_env_override_rerank_mode(case_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("RERANK_MODE", "enforce")
    cfg = load_case_config("300001", case_root=case_root)
    assert cfg.rerank_mode == "enforce"


def test_env_override_pii_mode(case_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("PII_CLASSIFY_MODE", "off")
    cfg = load_case_config("300001", case_root=case_root)
    assert cfg.pii_classify_mode == "off"


def test_env_override_invalid_mode_raises(case_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("RERANK_MODE", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        load_case_config("300001", case_root=case_root)


def test_override_file_beats_env(case_root: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RERANK_MODE", "enforce")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".dsar-rerank-mode").write_text("off\n")
    cfg = load_case_config("300001", case_root=case_root)
    assert cfg.rerank_mode == "off"


def test_env_threshold_override(case_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("RERANK_THRESHOLD", "0.05")
    cfg = load_case_config("300001", case_root=case_root)
    assert cfg.rerank_threshold == 0.05


def test_env_budget_override(case_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("DSAR_PII_BUDGET_USD", "20")
    cfg = load_case_config("300001", case_root=case_root)
    assert cfg.pii_budget_usd == 20.0


# NB: test_env_discovery_disable removed in Contract B / #10 — the
# DISCOVERY_ENABLED env-var override was deleted along with the
# pii_discovery stage; the config field remains as a deprecated no-op
# carrier (removal target = v0.5.0).


# ─── validate_phase_4_prereqs ───


def test_validate_phase_4_passes_when_mode_off() -> None:
    cfg = CaseConfig(case_no="X", case_path=Path("/tmp"), pii_classify_mode="off")
    validate_phase_4_prereqs(cfg)  # no raise


def test_validate_phase_4_passes_with_subject() -> None:
    cfg = CaseConfig(
        case_no="X",
        case_path=Path("/tmp"),
        pii_classify_mode="shadow",
        subject_identifier=SubjectIdentifier(primary_name="James Carter"),
    )
    validate_phase_4_prereqs(cfg)  # no raise


def test_validate_phase_4_raises_when_missing_subject() -> None:
    cfg = CaseConfig(
        case_no="X",
        case_path=Path("/tmp"),
        pii_classify_mode="shadow",
        subject_identifier=None,
    )
    with pytest.raises(ValueError, match="subject_identifier"):
        validate_phase_4_prereqs(cfg)


def test_validate_phase_4_raises_when_empty_name() -> None:
    cfg = CaseConfig(
        case_no="X",
        case_path=Path("/tmp"),
        pii_classify_mode="enforce",
        subject_identifier=SubjectIdentifier(primary_name="   "),
    )
    with pytest.raises(ValueError, match="primary_name is required"):
        validate_phase_4_prereqs(cfg)


def test_subject_identifier_from_dict_returns_none_for_none() -> None:
    assert SubjectIdentifier.from_dict(None) is None


def test_subject_identifier_from_dict_parses_fields() -> None:
    si = SubjectIdentifier.from_dict(
        {
            "primary_name": "James Carter",
            "dob": "1985-03-12",
            "aliases": ["Jim"],
            "disambiguation_notes": "Not James Marshall.",
        }
    )
    assert si is not None
    assert si.primary_name == "James Carter"
    assert si.dob == "1985-03-12"
    assert si.aliases == ["Jim"]
    assert si.disambiguation_notes == "Not James Marshall."


# ─── Phase 5 model-fitness canary fields (spec §10.2) ───


def test_case_config_fitness_check_fields_default(tmp_path):
    """CaseConfig has fitness_check_* fields with safe defaults."""
    case_dir = tmp_path / "case_default"
    case_dir.mkdir()
    (case_dir / "case_config.json").write_text(
        '{"case_no": "TEST", "case_scope": "x"}', encoding="utf-8"
    )
    cfg = load_case_config("TEST", case_root=case_dir)
    assert cfg.fitness_check_enabled is True
    assert cfg.fitness_check_canary_path is None
    assert cfg.fitness_check_max_report_age_days == 30
    assert cfg.force_skip_fitness_reason == ""


def test_case_config_fitness_check_fields_from_yaml(tmp_path):
    """All 4 fitness_check_* fields read from case_config.json."""
    case_dir = tmp_path / "case_custom"
    case_dir.mkdir()
    (case_dir / "case_config.json").write_text(
        '{"case_no": "TEST", "case_scope": "x", '
        '"fitness_check_enabled": false, '
        '"fitness_check_canary_path": "/tmp/canary", '
        '"fitness_check_max_report_age_days": 7, '
        '"force_skip_fitness_reason": "operator pilot run"}',
        encoding="utf-8",
    )
    cfg = load_case_config("TEST", case_root=case_dir)
    assert cfg.fitness_check_enabled is False
    assert cfg.fitness_check_canary_path == Path("/tmp/canary")
    assert cfg.fitness_check_max_report_age_days == 7
    assert cfg.force_skip_fitness_reason == "operator pilot run"
