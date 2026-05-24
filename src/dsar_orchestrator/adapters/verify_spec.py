"""Conductor-owned verify-spec adapter — Stage 7 in v5.5.

Bridges to the toolkit's ``dsar_pipeline.verify_spec.verify_for_conductor``.
The verifier inspects the redaction plan (``working/redaction_input.jsonl``)
against the upstream PII evidence (``working/pii_findings.jsonl``) and
emits per-failure rows to ``<case>/working/verify_spec_findings.jsonl``;
the conductor inspects the Verdict object the call returns and halts the
pipeline on any failure.

verify_spec is always-on — there is no enable flag. The operator's only
way to skip it is ``--from bake`` or later. The motivation is that
verify_spec is cheap (pure plan inspection, no LLM, no bake) and catches
plan-level mistakes BEFORE the expensive bake stage spends multi-minute
work on a doomed plan.

This is a thin adapter: the toolkit ships the flat Python entry, so no
subprocess is needed. The adapter adds testability (injectable verifier)
and a typed exception surface.

**Retirement contract.** The toolkit-side retirement trigger
(``dsar_pipeline.verify_spec.verify_for_conductor``) has shipped. The
adapter stays for halt-formatting + injection — these remain conductor
concerns.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError, PipelineHalt

PRODUCER_VERSION = "dsar_orchestrator.adapters.verify_spec 0.3.0"

# Injectable verifier: (case_path) -> Verdict-like.
# Production resolution lazy-imports dsar_pipeline.verify_spec.verify_for_conductor.
VerifyFn = Callable[[Any], Any]


def _default_verifier() -> VerifyFn:
    """Lazy-resolve ``dsar_pipeline.verify_spec.verify_for_conductor``.

    Toolkit merged this entry 2026-05-24 alongside post_bake_verify.
    """
    try:
        mod = importlib.import_module("dsar_pipeline.verify_spec")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_pipeline.verify_spec is not installed. The conductor's "
            "verify-spec adapter needs it to validate the redaction plan "
            "before bake. Install dsar-toolkit (pip install -e "
            "~/projects/dsar-toolkit/) and retry."
        ) from exc

    def run(case_path: Any) -> Any:
        return mod.verify_for_conductor(case_path)

    return run


def run_for_case(cfg: CaseConfig, *, verify_fn: VerifyFn | None = None) -> None:
    """Drive the toolkit's spec-level verifier; raise PipelineHalt on any failure.

    Always runs — verify_spec has no enable flag. To skip it, the operator
    invokes ``dsar-conductor --case <id> --from bake`` (or later).
    """
    if verify_fn is None:
        verify_fn = _default_verifier()

    verdict = verify_fn(cfg.case_path)

    if not getattr(verdict, "all_passed", False):
        failed_count = getattr(verdict, "failed_doc_count", "?")
        summary = getattr(verdict, "failed_verifier_summary", "")
        audit_log = verdict.audit_log_path
        raise PipelineHalt(
            f"case={cfg.case_no} spec-verify failed: "
            f"{failed_count} doc(s) flagged ({summary}). "
            f"See {audit_log}. "
            f"Re-run after fixing: "
            f"dsar-conductor --case {cfg.case_no} --from redact"
        )
