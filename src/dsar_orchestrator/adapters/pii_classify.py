"""Conductor-owned pii-classify adapter — Stage 5 (LLM PII classifier).

Bridges to the toolkit's ``dsar_pii_classifier.core.discover_case``.
That function runs the v3 detector cascade (Presidio, scrubadub,
fuzzy, gliner, mosaic, ...) and returns a ``{stage: [Finding]}`` map
while also writing ``~/.dsar-audit/<case_id>/pii_findings_stage<N>.jsonl``.

The conductor's cascade expects ``working/pii_collection.jsonl`` with
one row per ref, each carrying ``in_scope_recheck`` + entities +
upstream_hash. This adapter aggregates discover_case's per-stage
findings into that per-ref shape.

**Retirement contract.** When the toolkit ships a thin Python entry
``dsar_pii_classifier.core.classify_case_for_conductor(case_path)``
that writes pii_collection.jsonl directly, this adapter retires.
Output shape locked.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import BudgetExceededError, DSARPipelineError
from dsar_orchestrator.hash_chain import hash_pairs, sha256_file, sha256_text

PRODUCER_VERSION = "dsar_orchestrator.adapters.pii_classify 0.4.9"
SCHEMA_VERSION = "1.0"

# Injectable classifier: (case_path, mode) -> {stage_no: [finding-dict]}.
# Production resolution lazy-imports dsar_pii_classifier.core.discover_case.
ClassifierFn = Callable[[Path, str], dict[int, list[Any]]]


def _default_classifier() -> ClassifierFn:
    """Lazy-resolve `dsar_pii_classifier.core.discover_case`."""
    try:
        mod = importlib.import_module("dsar_pii_classifier.core")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_pii_classifier.core is not installed. The conductor's "
            "pii-classify adapter needs it to run the detector cascade. "
            "Install dsar-toolkit (pip install -e ~/projects/"
            "dsar-toolkit/) and retry."
        ) from exc

    def run(case_path: Path, mode: str) -> dict[int, list[Any]]:
        return mod.discover_case(case_path, mode=mode)

    return run


def run_for_case(cfg: CaseConfig, *, classifier_fn: ClassifierFn | None = None) -> None:
    """Run the toolkit's detector cascade; aggregate to pii_collection.jsonl.

    No-op when ``cfg.pii_classify_mode == "off"``.
    """
    if cfg.pii_classify_mode == "off":
        return

    if classifier_fn is None:
        classifier_fn = _default_classifier()

    # Toolkit's discover_case raises when subject_identifier is missing
    # (Phase 4 prereq). Validation also done in pipeline.run() at startup,
    # but defending here keeps the adapter self-contained.
    if cfg.subject_identifier is None:
        raise DSARPipelineError(
            f"case={cfg.case_no}: pii_classify_mode={cfg.pii_classify_mode!r} "
            f"requires case_config.subject_identifier."
        )

    try:
        findings_by_stage = classifier_fn(cfg.case_path, cfg.pii_classify_mode)
    except Exception as exc:
        # The toolkit's classifier raises PIIBudgetExceeded when the
        # per-case Haiku 4.5 budget cap is hit; wrap for the
        # orchestrator's typed surface.
        if type(exc).__name__ == "PIIBudgetExceeded":
            raise BudgetExceededError(str(exc)) from exc
        raise

    by_ref = _aggregate_by_ref(findings_by_stage)
    upstream_hash = _compute_upstream_hash(cfg)
    _write_pii_collection(cfg.case_path, by_ref, cfg.pii_classify_mode, upstream_hash)


# ─── aggregation ────────────────────────────────────────────────────


def _aggregate_by_ref(
    findings_by_stage: dict[int, list[Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group findings by their ``ref`` field. Each Finding is expected
    to be a dataclass / object with ``ref`` and ``surface`` (and other
    fields); falls back to dict-style access for stubs.

    Returns ``{ref: [entity_dict, ...]}``.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for stage_no, findings in findings_by_stage.items():
        for f in findings:
            ref = _attr(f, "ref")
            if not ref:
                continue
            entity = {
                "stage": stage_no,
                "surface": _attr(f, "surface") or _attr(f, "value") or "",
                "type": _attr(f, "type") or _attr(f, "category") or "unknown",
                "detector": _attr(f, "detector") or _attr(f, "source") or "",
            }
            confidence = _attr(f, "confidence")
            if confidence is not None:
                entity["confidence"] = confidence
            out.setdefault(ref, []).append(entity)
    return out


def _attr(obj: Any, name: str) -> Any:
    """Read `name` from either dataclass-style or dict-style objects."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


# ─── upstream hash ─────────────────────────────────────────────────


def _compute_upstream_hash(cfg: CaseConfig) -> str:
    """Mirror stages._hash_pii_classify_inputs: hash of all *_tags.json
    files + subject_identifier.primary_name + mode.

    The cascade uses this to invalidate pii_collection downstream when
    upstream changes.
    """
    tags_dir = cfg.case_path / "working"
    pairs: list[tuple[str, str]] = []
    if tags_dir.exists():
        for p in sorted(tags_dir.glob("*_tags.json")):
            pairs.append((p.name, sha256_file(p)))
    tags_hash = hash_pairs(pairs) if pairs else ""
    subj = cfg.subject_identifier.primary_name if cfg.subject_identifier else ""
    return sha256_text(f"{tags_hash}\x1f{subj}\x1fmode={cfg.pii_classify_mode}")


# ─── output ────────────────────────────────────────────────────────


def _write_pii_collection(
    case_path: Path,
    by_ref: dict[str, list[dict[str, Any]]],
    mode: str,
    upstream_hash: str,
) -> None:
    """Write ``working/pii_collection.jsonl`` atomically.

    One row per ref. Refs come from the aggregated findings; if a ref
    in register.json has no findings, no row is emitted (consistent
    with "discover" semantics — absent ref = no PII detected).

    ``in_scope_recheck`` defaults to ``"confirmed"`` since the toolkit's
    discover_case doesn't currently produce per-ref scope re-verification
    verdicts. When the toolkit ships richer per-ref verdicts, the
    adapter will pass them through here.
    """
    out_path = case_path / "working" / "pii_collection.jsonl"
    tmp_path = out_path.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for ref, entities in sorted(by_ref.items()):
            row = {
                "ref": ref,
                "in_scope_recheck": "confirmed",
                "entities": entities,
                "mode": mode,
                "upstream_hash": upstream_hash,
                "schema_version": SCHEMA_VERSION,
                "producer_version": PRODUCER_VERSION,
            }
            f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
