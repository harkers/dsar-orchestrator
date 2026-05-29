"""Tests verifying the three new stages from Phase 3/5 + Presidio are
wired into the orchestrator pipeline."""

from __future__ import annotations

import pytest

from dsar_orchestrator.pipeline import (
    ALL_STAGE_NAMES,
    STAGE_ORDER,
    SUB_STAGES_BY_STAGE,
)


# ---- STAGE_ORDER ----


def test_sig_block_discovery_in_stage_order() -> None:
    assert "sig_block_discovery" in STAGE_ORDER


def test_pii_jury_review_in_stage_order() -> None:
    assert "pii_jury_review" in STAGE_ORDER


def test_presidio_anonymize_in_stage_order() -> None:
    assert "presidio_anonymize" in STAGE_ORDER


def test_sig_block_runs_after_stage_3_parallel() -> None:
    """sig_block_discovery extends people_register (which lives in
    stage_3_parallel) so it must run AFTER it."""
    assert STAGE_ORDER.index("sig_block_discovery") > STAGE_ORDER.index("stage_3_parallel")


def test_sig_block_runs_before_redact() -> None:
    """sig_block_discovery enriches the people_register that the redact
    stage consumes (via the third_party_denylist)."""
    assert STAGE_ORDER.index("sig_block_discovery") < STAGE_ORDER.index("redact")


def test_pii_jury_runs_after_redact() -> None:
    """pii_jury_review is post-redact defense-in-depth per spec §1.8."""
    assert STAGE_ORDER.index("pii_jury_review") > STAGE_ORDER.index("redact")


def test_pii_jury_runs_before_export() -> None:
    """pii_jury_review verdicts gate the final_synth/bake/export per
    spec §1.8 ('Gates final_synth')."""
    assert STAGE_ORDER.index("pii_jury_review") < STAGE_ORDER.index("export")


def test_presidio_anonymize_runs_after_redact() -> None:
    assert STAGE_ORDER.index("presidio_anonymize") > STAGE_ORDER.index("redact")


# ---- SUB_STAGES_BY_STAGE ----


def test_sig_block_discovery_in_sub_stages() -> None:
    assert "sig_block_discovery" in SUB_STAGES_BY_STAGE


def test_pii_jury_review_in_sub_stages() -> None:
    assert "pii_jury_review" in SUB_STAGES_BY_STAGE


def test_presidio_anonymize_in_sub_stages() -> None:
    assert "presidio_anonymize" in SUB_STAGES_BY_STAGE


# ---- ALL_STAGE_NAMES ----


def test_new_stages_in_all_stage_names() -> None:
    """--only choices should accept the new stage names."""
    assert "sig_block_discovery" in ALL_STAGE_NAMES
    assert "pii_jury_review" in ALL_STAGE_NAMES
    assert "presidio_anonymize" in ALL_STAGE_NAMES


# ---- Adapter modules ----


def test_sig_block_discovery_adapter_importable() -> None:
    from dsar_orchestrator.adapters import sig_block_discovery

    assert hasattr(sig_block_discovery, "run_for_case")


def test_pii_jury_review_adapter_importable() -> None:
    from dsar_orchestrator.adapters import pii_jury_review

    assert hasattr(pii_jury_review, "run_for_case")


def test_presidio_anonymize_adapter_importable() -> None:
    from dsar_orchestrator.adapters import presidio_anonymize

    assert hasattr(presidio_anonymize, "run_for_case")


# ---- Adapters call the toolkit's run_* functions ----


def test_sig_block_adapter_invokes_toolkit_with_case_path(tmp_path) -> None:
    """The adapter passes cfg.case_path to the toolkit's run_*."""
    from dsar_orchestrator.adapters import sig_block_discovery
    from dsar_orchestrator.config import CaseConfig

    calls = []

    def _fake(case_dir):
        calls.append(case_dir)
        return {"candidates_found": 0}

    cfg = CaseConfig(case_no=tmp_path.name, case_path=tmp_path)
    sig_block_discovery.run_for_case(cfg, run_fn=_fake)
    assert calls == [tmp_path]


def test_pii_jury_adapter_passes_case_config_dict(tmp_path) -> None:
    """The pii_jury adapter constructs the case_config dict the toolkit
    expects (with pii_jury_dual_juror + data_subject keys)."""
    from dsar_orchestrator.adapters import pii_jury_review
    from dsar_orchestrator.config import CaseConfig

    calls = []

    def _fake(case_dir, **kwargs):
        calls.append((case_dir, kwargs))
        return {"verdicts_written": 0}

    cfg = CaseConfig(case_no=tmp_path.name, case_path=tmp_path, pii_jury_dual_juror=True)
    pii_jury_review.run_for_case(cfg, run_fn=_fake)
    assert len(calls) == 1
    case_dir, kwargs = calls[0]
    assert case_dir == tmp_path
    assert "case_config" in kwargs
    assert kwargs["case_config"]["pii_jury_dual_juror"] is True
    assert "data_subject" in kwargs["case_config"]


def test_presidio_anonymize_adapter_invokes_toolkit(tmp_path) -> None:
    from dsar_orchestrator.adapters import presidio_anonymize
    from dsar_orchestrator.config import CaseConfig

    calls = []

    def _fake(case_dir):
        calls.append(case_dir)
        return {"refs_processed": 0}

    cfg = CaseConfig(case_no=tmp_path.name, case_path=tmp_path)
    presidio_anonymize.run_for_case(cfg, run_fn=_fake)
    assert calls == [tmp_path]


def test_pii_jury_adapter_forwards_vulnerable_true(tmp_path) -> None:
    """DeepSeek convergent jury finding: spec §1.8 trigger (b) requires
    auto-promotion to dual juror when data_subject.vulnerable is True.
    The adapter MUST forward this flag from working/data_subject.json."""
    import json

    from dsar_orchestrator.adapters import pii_jury_review
    from dsar_orchestrator.config import CaseConfig

    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "X", "vulnerable": True})
    )

    captured = {}

    def _fake(case_dir, **kwargs):
        captured.update(kwargs)
        return {"verdicts_written": 0}

    cfg = CaseConfig(case_no=tmp_path.name, case_path=tmp_path)
    pii_jury_review.run_for_case(cfg, run_fn=_fake)
    assert captured["case_config"]["data_subject"]["vulnerable"] is True


def test_pii_jury_adapter_forwards_vulnerable_false_default(tmp_path) -> None:
    """Missing 'vulnerable' key in data_subject.json -> False (the
    explicit-opt-in semantics the spec implies)."""
    import json

    from dsar_orchestrator.adapters import pii_jury_review
    from dsar_orchestrator.config import CaseConfig

    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "X"})  # no vulnerable flag
    )

    captured = {}

    def _fake(case_dir, **kwargs):
        captured.update(kwargs)
        return {"verdicts_written": 0}

    cfg = CaseConfig(case_no=tmp_path.name, case_path=tmp_path)
    pii_jury_review.run_for_case(cfg, run_fn=_fake)
    assert captured["case_config"]["data_subject"]["vulnerable"] is False


def test_pii_jury_adapter_no_data_subject_file_treated_as_not_vulnerable(tmp_path) -> None:
    """Defensive: missing data_subject.json -> vulnerable=False (don't
    crash the adapter; let the conductor's people_register preflight
    catch the missing-file condition instead)."""
    from dsar_orchestrator.adapters import pii_jury_review
    from dsar_orchestrator.config import CaseConfig

    (tmp_path / "working").mkdir()

    captured = {}

    def _fake(case_dir, **kwargs):
        captured.update(kwargs)
        return {"verdicts_written": 0}

    cfg = CaseConfig(case_no=tmp_path.name, case_path=tmp_path)
    pii_jury_review.run_for_case(cfg, run_fn=_fake)
    assert captured["case_config"]["data_subject"]["vulnerable"] is False
