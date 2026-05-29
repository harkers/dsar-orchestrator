"""The orchestrator's ``run(case)`` entry point.

Implements the DAG from
``docs/superpowers/specs/2026-05-24-pipeline-orchestration-design-v5.md``
§ "Full pipeline.run() pseudocode" — 9 stages with ThreadPoolExecutor
parallelism on Stages 2 + 3, hash-chain verification at stage
boundaries, StageBanner audit emission, and PipelineHalt on Phase 6
verifier failure.

Toolkit imports are **lazy** (inside each stage function). This lets
the orchestrator install + test in isolation; stages only fail when
actually run against a case that needs the missing toolkit module.
"""

from __future__ import annotations

import getpass
import hashlib
import importlib
import json
import os
import socket
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dsar_orchestrator.audit import PipelineAuditor, RunReport, StageBanner
from dsar_orchestrator.config import CaseConfig, load_case_config, validate_phase_4_prereqs
from dsar_orchestrator.exceptions import (
    DSARPipelineError,
    EmptyIngestError,
    ExtractionQualityCatastrophicError,
    PeopleRegisterBuildError,
    PeopleRegisterEmptyError,
    PipelineHalt,
)

# Stage identifiers — used by --from/--through/--only and the resume
# plan. Ordered by data dependency.
#
# Two-level addressing:
#   STAGE_ORDER lists the 10 coarse stages (--from/--through use these
#   because resuming mid-parallel-group is awkward).
#   SUB_STAGES_BY_STAGE breaks each coarse stage into its constituent
#   surgical-re-run targets (--only accepts these as well).
STAGE_ORDER: tuple[str, ...] = (
    "ingest",
    "stage_2_parallel",  # { embed ∥ detect_2_1_to_2_4 }
    "stage_3_parallel",  # { people_register ∥ (scope_prefilter → rerank) }
    "scope_classify",
    "pii_classify",
    "redact",
    "verify_spec",  # NEW in v5.5 (pre-bake plan check)
    "bake",  # NEW in v5.0
    "verify_pdf",
    "export",
)

# Sub-stage names valid as `--only` targets — the operator-facing
# surgical-re-run granularity. Each maps to its containing coarse stage.
SUB_STAGES_BY_STAGE: dict[str, tuple[str, ...]] = {
    "ingest": ("ingest",),
    "stage_2_parallel": ("embed", "detect_2_1_to_2_4"),
    "stage_3_parallel": ("people_register", "scope_prefilter", "rerank"),
    "scope_classify": ("scope_classify",),
    "pii_classify": ("pii_classify",),
    "redact": ("redact",),
    "verify_spec": ("verify_spec",),  # NEW in v5.5
    "bake": ("bake",),  # NEW in v5.0
    "verify_pdf": ("verify_pdf",),
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
        if s == "verify_pdf" and not cfg.redact_verify_enabled:
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
    # ADAPTER for ingest (retires when toolkit ships
    # `dsar_pipeline.ingest.run_for_case(case_path, subject_name)`).
    from dsar_orchestrator.adapters import ingest as ingest_adapter

    ingest_adapter.run_for_case(cfg)
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
    # ADAPTER for detect (retires when toolkit ships
    # `dsar_pipeline.detect.run_for_case(case_path, subject_name)`).
    from dsar_orchestrator.adapters import detect_2_1_to_2_4 as detect_adapter

    detect_adapter.run_for_case(cfg)
    _check_module_work(cfg, "detect_2_1_to_2_4")


def _run_people_register(cfg: CaseConfig) -> None:
    # ADAPTER for people_register (retires when toolkit ships
    # `dsar_pipeline.people_register.run_for_case(case_path)`).
    from dsar_orchestrator.adapters import people_register as people_register_adapter

    people_register_adapter.run_for_case(cfg)
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

    # ADAPTER for rerank (Contract B / issue #11; retires when toolkit
    # ships `dsar_pipeline.rerank.run_for_case(case_path)`). The
    # conductor reads cosine_prefilter.jsonl, calls TEI's bge-reranker-large
    # via dsar_clients.tei_rerank_client.rerank_pairs, writes
    # scope_rerank.jsonl with the cascade's required upstream_hash field.
    if cfg.rerank_mode != "off":
        from dsar_orchestrator.adapters import rerank as rerank_adapter

        rerank_adapter.run_for_case(cfg)
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


def _run_verify_spec(cfg: CaseConfig) -> RunReport | None:
    """Returns None on success; raises PipelineHalt on any verifier failure.

    Pre-bake spec verifier: catches plan-level mistakes before bake
    spends multi-minute work on a doomed redaction plan. Always-on
    (no enable flag) — operators skip via ``--from bake`` or later.
    """
    # ADAPTER for verify_spec (retires when toolkit deprecates the
    # `dsar_pipeline.verify_spec.verify_for_conductor` entry — unlikely
    # since the contract is already locked).
    from dsar_orchestrator.adapters import verify_spec as verify_spec_adapter

    verify_spec_adapter.run_for_case(cfg)
    _check_module_work(cfg, "verify_spec")
    return None


def _run_bake(cfg: CaseConfig) -> None:
    # ADAPTER for bake (retires when toolkit ships
    # `dsar_pipeline.bake.run_for_case(case_path)` — not yet filed).
    # Extracted from the export adapter in v5.0 (rollout B phase 1).
    from dsar_orchestrator.adapters import bake as bake_adapter

    bake_adapter.run_for_case(cfg)
    _check_module_work(cfg, "bake")


def _run_verify_pdf(cfg: CaseConfig) -> RunReport | None:
    """Returns None on success; raises PipelineHalt on any verifier failure."""
    # ADAPTER for verify_pdf (retires when toolkit ships
    # `dsar_pipeline.post_bake_verify.verify_for_conductor(case_path)`).
    from dsar_orchestrator.adapters import verify_pdf as verify_pdf_adapter

    verify_pdf_adapter.run_for_case(cfg)
    _check_module_work(cfg, "verify_pdf")
    return None


def _run_export(cfg: CaseConfig) -> None:
    # ADAPTER for export (retires when toolkit ships
    # `dsar_pipeline.export.run_for_case(case_path)`).
    from dsar_orchestrator.adapters import export as export_adapter

    export_adapter.run_for_case(cfg)
    _check_module_work(cfg, "export")


# ─────────────────────────────────────────────────────────────────
# Phase 5 — fitness pre-flight (spec §4.4 F)
# ─────────────────────────────────────────────────────────────────


def _compute_inference_params_sha256(cfg: CaseConfig) -> str:
    """Canonicalised hash of the inference params that affect Durant
    classification — model alias + truncation cap. Used in the
    fitness-report lookup tuple."""
    params = {
        "model_alias": getattr(cfg, "model_alias", "claude-opus-4-7@anthropic"),
        "max_text_chars": getattr(cfg, "max_text_chars", 32000),
    }
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_matching_report(
    *,
    report_dir: Path,
    deployment_id: str,
    model_alias: str,
    primary_seal: str,
    recheck_seal: str | None,
    live_corpus_sha: str,
    inference_params_sha: str,
    max_age_days: int,
) -> tuple[dict | None, str | None]:
    """Search ``report_dir`` for the most-recent report whose tuple matches.

    Returns ``(report_dict, fail_reason)``. On a clean match:
    ``(report, None)``. On no match: ``(None, "<reason>")``.
    """
    deploy_dir = report_dir / deployment_id
    if not deploy_dir.is_dir():
        return None, f"no reports directory at {deploy_dir}"
    candidates: list[tuple[datetime, dict, Path]] = []
    for rp in sorted(deploy_dir.glob("*.json")):
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if r.get("deployment_id") != deployment_id:
            continue
        if r.get("model_alias") != model_alias:
            continue
        if r.get("primary_prompt_seal_sha256") != primary_seal:
            continue
        if r.get("recheck_prompt_seal_sha256") != recheck_seal:
            continue
        # Older reports may not carry inference_params_sha256; accept
        # missing (None) but enforce match when present.
        if r.get("inference_params_sha256") not in (None, inference_params_sha):
            continue
        try:
            gen_dt = datetime.fromisoformat(r["generated_at"])
        except (KeyError, ValueError):
            continue
        candidates.append((gen_dt, r, rp))

    if not candidates:
        return None, "no fitness report matching tuple"

    candidates.sort(reverse=True, key=lambda t: t[0])
    gen_dt, latest, _ = candidates[0]
    age_days = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 86400.0
    if age_days > max_age_days:
        return None, f"latest report is stale ({age_days:.1f}d > {max_age_days}d)"
    if latest.get("live_corpus_sha256") != live_corpus_sha:
        return None, (
            f"corpus_sha256 drift: report="
            f"{(latest.get('live_corpus_sha256') or '')[:16]}… "
            f"live={live_corpus_sha[:16]}…"
        )
    if not latest.get("passed", False):
        fails = latest.get("fails", [])
        detail = "; ".join(f"{f.get('kind')}: {f.get('code')}" for f in fails) or "unknown"
        return None, f"fitness failed: {detail}"
    return latest, None


def _write_skip_fitness_audit(case_path: Path, *, reason: str, fitness_tuple: dict) -> None:
    """Atomic write of case_audit/skip_fitness.json with the bypass record."""
    audit_dir = case_path / "case_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "reason": reason,
        "os_user": getpass.getuser(),
        "hostname": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fitness_tuple": fitness_tuple,
        "last_known_report_id": None,
    }
    path = audit_dir / "skip_fitness.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _run_fitness_preflight(cfg: CaseConfig, auditor: PipelineAuditor) -> None:
    """Spec §4.4 (F). Pre-flight gate before ``STAGE_ORDER[0]``.

    No-op when ``cfg.fitness_check_enabled`` is False. Halts with
    :class:`PipelineHalt` on missing/stale/failing/drift reports.
    Force-skip via ``cfg.force_skip_fitness_reason`` records an audit
    row + proceeds.
    """
    if not cfg.fitness_check_enabled:
        auditor.note("fitness_preflight", "fitness_check disabled by config")
        return

    with StageBanner(auditor, "fitness_preflight"):
        # Force-skip path: audit + proceed.
        if cfg.force_skip_fitness_reason.strip():
            cfg_raw = json.loads((cfg.case_path / "case_config.json").read_text(encoding="utf-8"))
            deployment_id = cfg_raw.get("fitness_check_deployment_id") or ""
            fitness_tuple = {
                "deployment_id": deployment_id,
                "model_alias": getattr(cfg, "model_alias", "claude-opus-4-7@anthropic"),
            }
            _write_skip_fitness_audit(
                cfg.case_path,
                reason=cfg.force_skip_fitness_reason.strip(),
                fitness_tuple=fitness_tuple,
            )
            auditor.note(
                "fitness_preflight",
                f"force_skip_fitness: {cfg.force_skip_fitness_reason!r}",
            )
            return

        cfg_raw = json.loads((cfg.case_path / "case_config.json").read_text(encoding="utf-8"))
        deployment_id = cfg_raw.get("fitness_check_deployment_id") or ""
        if not deployment_id:
            raise PipelineHalt(
                f"case={cfg.case_no}: fitness_check_enabled but "
                f"`fitness_check_deployment_id` missing in case_config.json. "
                f"Either set it, set fitness_check_enabled=false, "
                f'or pass --force-skip-fitness "<reason>".'
            )

        canary_override = os.environ.get("DSAR_CANARY_PATH_OVERRIDE")
        canary_path = (
            Path(canary_override)
            if canary_override
            else (
                cfg.fitness_check_canary_path
                or Path.home() / ".dsar" / "canary_sets" / deployment_id
            )
        )
        if not canary_path.is_dir():
            raise PipelineHalt(
                f"case={cfg.case_no}: canary set path not found: {canary_path}. "
                f"Run `dsar-fitness-canary --deployment-id {deployment_id}` "
                f"first or pass --auto-fitness."
            )

        # Compute live corpus sha — surface ValueError as a halt.
        from dsar_pipeline.canary_corpus import compute_corpus_sha256

        try:
            live_corpus_sha = compute_corpus_sha256(canary_path)
        except ValueError as e:
            raise PipelineHalt(f"case={cfg.case_no}: canary corpus invalid: {e}") from e

        # Resolve prompt seals.
        try:
            from dsar_pipeline.gates.prompt_loader import PromptLoader

            primary_seal = PromptLoader.load("durant.system").canonical_seal_sha256
            try:
                recheck_seal = PromptLoader.load("durant.recheck.system").canonical_seal_sha256
            except Exception:
                recheck_seal = None
        except ImportError as e:
            raise PipelineHalt(
                f"case={cfg.case_no}: dsar-toolkit not installed for fitness pre-flight: {e}"
            ) from e

        model_alias = getattr(cfg, "model_alias", "claude-opus-4-7@anthropic")
        inference_params_sha = _compute_inference_params_sha256(cfg)

        report_root = Path(
            os.environ.get(
                "DSAR_FITNESS_REPORT_ROOT",
                str(Path.home() / ".dsar" / "fitness_reports"),
            )
        )

        report, fail_reason = _find_matching_report(
            report_dir=report_root,
            deployment_id=deployment_id,
            model_alias=model_alias,
            primary_seal=primary_seal,
            recheck_seal=recheck_seal,
            live_corpus_sha=live_corpus_sha,
            inference_params_sha=inference_params_sha,
            max_age_days=cfg.fitness_check_max_report_age_days,
        )
        if report is None:
            reason = fail_reason or ""
            # Tailor the leading phrase so tests + operators can match precisely.
            if "no fitness report" in reason or "no reports directory" in reason:
                leading = "no fitness report"
            elif "stale" in reason:
                leading = "stale"
            elif "drift" in reason:
                leading = "drift"
            else:
                leading = "fitness failed"
            raise PipelineHalt(
                f"case={cfg.case_no}: fitness pre-flight halt: "
                f"{leading} ({fail_reason}). "
                f"Run `dsar-fitness-canary --deployment-id {deployment_id}`, "
                f"or pass --auto-fitness on the conductor."
            )

        auditor.note(
            "fitness_preflight",
            f"report_id={report.get('report_id')} passed=True age_ok corpus_ok",
        )


def _corpus_has_communicants(case_path: Path) -> bool:
    """Spec §2.1 helper: does the corpus have communicants?

    A 'communicant' is signalled by ANY of:
      - register.json entry with non-null mailbox_owner_email
      - register.json entry with a non-empty source_kind
      - register.json entry filename ending .eml / .msg
      - any .eml / .msg file present under working/ or redacted/
        (catches the case where ingest itself silently failed to record
        the ref — exactly the case-301770 silent-empty class)

    Phase 6 v1 heuristic; used to distinguish 'empty register because
    corpus is genuinely empty' from 'empty register because Phase 1
    build silently failed'."""
    working = case_path / "working"
    register_path = working / "register.json"
    if register_path.exists():
        try:
            refs = json.loads(register_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            refs = None
        if isinstance(refs, list):
            for r in refs:
                if not isinstance(r, dict):
                    continue
                if r.get("mailbox_owner_email"):
                    return True
                sk = r.get("source_kind")
                if isinstance(sk, str) and sk.strip():
                    return True
                fn = r.get("filename") or ""
                if isinstance(fn, str) and fn.lower().endswith((".eml", ".msg")):
                    return True
    # Directory scan fallback — catches ingest-side failures where the
    # register didn't get written / is missing entries.
    for d in (working, case_path / "redacted"):
        if d.exists():
            for p in d.iterdir():
                if p.is_file() and p.name.lower().endswith((".eml", ".msg")):
                    return True
    return False


def _emit_people_register_skip_event(case_path: Path, reason: str) -> None:
    """Write a PEOPLE_REGISTER_GATE_BYPASSED audit event with the operator's
    skip reason."""
    try:
        from dsar_pipeline.audit import AuditEventType, FileAuditStore

        event_type = getattr(AuditEventType, "PEOPLE_REGISTER_GATE_BYPASSED", None)
        if event_type is None:
            event_type = AuditEventType.PEOPLE_REGISTER_BUILT  # closest existing
        store = FileAuditStore(working_dir=case_path / "working")
        store.append_event(
            event_type=event_type,
            payload={
                "reason": reason,
                "bypass_kind": "force_skip_people_register_reason",
            },
            case_id=case_path.name,
            agent="orchestrator",
            stage="people_register_preflight",
        )
    except Exception as exc:
        import sys

        print(
            f"[people_register_preflight] audit emit failed: {exc!r}",
            file=sys.stderr,
        )


def _run_people_register_preflight(cfg: CaseConfig, auditor: PipelineAuditor) -> None:
    """Spec §2.1. Pre-flight gate before redact stage.

    No-op when cfg.people_register_enabled is False. Halts with
    PeopleRegisterBuildError if build_people_register doesn't produce
    a register file. Halts with PeopleRegisterEmptyError if the
    produced register has zero third-party clusters on a corpus that
    HAS communicants (people_register_enabled but build silently empty
    = case 301770 silent-empty bug, which the whole project addresses).

    Force-skip via cfg.force_skip_people_register_reason records an
    audit event + proceeds.
    """
    if not cfg.people_register_enabled:
        auditor.note(
            "people_register_preflight",
            "people_register_enabled=False; preflight skipped by config",
        )
        return

    with StageBanner(auditor, "people_register_preflight"):
        register_path = cfg.case_path / "working" / "people_register.json"

        # Force-skip path
        skip_reason = (cfg.force_skip_people_register_reason or "").strip()
        if skip_reason:
            _emit_people_register_skip_event(cfg.case_path, skip_reason)
            auditor.note(
                "people_register_preflight",
                f"force_skip_people_register: {skip_reason!r}",
            )
            return

        # Auto-build if missing
        if not register_path.exists():
            try:
                from dsar_pipeline.build_people_register import build_people_register
            except ImportError as exc:
                raise PeopleRegisterBuildError(
                    f"case={cfg.case_no}: dsar_pipeline.build_people_register unavailable: {exc!r}"
                ) from exc
            try:
                build_people_register(cfg.case_path)
            except Exception as exc:
                raise PeopleRegisterBuildError(
                    f"case={cfg.case_no}: build_people_register raised: {exc!r}"
                ) from exc
            if not register_path.exists():
                raise PeopleRegisterBuildError(
                    f"case={cfg.case_no}: build_people_register did not "
                    f"produce {register_path}. Source-strategy detection: "
                    f"check ingest output."
                )

        # Parse the register — malformed JSON is a build artifact failure
        # the operator must fix before redact can proceed (deferring to a
        # generic JSONDecodeError trace would crash the pipeline without
        # the structured PeopleRegisterBuildError the conductor expects).
        try:
            register_raw_bytes = register_path.read_bytes()
            register = json.loads(register_raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PeopleRegisterBuildError(
                f"case={cfg.case_no}: people_register.json unreadable / "
                f"malformed: {exc!r}. Re-run dsar-build-people-register "
                f"or hand-fix the file."
            ) from exc

        # Sufficiency check
        register = json.loads(register_path.read_text(encoding="utf-8"))
        third_party_clusters = [c for c in register if not c.get("is_data_subject")]

        if not third_party_clusters and _corpus_has_communicants(cfg.case_path):
            raise PeopleRegisterEmptyError(
                f"case={cfg.case_no}: people_register has zero third-party "
                f"clusters but corpus contains communicants. Likely source-"
                f"strategy misdetection or extraction failure. Run "
                f"`dsar-build-people-register --case {cfg.case_no}` manually "
                f"and inspect the source_strategy detection output."
            )

        # Strategy-specific validation. Narrow except to ImportError +
        # AttributeError ONLY — those are infrastructure-availability
        # conditions (older toolkit install missing source_strategies
        # module or the default_registry symbol). Any OTHER exception
        # (strategy.validate raising on a real validation failure, or a
        # network error from a future remote strategy) MUST propagate so
        # the gate enforces what spec §2.1 says it enforces.
        try:
            from dsar_pipeline.source_strategies import default_registry
        except (ImportError, AttributeError) as exc:
            auditor.note(
                "people_register_preflight",
                f"source_strategies unavailable; skipping strategy validation: {exc!r}",
            )
        else:
            strategy = default_registry().select_strategy(cfg.case_path)
            validation = strategy.validate(register)
            if not validation.valid:
                raise PeopleRegisterBuildError(
                    f"case={cfg.case_no}: strategy={strategy.name} validation "
                    f"failed: {validation.errors}"
                )

        auditor.note(
            "people_register_preflight",
            f"OK: {len(third_party_clusters)} third-party clusters",
        )


# ─────────────────────────────────────────────────────────────────
# Phase 6 Task 3: extraction-quality gate (spec §2.4 R4/R5)
# ─────────────────────────────────────────────────────────────────


def _load_refs_for_quality_check(case_path: Path) -> list[dict]:
    """Read working/register.json; return [] on any error so the caller
    raises EmptyIngestError with a clean message."""
    p = case_path / "working" / "register.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _emit_extraction_quality_warning(case_path: Path, rate: float, refs_total: int) -> None:
    """Write an EXTRACTION_QUALITY_GATE_WARNING audit event. Falls back
    to a generic event type + stderr if the new enum value isn't
    available on the installed toolkit (older deploys)."""
    try:
        from dsar_pipeline.audit import AuditEventType, FileAuditStore

        event_type = getattr(AuditEventType, "EXTRACTION_QUALITY_GATE_WARNING", None)
        if event_type is None:
            event_type = AuditEventType.PEOPLE_REGISTER_BUILT  # closest existing
        store = FileAuditStore(working_dir=case_path / "working")
        store.append_event(
            event_type=event_type,
            payload={
                "ocr_failure_rate": round(rate, 4),
                "refs_total": refs_total,
                "soft_gate_threshold": 0.10,
                "hard_halt_threshold": 0.50,
            },
            case_id=case_path.name,
            agent="orchestrator",
            stage="extraction_quality_gate",
        )
    except (ImportError, AttributeError) as exc:
        import sys

        print(
            f"[extraction_quality_gate] audit emit failed: {exc!r}",
            file=sys.stderr,
        )


def _check_extraction_quality(cfg: CaseConfig, auditor: PipelineAuditor) -> None:
    """Spec §2.4 R4/R5. Soft + hard gate on extraction-quality posture.

    - 0 refs   -> EmptyIngestError (hard halt; nothing to redact)
    - >50% OCR -> ExtractionQualityCatastrophicError (hard halt; operator
                  must triage extraction upstream)
    - >10% OCR -> EXTRACTION_QUALITY_GATE_WARNING audit event (soft gate;
                  operator can review and proceed with reduced set)
    """
    with StageBanner(auditor, "extraction_quality_gate"):
        refs = _load_refs_for_quality_check(cfg.case_path)
        if not refs:
            raise EmptyIngestError(
                f"case={cfg.case_no}: 0 refs ingested. Nothing to redact. "
                f"Check ingest stage output."
            )
        ocr_failures = sum(1 for r in refs if (r.get("text_quality") or "unknown") == "ocr_failure")
        rate = ocr_failures / len(refs)

        if rate > 0.50:
            raise ExtractionQualityCatastrophicError(
                f"case={cfg.case_no}: ocr_failure rate {rate:.1%} > 50%. "
                f"Halt pipeline; operator must triage extraction failures "
                f"upstream before redaction can proceed."
            )

        if rate > 0.10:
            _emit_extraction_quality_warning(cfg.case_path, rate, len(refs))
            auditor.note(
                "extraction_quality_gate",
                f"WARN: ocr_failure rate {rate:.1%} > 10% "
                f"(threshold for hard halt: 50%). Proceeding.",
            )
            return

        auditor.note(
            "extraction_quality_gate",
            f"OK: {len(refs)} refs, ocr_failure {rate:.1%}",
        )


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

    See ``docs/superpowers/specs/2026-05-24-pipeline-orchestration-design-v5.md``
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
        # Phase 5 (spec §4.4): fitness pre-flight. Halts BEFORE any
        # stage runs if the model is not certified fit. Skipped when
        # cfg.fitness_check_enabled is False.
        _run_fitness_preflight(cfg, audit)

        # Phase 6 (spec §2.1): people-register pre-flight. Halts BEFORE
        # any stage runs if the register is missing or empty on a corpus
        # that has communicants. Skipped when
        # cfg.people_register_enabled is False.
        _run_people_register_preflight(cfg, audit)

        # Phase 6 (spec §2.4 R4/R5): extraction-quality gate. Halts on
        # 0 refs or >50% OCR failure; soft-warns on >10%.
        _check_extraction_quality(cfg, audit)

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

        # Stage 7 — verify-spec (v5.5; pre-bake plan check, halt-on-fail)
        if plan.includes("verify_spec"):
            with StageBanner(audit, "verify_spec"):
                _run_verify_spec(cfg)

        # Stage 8 — bake (v5.0; was inside export adapter)
        if plan.includes("bake"):
            with StageBanner(audit, "bake"):
                _run_bake(cfg)

        # Stage 9 — verify-pdf (Phase 6, halt-on-fail)
        if plan.includes("verify_pdf"):
            with StageBanner(audit, "verify_pdf"):
                _run_verify_pdf(cfg)

        # Stage 10 — export
        if plan.includes("export"):
            with StageBanner(audit, "export"):
                _run_export(cfg)

    except PipelineHalt as e:
        audit.mark_halted(str(e))
        raise

    return audit.finalise()


def _run_stage_2_parallel(cfg: CaseConfig) -> None:
    """ThreadPoolExecutor fan-out for Stage 2.

    Two branches: embed, detect-2.1-2.4. Joined with FIRST_EXCEPTION
    semantics — if any branch raises, the others are cancelled
    (best-effort) and the exception propagates immediately.
    """
    targets = [
        ("embed", _run_embed),
        ("detect_2_1_to_2_4", _run_detect_2_1_to_2_4),
    ]

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
