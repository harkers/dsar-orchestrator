"""Tests for the dsar-durant-calibration CLI promotion (#111 sub-6).

Covers sample-building stratification, decision persistence (append-only,
latest-wins), agreement report math, normalisation helpers, and subject
display name resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


@pytest.fixture
def case_root(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    (tmp_path / "audit").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(json.dumps({"full_name": "Jane Doe"}))
    return tmp_path


def _seed_verdicts(
    case_root: Path,
    *,
    durant: list[dict],
    recheck: list[dict] | None = None,
) -> None:
    """Write durant + recheck verdicts + a minimal register."""
    refs = {r["doc_ref"] for r in durant}
    register = [
        {"ref": ref, "filename": f"{ref}.eml", "text_file": str(case_root / f"{ref}.txt")}
        for ref in sorted(refs)
    ]
    for entry in register:
        Path(entry["text_file"]).write_text(f"body for {entry['ref']}", encoding="utf-8")
    (case_root / "working" / "register.json").write_text(json.dumps(register))
    with (case_root / "working" / "durant_verdicts.jsonl").open("w") as f:
        for r in durant:
            f.write(json.dumps(r) + "\n")
    if recheck:
        with (case_root / "working" / "durant_underdisclosure_recheck.jsonl").open("w") as f:
            for r in recheck:
                f.write(json.dumps(r) + "\n")


# --- module ---


def test_module_importable() -> None:
    import dsar_orchestrator.local_broker.durant_calibration as mod

    assert hasattr(mod, "build_sample")
    assert hasattr(mod, "agreement_report")
    assert hasattr(mod, "main")
    assert hasattr(mod, "make_handler")
    assert sum(mod.SAMPLE_STRATA.values()) == 30


def test_case_root_resolution(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import _resolve_case_root

    assert _resolve_case_root(tmp_path) == tmp_path
    monkeypatch.setenv("DSAR_CASE_ROOT", str(tmp_path))
    assert _resolve_case_root(None) == tmp_path
    monkeypatch.delenv("DSAR_CASE_ROOT")
    monkeypatch.chdir(tmp_path)
    assert _resolve_case_root(None) == tmp_path


# --- subject display name ---


def test_subject_display_name_from_data_subject_json(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import _subject_display_name

    assert _subject_display_name(case_root) == "Jane Doe"


def test_subject_display_name_falls_back_to_dir_name(tmp_path: Path) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import _subject_display_name

    case = tmp_path / "case-bob"
    case.mkdir()
    (case / "working").mkdir()
    assert _subject_display_name(case) == "case-bob"


# --- Normalisation ---


def test_normalisation_helpers() -> None:
    from dsar_orchestrator.local_broker.durant_calibration import (
        _normalise_durant,
        _normalise_op_verdict,
        _normalise_recheck,
    )

    assert _normalise_op_verdict("yes") == "include"
    assert _normalise_op_verdict("no") == "exclude"
    assert _normalise_op_verdict("uncertain") == "uncertain"

    assert _normalise_durant("biographical") == "include"
    assert _normalise_durant("work_context_only") == "exclude"
    assert _normalise_durant("ambiguous") == "uncertain"
    assert _normalise_durant(None) == "uncertain"

    assert _normalise_recheck("reclassify_to_biographical") == "include"
    assert _normalise_recheck("confirmed_work_context_only") == "exclude"
    assert _normalise_recheck("reclassify_to_ambiguous") == "uncertain"


# --- build_sample ---


def test_build_sample_picks_correct_strata(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import build_sample

    durant = []
    recheck = []
    # 12 disputed (need 10)
    for i in range(12):
        ref = f"disp-{i:02d}"
        durant.append({"doc_ref": ref, "durant_verdict": "work_context_only", "rationale": ""})
        recheck.append(
            {"doc_ref": ref, "recheck_verdict": "reclassify_to_biographical", "rationale": ""}
        )
    # 11 agreed-exclude (need 10)
    for i in range(11):
        ref = f"agr-{i:02d}"
        durant.append({"doc_ref": ref, "durant_verdict": "work_context_only", "rationale": ""})
        recheck.append(
            {"doc_ref": ref, "recheck_verdict": "confirmed_work_context_only", "rationale": ""}
        )
    # 7 recheck-ambiguous (need 5)
    for i in range(7):
        ref = f"amb-{i:02d}"
        durant.append({"doc_ref": ref, "durant_verdict": "work_context_only", "rationale": ""})
        recheck.append(
            {"doc_ref": ref, "recheck_verdict": "reclassify_to_ambiguous", "rationale": ""}
        )
    # 8 originally-biographical (need 5)
    for i in range(8):
        durant.append(
            {"doc_ref": f"bio-{i:02d}", "durant_verdict": "biographical", "rationale": ""}
        )
    _seed_verdicts(case_root, durant=durant, recheck=recheck)

    sample = build_sample(case_root)
    strata = {
        s: 0
        for s in (
            "disputed_recheck_says_bio",
            "agreed_work_context_only",
            "recheck_ambiguous",
            "originally_biographical",
        )
    }
    for s in sample:
        strata[s["stratum"]] += 1
    assert strata == {
        "disputed_recheck_says_bio": 10,
        "agreed_work_context_only": 10,
        "recheck_ambiguous": 5,
        "originally_biographical": 5,
    }
    # Persisted
    sample_path = case_root / "audit" / "calibration_sample_30.json"
    assert sample_path.exists()


def test_build_sample_under_samples_when_pool_small(case_root: Path) -> None:
    """If only 3 disputed candidates exist, sample takes all 3 (warning logged)."""
    from dsar_orchestrator.local_broker.durant_calibration import build_sample

    durant = []
    recheck = []
    for i in range(3):
        ref = f"disp-{i}"
        durant.append({"doc_ref": ref, "durant_verdict": "work_context_only", "rationale": ""})
        recheck.append(
            {"doc_ref": ref, "recheck_verdict": "reclassify_to_biographical", "rationale": ""}
        )
    # Pad bio so total isn't zero
    for i in range(8):
        durant.append({"doc_ref": f"bio-{i}", "durant_verdict": "biographical", "rationale": ""})
    _seed_verdicts(case_root, durant=durant, recheck=recheck)

    sample = build_sample(case_root)
    disputed = [s for s in sample if s["stratum"] == "disputed_recheck_says_bio"]
    assert len(disputed) == 3


def test_build_sample_deterministic_with_default_seed(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import build_sample

    durant = [
        {"doc_ref": f"disp-{i:02d}", "durant_verdict": "work_context_only", "rationale": ""}
        for i in range(30)
    ]
    recheck = [
        {
            "doc_ref": f"disp-{i:02d}",
            "recheck_verdict": "reclassify_to_biographical",
            "rationale": "",
        }
        for i in range(30)
    ]
    _seed_verdicts(case_root, durant=durant, recheck=recheck)

    s1 = build_sample(case_root)
    s2 = build_sample(case_root)
    assert [s["ref"] for s in s1] == [s["ref"] for s in s2]


# --- Decisions ---


def test_save_decision_append_only_latest_wins(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import (
        _load_decisions,
        _save_decision,
    )

    _save_decision(
        case_root,
        ref="doc-001",
        verdict="uncertain",
        notes="hmm",
        decided_at="2026-05-26T10:00:00Z",
        time_taken_s=12.5,
    )
    _save_decision(
        case_root,
        ref="doc-001",
        verdict="yes",
        notes="changed mind",
        decided_at="2026-05-26T10:05:00Z",
        time_taken_s=8.0,
    )
    decisions = _load_decisions(case_root)
    assert decisions["doc-001"]["verdict"] == "yes"
    # File contains BOTH rows (append-only audit trail)
    raw = (case_root / "working" / "operator_calibration_30.jsonl").read_text()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(rows) == 2


# --- agreement_report ---


def test_agreement_report_returns_string(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import (
        agreement_report,
        build_sample,
    )

    durant = [
        {"doc_ref": f"bio-{i:02d}", "durant_verdict": "biographical", "rationale": ""}
        for i in range(5)
    ]
    _seed_verdicts(case_root, durant=durant)
    build_sample(case_root)
    report = agreement_report(case_root)
    assert "Calibration report" in report
    assert "originally_biographical" in report


def test_agreement_report_no_sample(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_calibration import agreement_report

    assert agreement_report(case_root) == "no sample built yet"


def test_agreement_counts_match_decisions(case_root: Path) -> None:
    """Operator says yes; durant said biographical; agreement should count."""
    from dsar_orchestrator.local_broker.durant_calibration import (
        _save_decision,
        agreement_report,
        build_sample,
    )

    durant = [
        {"doc_ref": f"bio-{i:02d}", "durant_verdict": "biographical", "rationale": ""}
        for i in range(5)
    ]
    _seed_verdicts(case_root, durant=durant)
    sample = build_sample(case_root)
    for s in sample:
        _save_decision(
            case_root,
            ref=s["ref"],
            verdict="yes",
            notes="",
            decided_at="2026-05-26T10:00:00Z",
            time_taken_s=1.0,
        )
    report = agreement_report(case_root)
    assert "5/5" in report  # all agree
    assert "100%" in report
