"""The orchestrator's ``run(case)`` entry point.

Implements the DAG from
``docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v2.md``
§ "Full pipeline.run() pseudocode" — 8 stages with ThreadPoolExecutor
parallelism on Stages 2 + 3, hash-chain verification at stage
boundaries, StageBanner audit emission, and PipelineHalt on Phase 6
verifier failure.

Toolkit imports are **lazy** (inside each stage function). This lets
the orchestrator install + test in isolation; stages only fail when
actually run against a case that needs the missing toolkit module.
"""

from __future__ import annotations

import importlib
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from dsar_orchestrator.audit import PipelineAuditor, RunReport, StageBanner
from dsar_orchestrator.config import CaseConfig, load_case_config, validate_phase_4_prereqs
from dsar_orchestrator.exceptions import (
    BudgetExceededError,
    DSARPipelineError,
    PipelineHalt,
)

# Stage identifiers — used by --from/--through/--only and the resume
# plan. Ordered by data dependency.
#
# Two-level addressing:
#   STAGE_ORDER lists the 8 coarse stages (--from/--through use these
#   because resuming mid-parallel-group is awkward).
#   SUB_STAGES_BY_STAGE breaks each coarse stage into its constituent
#   surgical-re-run targets (--only accepts these as well).
STAGE_ORDER: tuple[str, ...] = (
    "ingest",
    "stage_2_parallel",  # { embed ∥ detect_2_1_to_2_4 ∥ pii_discovery }
    "stage_3_parallel",  # { people_register ∥ (scope_prefilter → rerank) }
    "scope_classify",
    "pii_classify",
    "redact",
    "redact_verify",
    "export",
)

# Sub-stage names valid as `--only` targets — the operator-facing
# surgical-re-run granularity. Each maps to its containing coarse stage.
SUB_STAGES_BY_STAGE: dict[str, tuple[str, ...]] = {
    "ingest": ("ingest",),
    "stage_2_parallel": ("embed", "detect_2_1_to_2_4", "pii_discovery"),
    "stage_3_parallel": ("people_register", "scope_prefilter", "rerank"),
    "scope_classify": ("scope_classify",),
    "pii_classify": ("pii_classify",),
    "redact": ("redact",),
    "redact_verify": ("redact_verify",),
    "export": ("export",),
}

# Flat lookup of all valid stage names accepted by --only.
# Includes both the coarse names from STAGE_ORDER and every sub-stage
# from SUB_STAGES_BY_STAGE. Sorted for stable choices=() ordering in argparse.
ALL_STAGE_NAMES: tuple[str, ...] = tuple(
    sorted(set(STAGE_ORDER) | {n for subs in SUB_STAGES_BY_STAGE.values() for n in subs})
)


@dataclass
class StagePlan:
    """The resume plan for a single case run."""

    case_no: str
    stages: list[str] = field(default_factory=list)  # stages to run
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (stage, reason)

    def includes(self, stage: str) -> bool:
        return stage in self.stages

    def includes_any(self, *stages: str) -> bool:
        return any(s in self.stages for s in stages)

    def render(self) -> str:
        """Human-readable resume plan for --check / --dry-run."""
        lines = [f"Case {self.case_no} resume plan:"]
        for s in STAGE_ORDER:
            if s in self.stages:
                lines.append(f"  ✗ {s:<22} → will run")
            else:
                reason = dict(self.skipped).get(s, "out of scope")
                lines.append(f"  ✓ {s:<22} ({reason})")
        return "\n".join(lines)


def build_stage_plan(
    case_path: Path,
    cfg: CaseConfig,
    from_stage: str | None,
    through_stage: str | None,
    only_stage: str | None,
) -> StagePlan:
    """Decide which stages to run based on flags + existing artefacts.

    Today this is intentionally simple: it honours --from/--through/
    --only and the per-phase enabled flags from cfg. The full
    upstream_hash-driven resume cascade lands once the toolkit modules
    exist and produce hashed artefacts; this function picks them up via
    presence + hash checks in a follow-up.
    """
    plan = StagePlan(case_no=cfg.case_no)

    if only_stage is not None:
        if only_stage not in ALL_STAGE_NAMES:
            raise ValueError(f"Unknown stage {only_stage!r}. Valid: {list(ALL_STAGE_NAMES)}")
        plan.stages = [only_stage]
        return plan

    # Default: all stages from `from_stage` (or start) to `through_stage`
    # (or end), filtered by phase-enabled flags.
    start_idx = STAGE_ORDER.index(from_stage) if from_stage else 0
    end_idx = STAGE_ORDER.index(through_stage) + 1 if through_stage else len(STAGE_ORDER)
    candidate = list(STAGE_ORDER[start_idx:end_idx])

    # Per-phase enable flags from cfg.
    for s in list(candidate):
        if s == "pii_classify" and cfg.pii_classify_mode == "off":
            candidate.remove(s)
            plan.skipped.append((s, "PII_CLASSIFY_MODE=off"))
        if s == "redact_verify" and not cfg.redact_verify_enabled:
            candidate.remove(s)
            plan.skipped.append((s, "REDACT_VERIFY_ENABLED=false"))

    plan.stages = candidate
    return plan


# ─────────────────────────────────────────────────────────────────
# Stage helpers — each does a lazy import so the orchestrator can
# install without the toolkit being present.
# ─────────────────────────────────────────────────────────────────


def _lazy_import(module_path: str):
    """Lazy import with a clear error if the toolkit module is absent."""
    try:
        return importlib.import_module(module_path)
    except ImportError as e:
        raise DSARPipelineError(
            f"Required toolkit module {module_path!r} is not installed. "
            f"Install dsar-toolkit (pip install -e ~/projects/dsar-toolkit/) "
            f"or set the corresponding phase to `off`/`false` in case_config.json. "
            f"Original error: {e}"
        ) from e


def _run_ingest(cfg: CaseConfig) -> None:
    ingest = _lazy_import("dsar_pipeline.ingest")
    ingest.run(cfg.case_path)


def _run_embed(cfg: CaseConfig) -> None:
    embed_core = _lazy_import("dsar_embed.core")
    embed_core.embed_corpus(cfg.case_path)


def _run_detect_2_1_to_2_4(cfg: CaseConfig) -> None:
    detect = _lazy_import("dsar_pipeline.detect")
    detect.run_2_1_to_2_4(cfg.case_path)


def _run_pii_discovery(cfg: CaseConfig) -> None:
    if not cfg.discovery_enabled:
        return
    pii_discovery = _lazy_import("dsar_pii_discovery.core")
    pii_discovery.discover_entities(cfg.case_path)


def _run_people_register(cfg: CaseConfig) -> None:
    pr = _lazy_import("dsar_pipeline.people_register")
    pr.run(cfg.case_path)


def _run_scope_filter_chain(cfg: CaseConfig) -> None:
    """Stage 3's chained scope_prefilter → dsar_rerank branch."""
    detect = _lazy_import("dsar_pipeline.detect")
    detect.run_scope_prefilter(cfg.case_path)
    if cfg.rerank_mode != "off":
        rerank_core = _lazy_import("dsar_rerank.core")
        rerank_core.rerank_case(
            cfg.case_path,
            mode=cfg.rerank_mode,
            threshold=cfg.rerank_threshold,
            top_n=cfg.rerank_top_n,
            sample_rate=cfg.rerank_sample_rate,
        )


def _run_scope_classify(cfg: CaseConfig) -> None:
    detect = _lazy_import("dsar_pipeline.detect")
    detect.run_scope_classify(cfg.case_path)


def _run_pii_classify(cfg: CaseConfig) -> None:
    pii_classify_core = _lazy_import("dsar_pii_classifier.core")
    try:
        pii_classify_core.classify_case(
            cfg.case_path,
            mode=cfg.pii_classify_mode,
            subject_identifier=cfg.subject_identifier,
            budget_usd=cfg.pii_budget_usd,
        )
    except Exception as e:
        # Wrap a budget-class error into the orchestrator's typed exception
        # so the caller sees a consistent surface.
        if type(e).__name__ == "PIIBudgetExceeded":
            raise BudgetExceededError(str(e)) from e
        raise


def _run_redact(cfg: CaseConfig) -> None:
    redact = _lazy_import("dsar_pipeline.redact")
    redact.run(
        cfg.case_path,
        prefer_llm_entities=(cfg.pii_classify_mode == "enforce"),
        respect_dispute_halts=True,
    )


def _run_redact_verify(cfg: CaseConfig) -> RunReport | None:
    """Returns a verdict; raises PipelineHalt on any verifier failure."""
    verify_core = _lazy_import("dsar_redact_verify.core")
    verdict = verify_core.verify_case(cfg.case_path)
    if not verdict.all_passed:
        raise PipelineHalt(
            f"case={cfg.case_no} redact-verify failed: "
            f"{verdict.failed_doc_count} doc(s) flagged "
            f"({verdict.failed_verifier_summary}). "
            f"See ~/.dsar-audit/{cfg.case_no}/redact_verify.jsonl. "
            f"Re-run after fixing: dsar-pipeline --case {cfg.case_no} --from redact"
        )
    return None


def _run_export(cfg: CaseConfig) -> None:
    export = _lazy_import("dsar_pipeline.export")
    export.run(cfg.case_path)


# ─────────────────────────────────────────────────────────────────
# The orchestrator entry point
# ─────────────────────────────────────────────────────────────────


def run(
    case_no: str,
    *,
    case_root: Path | None = None,
    from_stage: str | None = None,
    through_stage: str | None = None,
    only_stage: str | None = None,
    dry_run: bool = False,
    check: bool = False,
) -> RunReport:
    """Orchestrate a full DSAR case run.

    See ``docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v2.md``
    § "Full pipeline.run() pseudocode" for the authoritative behaviour.
    """
    cfg = load_case_config(case_no, case_root=case_root)
    validate_phase_4_prereqs(cfg)

    plan = build_stage_plan(cfg.case_path, cfg, from_stage, through_stage, only_stage)

    if check or dry_run:
        # Print the plan; do not run.
        print(plan.render())
        report = RunReport(case_no=case_no)
        report.stages_skipped = list(plan.stages)  # would have run; none actually did
        return report

    audit = PipelineAuditor(case_no)
    for stage, reason in plan.skipped:
        audit.mark_skipped(stage, reason)

    try:
        # Stage 1 — ingest (serial)
        if plan.includes("ingest"):
            with StageBanner(audit, "ingest"):
                _run_ingest(cfg)

        # Stage 2 — parallel: embed ∥ detect-2.1-2.4 ∥ pii-discovery
        if plan.includes_any("stage_2_parallel"):
            with StageBanner(audit, "stage_2_parallel"):
                _run_stage_2_parallel(cfg)

        # Stage 3 — parallel: people_register ∥ scope_filter_chain
        if plan.includes_any("stage_3_parallel"):
            with StageBanner(audit, "stage_3_parallel"):
                _run_stage_3_parallel(cfg)

        # Stage 4 — LLM scope-classify (Sonnet 4.6, semaphore-gated)
        if plan.includes("scope_classify"):
            with StageBanner(audit, "scope_classify"):
                _run_scope_classify(cfg)

        # Stage 5 — LLM PII classifier (Haiku 4.5, Phase 4)
        if plan.includes("pii_classify"):
            with StageBanner(audit, "pii_classify"):
                _run_pii_classify(cfg)

        # Stage 6 — redact
        if plan.includes("redact"):
            with StageBanner(audit, "redact"):
                _run_redact(cfg)

        # Stage 7 — redact-verify (Phase 6, halt-on-fail)
        if plan.includes("redact_verify"):
            with StageBanner(audit, "redact_verify"):
                _run_redact_verify(cfg)

        # Stage 8 — export
        if plan.includes("export"):
            with StageBanner(audit, "export"):
                _run_export(cfg)

    except PipelineHalt as e:
        audit.mark_halted(str(e))
        raise

    return audit.finalise()


def _run_stage_2_parallel(cfg: CaseConfig) -> None:
    """ThreadPoolExecutor fan-out for Stage 2.

    Three branches: embed, detect-2.1-2.4, pii-discovery. Joined with
    FIRST_EXCEPTION semantics — if any branch raises, the others are
    cancelled (best-effort; ThreadPoolExecutor cannot truly cancel
    running tasks but does cancel queued ones) and the exception
    propagates immediately.
    """
    targets = [
        ("embed", _run_embed),
        ("detect_2_1_to_2_4", _run_detect_2_1_to_2_4),
    ]
    if cfg.discovery_enabled:
        targets.append(("pii_discovery", _run_pii_discovery))

    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futures = {name: ex.submit(fn, cfg) for name, fn in targets}
        done, _not_done = wait(futures.values(), return_when=FIRST_EXCEPTION)
        for f in done:
            f.result()  # re-raise if it had an exception


def _run_stage_3_parallel(cfg: CaseConfig) -> None:
    """ThreadPoolExecutor fan-out for Stage 3.

    Two branches: people_register, scope_filter_chain (cosine → rerank).
    """
    targets = [
        ("people_register", _run_people_register),
        ("scope_filter_chain", _run_scope_filter_chain),
    ]
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futures = {name: ex.submit(fn, cfg) for name, fn in targets}
        done, _not_done = wait(futures.values(), return_when=FIRST_EXCEPTION)
        for f in done:
            f.result()
