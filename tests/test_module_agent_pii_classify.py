"""Tests for check_pii_classify smart-empty tolerance (Contract B / #12).

When pii_collection.jsonl is missing/empty:
- If scope_classify produced 0 in-scope ("present") verdicts → info (ok).
- If scope_classify produced ≥1 in-scope verdicts → critical (halts).
When populated: existing strict checks apply.
"""

from __future__ import annotations

import json
from pathlib import Path

from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.module_agents import check_pii_classify


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no="pii-test",
        case_path=case_path,
        case_scope="test scope",
        subject_identifier=SubjectIdentifier(primary_name="Test"),
        rerank_mode="shadow",
        rerank_threshold=0.01,
        rerank_top_n=20,
        rerank_sample_rate=0.05,
        pii_classify_mode="shadow",
        pii_budget_usd=5.0,
        discovery_enabled=False,
        redact_verify_enabled=True,
        llm_concurrency=5,
    )


def _seed_scope_verdicts(case_path: Path, verdicts: list[str]) -> None:
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    rows = [{"ref": f"r-{i}", "verdict": v} for i, v in enumerate(verdicts)]
    (working / "scope_verdicts.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")
    )


def test_empty_pii_collection_no_in_scope_is_info(tmp_path) -> None:
    """When scope had 0 'present' verdicts, empty pii_collection is OK."""
    cfg = _make_cfg(tmp_path)
    _seed_scope_verdicts(tmp_path, ["ambiguous", "not_present", "ambiguous"])

    result = check_pii_classify(cfg)
    assert result.ok is True
    assert result.severity == "info"
    assert any(
        "nothing to classify" in f.lower() or "no in-scope" in f.lower() for f in result.findings
    )


def test_empty_pii_collection_with_in_scope_is_critical(tmp_path) -> None:
    """When scope had ≥1 'present' verdicts, empty pii_collection is wrong."""
    cfg = _make_cfg(tmp_path)
    _seed_scope_verdicts(tmp_path, ["present", "not_present", "present"])

    result = check_pii_classify(cfg)
    assert result.ok is False
    assert result.severity == "critical"


def test_populated_pii_collection_uses_existing_strict_checks(tmp_path) -> None:
    """When pii_collection has rows, existing field-validity checks run."""
    cfg = _make_cfg(tmp_path)
    _seed_scope_verdicts(tmp_path, ["present"])
    working = tmp_path / "working"
    # Row missing in_scope_recheck → triggers existing critical
    (working / "pii_collection.jsonl").write_text(
        json.dumps({"ref": "r-0", "entities": [], "upstream_hash": "h"}) + "\n"
    )

    result = check_pii_classify(cfg)
    assert result.ok is False
    assert result.severity == "critical"
