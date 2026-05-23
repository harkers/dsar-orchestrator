"""Tests for the pii_classify adapter — `adapters.pii_classify`.

Adapter calls `dsar_pii_classifier.core.discover_case` and aggregates
per-stage findings into per-ref rows in `working/pii_collection.jsonl`.
Classifier is injected so tests are hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import pii_classify as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import BudgetExceededError, DSARPipelineError


def _make_cfg(
    case_path: Path,
    *,
    mode: str = "shadow",
    subject_name: str | None = "James Carter",
) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=(SubjectIdentifier(primary_name=subject_name) if subject_name else None),
        pii_classify_mode=mode,
    )


def _seed_case_with_tags(tmp_path: Path, refs: list[str]) -> Path:
    case_path = tmp_path / "310000"
    working = case_path / "working"
    working.mkdir(parents=True)
    for ref in refs:
        (working / f"{ref}_tags.json").write_text(json.dumps({"ref": ref, "in_scope": True}))
    return case_path


# ─── happy path ────────────────────────────────────────────────────


def test_writes_one_row_per_ref_with_findings(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1", "d2"])

    def fake_classifier(cp: Path, mode: str) -> dict[int, list[dict]]:
        return {
            1: [
                {"ref": "d1", "surface": "James", "type": "subject_name", "detector": "spacy"},
                {"ref": "d2", "surface": "Carter", "type": "subject_name", "detector": "spacy"},
            ],
            2: [{"ref": "d1", "surface": "£78,400", "type": "salary", "detector": "regex"}],
        }

    adapter.run_for_case(_make_cfg(case_path), classifier_fn=fake_classifier)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "pii_collection.jsonl").read_text().splitlines()
        if line
    ]
    refs_in_output = {r["ref"] for r in rows}
    assert refs_in_output == {"d1", "d2"}


def test_aggregates_multiple_findings_per_ref(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    def fake_classifier(cp, mode):
        return {
            1: [{"ref": "d1", "surface": "James", "type": "name", "detector": "spacy"}],
            2: [{"ref": "d1", "surface": "£78,400", "type": "salary", "detector": "regex"}],
            3: [{"ref": "d1", "surface": "FIN-0241", "type": "employee_id", "detector": "fuzzy"}],
        }

    adapter.run_for_case(_make_cfg(case_path), classifier_fn=fake_classifier)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "pii_collection.jsonl").read_text().splitlines()
        if line
    ]
    assert len(rows) == 1
    assert len(rows[0]["entities"]) == 3
    entity_types = {e["type"] for e in rows[0]["entities"]}
    assert entity_types == {"name", "salary", "employee_id"}


def test_row_has_required_fields(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    def fake_classifier(cp, mode):
        return {1: [{"ref": "d1", "surface": "x", "type": "name", "detector": "test"}]}

    adapter.run_for_case(_make_cfg(case_path), classifier_fn=fake_classifier)

    row = json.loads((case_path / "working" / "pii_collection.jsonl").read_text().splitlines()[0])
    assert row["ref"] == "d1"
    assert row["in_scope_recheck"] == "confirmed"
    assert row["mode"] == "shadow"
    assert "upstream_hash" in row
    assert row["schema_version"] == "1.0"
    assert row["producer_version"].startswith("dsar_orchestrator.adapters.pii_classify")
    assert "entities" in row


def test_entities_carry_stage_surface_type_detector(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    def fake_classifier(cp, mode):
        return {
            2: [
                {
                    "ref": "d1",
                    "surface": "Test Subject",
                    "type": "name",
                    "detector": "presidio",
                    "confidence": 0.95,
                }
            ]
        }

    adapter.run_for_case(_make_cfg(case_path), classifier_fn=fake_classifier)

    row = json.loads((case_path / "working" / "pii_collection.jsonl").read_text().splitlines()[0])
    ent = row["entities"][0]
    assert ent["stage"] == 2
    assert ent["surface"] == "Test Subject"
    assert ent["type"] == "name"
    assert ent["detector"] == "presidio"
    assert ent["confidence"] == 0.95


def test_no_op_when_mode_off(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    def panic_classifier(cp, mode):
        raise RuntimeError("classifier should not have been called")

    adapter.run_for_case(_make_cfg(case_path, mode="off"), classifier_fn=panic_classifier)
    # No output file written
    assert not (case_path / "working" / "pii_collection.jsonl").exists()


def test_mode_propagated_to_row(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    def fake_classifier(cp, mode):
        return {1: [{"ref": "d1", "surface": "x", "type": "name", "detector": "t"}]}

    adapter.run_for_case(_make_cfg(case_path, mode="enforce"), classifier_fn=fake_classifier)
    row = json.loads((case_path / "working" / "pii_collection.jsonl").read_text().splitlines()[0])
    assert row["mode"] == "enforce"


def test_handles_dataclass_style_findings(tmp_path: Path) -> None:
    """Adapter must work with both dict-style and dataclass-style
    Finding objects (the toolkit may ship either)."""
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    class FakeFinding:
        def __init__(self, ref, surface, type_, detector):
            self.ref = ref
            self.surface = surface
            self.type = type_
            self.detector = detector

    def fake_classifier(cp, mode):
        return {1: [FakeFinding("d1", "X", "name", "fake")]}

    adapter.run_for_case(_make_cfg(case_path), classifier_fn=fake_classifier)

    row = json.loads((case_path / "working" / "pii_collection.jsonl").read_text().splitlines()[0])
    assert row["entities"][0]["surface"] == "X"


# ─── error handling ────────────────────────────────────────────────


def test_raises_when_subject_identifier_missing(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])
    cfg = _make_cfg(case_path, subject_name=None)
    with pytest.raises(DSARPipelineError, match="subject_identifier"):
        adapter.run_for_case(cfg, classifier_fn=lambda cp, mode: {1: []})


def test_wraps_pii_budget_exceeded(tmp_path: Path) -> None:
    """A toolkit exception type named 'PIIBudgetExceeded' (string match)
    is wrapped into the orchestrator's BudgetExceededError so the
    caller sees a typed surface."""
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    class PIIBudgetExceeded(Exception):
        pass

    def over_budget(cp, mode):
        raise PIIBudgetExceeded("budget cap hit at $9.99")

    cfg = _make_cfg(case_path)
    with pytest.raises(BudgetExceededError, match="budget cap"):
        adapter.run_for_case(cfg, classifier_fn=over_budget)


def test_propagates_other_classifier_exceptions(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    def bad_classifier(cp, mode):
        raise RuntimeError("something broke in the detector")

    cfg = _make_cfg(case_path)
    with pytest.raises(RuntimeError, match="something broke"):
        adapter.run_for_case(cfg, classifier_fn=bad_classifier)


# ─── atomic write ──────────────────────────────────────────────────


def test_no_temp_file_leftover(tmp_path: Path) -> None:
    case_path = _seed_case_with_tags(tmp_path, ["d1"])

    def fake_classifier(cp, mode):
        return {1: [{"ref": "d1", "surface": "x", "type": "n", "detector": "t"}]}

    adapter.run_for_case(_make_cfg(case_path), classifier_fn=fake_classifier)
    working = case_path / "working"
    assert not any(p.suffix == ".tmp" for p in working.iterdir())


def test_refs_without_findings_skipped(tmp_path: Path) -> None:
    """If no findings reference a particular ref, no row is emitted —
    consistent with discover semantics."""
    case_path = _seed_case_with_tags(tmp_path, ["d1", "d2", "d3"])

    def fake_classifier(cp, mode):
        return {1: [{"ref": "d1", "surface": "x", "type": "n", "detector": "t"}]}

    adapter.run_for_case(_make_cfg(case_path), classifier_fn=fake_classifier)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "pii_collection.jsonl").read_text().splitlines()
        if line
    ]
    assert [r["ref"] for r in rows] == ["d1"]
