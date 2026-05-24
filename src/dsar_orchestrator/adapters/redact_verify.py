"""Conductor-owned redact-verify adapter — Stage 7 (renamed to verify_pdf in v5.0 Phase 3).

Bridges to the toolkit's ``dsar_pipeline.post_bake_verify.verify_for_conductor``.
The verifier inspects redacted output + emits per-finding rows to
``<case>/working/post_bake_findings.jsonl``; the conductor inspects
the Verdict object the call returns and halts the pipeline on any
failure.

This is a thin adapter: the toolkit ships the flat Python entry, so
no subprocess is needed. The adapter adds testability (injectable
verifier) + a typed exception surface + a no-op gate when the
operator has disabled verification.

**Retirement contract.** The toolkit-side retirement trigger
(``dsar_pipeline.post_bake_verify.verify_for_conductor``) has shipped.
The adapter stays for halt-formatting + injection + no-op gate —
these remain conductor concerns.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError, PipelineHalt

PRODUCER_VERSION = "dsar_orchestrator.adapters.redact_verify 0.1.0"

# Injectable verifier: (case_path) -> Verdict-like.
# Production resolution lazy-imports dsar_pipeline.post_bake_verify.verify_for_conductor.
VerifyFn = Callable[[Any], Any]


def _default_verifier() -> VerifyFn:
    """Lazy-resolve ``dsar_pipeline.post_bake_verify.verify_for_conductor``.

    Toolkit merged this entry 2026-05-24 (was previously the fictional
    ``dsar_redact_verify.core.verify_case`` — bug fix #1).
    """
    try:
        mod = importlib.import_module("dsar_pipeline.post_bake_verify")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_pipeline.post_bake_verify is not installed. The "
            "conductor's redact-verify adapter needs it to validate "
            "redacted output. Install dsar-toolkit (pip install -e "
            "~/projects/dsar-toolkit/) and retry."
        ) from exc

    def run(case_path: Any) -> Any:
        return mod.verify_for_conductor(case_path)

    return run


def run_for_case(cfg: CaseConfig, *, verify_fn: VerifyFn | None = None) -> None:
    """Drive the toolkit's verifier; raise PipelineHalt on any failure.

    No-op when ``cfg.redact_verify_enabled`` is False — matches the
    existing pipeline behaviour where the stage is skipped entirely
    rather than running and short-circuiting inside the verifier.
    """
    if not cfg.redact_verify_enabled:
        return

    if verify_fn is None:
        verify_fn = _default_verifier()

    verdict = verify_fn(cfg.case_path)

    if not getattr(verdict, "all_passed", False):
        failed_count = getattr(verdict, "failed_doc_count", "?")
        summary = getattr(verdict, "failed_verifier_summary", "")
        audit_log = verdict.audit_log_path
        raise PipelineHalt(
            f"case={cfg.case_no} redact-verify failed: "
            f"{failed_count} doc(s) flagged ({summary}). "
            f"See {audit_log}. "
            f"Re-run after fixing: "
            f"dsar-conductor --case {cfg.case_no} --from redact"
        )
