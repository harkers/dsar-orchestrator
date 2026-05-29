"""Conductor adapter for pii_jury_review_stage (Phase 5 Task 4).

Bridges to the toolkit's
``dsar_pipeline.pii_jury_review_stage.run_pii_jury_review``.
Post-redact PII jury defence-in-depth: stratified-sample N refs,
ask LLM juror(s) to identify any third-party PII that survived
redaction, write verdicts to working/pii_jury_verdicts.jsonl with
full ROPA provenance.

Retirement: when the toolkit ships a thin
``run_for_case(case_path)`` entry that matches the conductor's
adapter contract.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from dsar_orchestrator.config import CaseConfig

PRODUCER_VERSION = "dsar_orchestrator.adapters.pii_jury_review 0.1.0"

RunFn = Callable[..., dict[str, Any]]


def _read_data_subject_vulnerable(case_path: Path) -> bool:
    """Spec §1.8 trigger (b): read the vulnerable flag from
    working/data_subject.json. Returns False if file missing /
    malformed / flag absent — explicit-opt-in semantics."""
    import json

    ds_path = case_path / "working" / "data_subject.json"
    if not ds_path.exists():
        return False
    try:
        ds = json.loads(ds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(ds, dict):
        return False
    return bool(ds.get("vulnerable", False))


def run_for_case(cfg: CaseConfig, *, run_fn: RunFn | None = None) -> None:
    """Invoke the toolkit's run_pii_jury_review with the case config.

    Builds the case_config dict the toolkit stage expects. Reads
    data_subject.vulnerable directly from working/data_subject.json
    (spec §1.8 trigger b) — the dataclass-side CaseConfig doesn't
    expose this field; data_subject.json is the source of truth."""
    if run_fn is None:
        from dsar_pipeline.pii_jury_review_stage import run_pii_jury_review

        run_fn = run_pii_jury_review
    case_config = {
        "pii_jury_dual_juror": getattr(cfg, "pii_jury_dual_juror", False),
        "data_subject": {
            "vulnerable": _read_data_subject_vulnerable(cfg.case_path),
        },
    }
    run_fn(cfg.case_path, case_config=case_config)
