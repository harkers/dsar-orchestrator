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
import json
import os
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dsar_orchestrator.audit import PipelineAuditor, RunReport, StageBanner
from dsar_orchestrator.config import CaseConfig, load_case_config, validate_phase_4_prereqs
from dsar_orchestrator.exceptions import (
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
    *,
    skip_fresh_artefacts: bool = True,
) -> StagePlan:
    """Decide which stages to run based on flags + existing artefacts.

    Three filters apply in order:

    1. ``--only`` short-circuits to a single-stage plan.
    2. ``--from`` / ``--through`` clip the coarse-stage range.
    3. Per-phase enabled flags (``PII_CLASSIFY_MODE=off``,
       ``REDACT_VERIFY_ENABLED=false``) remove stages.
    4. Resume cascade: if ``skip_fresh_artefacts`` (default), every
       sub-stage whose artefact is present AND whose recorded
       ``upstream_hash`` matches the current upstream is skipped.
       Coarse composite stages (``stage_2_parallel``, ``stage_3_parallel``)
       are kept if ANY of their sub-stages need running.

    Pass ``skip_fresh_artefacts=False`` to force a full re-run plan
    (equivalent to the operator passing ``--if-exists overwrite`` on
    every sub-stage's CLI).
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

    # Resume cascade: check freshness of each sub-stage's artefact.
    if skip_fresh_artefacts:
        candidate = _apply_resume_cascade(cfg, candidate, plan)

    plan.stages = candidate
    return plan


def _apply_resume_cascade(cfg: CaseConfig, candidate: list[str], plan: StagePlan) -> list[str]:
    """Walk the candidate coarse stages; for each, check the freshness
    of its sub-stages via the artefact registry. If ALL sub-stages are
    fresh, the coarse stage is skipped + recorded; otherwise the coarse
    stage is kept (the run() function will resolve which sub-stages
    actually execute).

    Once any stage is added to the plan, all downstream stages are
    automatically included — a fresh artefact downstream of a stale
    one is meaningless, since re-running upstream will invalidate it.
    """
    # Local import to keep stages.py a leaf at the layering level.
    from dsar_orchestrator.stages import STAGE_ARTEFACTS, is_artefact_fresh

    result: list[str] = []
    downstream_forced = False

    for coarse_stage in candidate:
        if downstream_forced:
            result.append(coarse_stage)
            continue

        sub_stages = SUB_STAGES_BY_STAGE.get(coarse_stage, (coarse_stage,))
        any_stale = False
        for sub in sub_stages:
            art = STAGE_ARTEFACTS.get(sub)
            if art is None:
                # Unregistered sub-stage — treat as always-run.
                any_stale = True
                break
            fresh, _reason = is_artefact_fresh(cfg, art)
            if not fresh:
                any_stale = True
                break

        if any_stale:
            result.append(coarse_stage)
            downstream_forced = True
        else:
            plan.skipped.append((coarse_stage, "all sub-stage artefacts fresh"))

    return result


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


def _check_module_work(cfg: CaseConfig, sub_stage: str) -> None:
    """Invoke the orchestrator-side agent for ``sub_stage`` to validate
    the work that just finished.

    Agents live in ``dsar_orchestrator.module_agents`` (brought home
    2026-05-22 so validation versions with the orchestrator rather
    than waiting on toolkit releases). Each ``check_<sub_stage>(cfg)``
    function returns a ``ModuleCheckResult`` with severity in
    ``{info, warning, critical}``.

    - ``ok=True`` (any severity) → record an audit row, continue.
    - ``ok=False`` + ``severity=critical`` → record + ``PipelineHalt``.
    - ``ok=False`` + warning → record, continue (log analyser may
      escalate downstream).

    Audit rows append to ``~/.dsar-audit/<case>/module_checks.jsonl``.
    """
    # Local import to keep module_agents a leaf at the layering level.
    from dsar_orchestrator import __version__
    from dsar_orchestrator.module_agents import check_work

    result = check_work(cfg, sub_stage)

    audit_path = Path.home() / ".dsar-audit" / cfg.case_no / "module_checks.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "sub_stage": sub_stage,
        "ok": result.ok,
        "severity": result.severity,
        "findings": list(result.findings),
        "recommendation": result.recommendation,
        "schema_version": "1.0",
        "producer_version": f"dsar_orchestrator {__version__}",
    }
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())

    if not result.ok and result.severity == "critical":
        raise PipelineHalt(
            f"case={cfg.case_no}: module agent for {sub_stage!r} "
            f"flagged a critical issue: {result.findings!r}. "
            f"Recommendation: {result.recommendation or '(none)'}. "
            f"Full log: {audit_path}."
        )


def _run_ingest(cfg: CaseConfig) -> None:
    ingest = _lazy_import("dsar_pipeline.ingest")
    ingest.run(cfg.case_path)
    _check_module_work(cfg, "ingest")


def _run_embed(cfg: CaseConfig) -> None:
    # ADAPTER (retires when toolkit ships `dsar_embed.core.embed_corpus`
    # per https://github.com/harkers/dsar-toolkit/issues/1):
    # The conductor handles the corpus-level embed flow itself, using
    # `dsar_clients.tei_embed_client.embed()` as the HTTP leaf.
    # Output JSONL shape matches what the eventual toolkit module will
    # produce, so the resume cascade is unaffected on retirement.
    from dsar_orchestrator.adapters import embed as embed_adapter

    embed_adapter.run_for_case(cfg)
    _check_module_work(cfg, "embed")


def _run_detect_2_1_to_2_4(cfg: CaseConfig) -> None:
    detect = _lazy_import("dsar_pipeline.detect")
    detect.run_2_1_to_2_4(cfg.case_path)
    _check_module_work(cfg, "detect_2_1_to_2_4")


def _run_pii_discovery(cfg: CaseConfig) -> None:
    if not cfg.discovery_enabled:
        return
    pii_discovery = _lazy_import("dsar_pii_discovery.core")
    pii_discovery.discover_entities(cfg.case_path)
    _check_module_work(cfg, "pii_discovery")


def _run_people_register(cfg: CaseConfig) -> None:
    pr = _lazy_import("dsar_pipeline.people_register")
    pr.run(cfg.case_path)
    _check_module_work(cfg, "people_register")


def _run_scope_filter_chain(cfg: CaseConfig) -> None:
    """Stage 3's chained scope_prefilter → dsar_rerank branch."""
    # ADAPTER for scope_prefilter (retires when the toolkit ships
    # `dsar_pipeline.scope_prefilter.run_for_case(case_path)` —
    # see harkers/dsar-toolkit#1 reply with the prioritised adapter
    # list). The conductor reads embeddings.jsonl, embeds the case
    # scope via tei_embed_client, runs the cosine prefilter math
    # itself, writes cosine_prefilter.jsonl with the cascade's
    # required upstream_hash field.
    from dsar_orchestrator.adapters import scope_prefilter as scope_prefilter_adapter

    scope_prefilter_adapter.run_for_case(cfg)
    _check_module_work(cfg, "scope_prefilter")

    # Rerank still goes through the toolkit-stub-or-lazy-import path.
    # Awaiting the resolution of toolkit issue #4 (is `dsar_rerank` a
    # standalone module or does it live inside `dsar_pii_classifier`?)
    # before writing a conductor-side rerank adapter.
    if cfg.rerank_mode != "off":
        rerank_core = _lazy_import("dsar_rerank.core")
        rerank_core.rerank_case(
            cfg.case_path,
            mode=cfg.rerank_mode,
            threshold=cfg.rerank_threshold,
            top_n=cfg.rerank_top_n,
            sample_rate=cfg.rerank_sample_rate,
        )
        _check_module_work(cfg, "rerank")


def _run_scope_classify(cfg: CaseConfig) -> None:
    # ADAPTER for scope_classify (retires when toolkit ships
    # `dsar_pipeline.scope_check_stage.run_for_case(case_path)` —
    # the heavy `ScopeCheckStage` class isn't a clean adapter target
    # so the bridge shells out to the `dsar-scope-check` CLI).
    from dsar_orchestrator.adapters import scope_classify as scope_classify_adapter

    scope_classify_adapter.run_for_case(cfg)
    _check_module_work(cfg, "scope_classify")


def _run_pii_classify(cfg: CaseConfig) -> None:
    # ADAPTER for pii_classify (retires when toolkit ships a thin
    # `dsar_pii_classifier.core.classify_case_for_conductor(case_path)`
    # entry that writes working/pii_collection.jsonl directly).
    # Current adapter calls discover_case + aggregates per-stage
    # findings into the per-ref shape the cascade expects.
    from dsar_orchestrator.adapters import pii_classify as pii_classify_adapter

    pii_classify_adapter.run_for_case(cfg)
    _check_module_work(cfg, "pii_classify")


def _run_redact(cfg: CaseConfig) -> None:
    # ADAPTER for redact (retires when toolkit ships
    # `dsar_pipeline.redact_stage.run_for_case(case_path)`).
    # Toolkit's redact stage builds working/redaction_input.jsonl —
    # the canonical spec of what to redact. Actual file output
    # (redacted PDFs) happens in the export adapter's bake step.
    from dsar_orchestrator.adapters import redact as redact_adapter

    redact_adapter.run_for_case(cfg)
    _check_module_work(cfg, "redact")


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
            f"Re-run after fixing: dsar-conductor --case {cfg.case_no} --from redact"
        )
    _check_module_work(cfg, "redact_verify")
    return None


def _run_export(cfg: CaseConfig) -> None:
    export = _lazy_import("dsar_pipeline.export")
    export.run(cfg.case_path)
    _check_module_work(cfg, "export")


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
    force: bool = False,
    acknowledge_issues: bool = False,
) -> RunReport:
    """Orchestrate a full DSAR case run.

    See ``docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v2.md``
    § "Full pipeline.run() pseudocode" for the authoritative behaviour.

    ``force`` disables the resume cascade — every in-scope stage runs
    regardless of artefact freshness.

    ``acknowledge_issues`` clears any analyser block flag from a
    previous ``dsar-analyse-logs`` run and proceeds. Operator-facing;
    the orchestrator refuses to start when a block is present
    otherwise.
    """
    cfg = load_case_config(case_no, case_root=case_root)
    validate_phase_4_prereqs(cfg)

    # Analyser block gate — only checked for real runs (not --check).
    if not (check or dry_run):
        from dsar_orchestrator.log_analyser.core import clear_block, is_blocked

        if is_blocked(case_no):
            if acknowledge_issues:
                clear_block(case_no)
            else:
                raise DSARPipelineError(
                    f"case={case_no} is under an analyser block. "
                    f"Inspect ~/.dsar-audit/{case_no}/analysis.md and either:\n"
                    f"  - fix the critical findings, then "
                    f"`dsar-analyse-logs --case {case_no}` (clean run "
                    f"removes the block automatically), or\n"
                    f"  - `dsar-conductor --case {case_no} "
                    f"--acknowledge-issues` to proceed anyway."
                )

    plan = build_stage_plan(
        cfg.case_path,
        cfg,
        from_stage,
        through_stage,
        only_stage,
        skip_fresh_artefacts=not force,
    )

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
