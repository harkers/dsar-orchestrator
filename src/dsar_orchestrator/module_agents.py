"""Per-sub-stage validation agents.

Each agent reads the artefact(s) a sub-stage just produced and
validates them — presence, JSON shape, required fields, range
constraints, upstream_hash invariants. Agents are CHEAP +
DETERMINISTIC; they run inside the orchestrator process, not via a
toolkit lazy-import.

Brought into the orchestrator (was: `dsar_pipeline.module_agents.*`
in the toolkit) so the validation contract versions with the
orchestrator rather than waiting on a toolkit release.

Each agent function:

    def check_<sub_stage>(cfg: CaseConfig) -> ModuleCheckResult

The orchestrator's ``_check_module_work`` calls
``check_work(cfg.case_path, sub_stage, cfg)`` after every stage; on
``severity=critical`` it raises ``PipelineHalt``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from dsar_orchestrator.config import CaseConfig


@dataclass
class ModuleCheckResult:
    """Outcome of one agent's validation pass."""

    ok: bool
    severity: str  # "info" | "warning" | "critical"
    findings: list[str] = field(default_factory=list)
    recommendation: str = ""


# ─── helpers ────────────────────────────────────────────────────────


def _load_jsonl(path: Path) -> list[dict]:
    """Load a JSONL file or return [] if absent. Caller validates
    presence + non-emptiness separately."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _all_have(rows: list[dict], required: tuple[str, ...]) -> list[str]:
    """Return a list of human-readable findings for rows missing any
    required field. Empty list means everything's fine."""
    findings: list[str] = []
    for i, row in enumerate(rows):
        missing = [k for k in required if k not in row]
        if missing:
            findings.append(
                f"row {i} missing required fields: {missing}; ref={row.get('ref', '?')}"
            )
            if len(findings) >= 5:
                break
    return findings


def _critical(findings: list[str], recommendation: str = "") -> ModuleCheckResult:
    return ModuleCheckResult(
        ok=False, severity="critical", findings=findings, recommendation=recommendation
    )


def _warning(findings: list[str], recommendation: str = "") -> ModuleCheckResult:
    return ModuleCheckResult(
        ok=False, severity="warning", findings=findings, recommendation=recommendation
    )


def _ok(findings: list[str] | None = None) -> ModuleCheckResult:
    return ModuleCheckResult(ok=True, severity="info", findings=findings or [], recommendation="")


def _rerun_hint(sub_stage: str, case_no: str) -> str:
    return f"Re-run with: dsar-conductor --case {case_no} --only {sub_stage} --force"


# ─── ingest ─────────────────────────────────────────────────────────


def check_ingest(cfg: CaseConfig) -> ModuleCheckResult:
    register_path = cfg.case_path / "working" / "register.json"
    if not register_path.exists():
        return _critical(
            [f"working/register.json missing at {register_path}"],
            _rerun_hint("ingest", cfg.case_no),
        )
    try:
        register = json.loads(register_path.read_text())
    except json.JSONDecodeError as e:
        return _critical(
            [f"register.json is not valid JSON: {e}"],
            _rerun_hint("ingest", cfg.case_no),
        )
    refs = register.get("refs", [])
    if not refs:
        return _critical(
            ["register.json has no refs"],
            "Confirm source/ has documents; " + _rerun_hint("ingest", cfg.case_no),
        )
    if "upstream_hash" not in register:
        return _warning(
            ["register.json missing upstream_hash field"],
            "Resume cascade won't work for this case until ingest writes "
            "the hash. " + _rerun_hint("ingest", cfg.case_no),
        )
    missing: list[str] = []
    for entry in refs:
        text_path = cfg.case_path / entry.get("text_path", "")
        if not text_path.exists():
            missing.append(f"text_path missing: {text_path}")
        if len(missing) >= 5:
            break
    if missing:
        return _critical(
            missing,
            "Some refs point at missing files; inspect source/ + register.json",
        )
    return _ok([f"register.json valid: {len(refs)} refs"])


# ─── embed ──────────────────────────────────────────────────────────


def check_embed(cfg: CaseConfig) -> ModuleCheckResult:
    path = cfg.case_path / "working" / "embeddings.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        return _critical(
            [f"embeddings.jsonl missing or empty at {path}"],
            _rerun_hint("embed", cfg.case_no),
        )
    missing = _all_have(rows, ("ref", "embedding", "upstream_hash"))
    if missing:
        return _critical(missing, _rerun_hint("embed", cfg.case_no))
    # Dimensional + sanity checks on a sample
    sample_size = min(5, len(rows))
    dim_findings: list[str] = []
    for row in rows[:sample_size]:
        emb = row.get("embedding", [])
        if not isinstance(emb, list) or not emb:
            dim_findings.append(f"ref={row.get('ref')} has non-list/empty embedding")
        elif len(emb) != 1024:
            dim_findings.append(f"ref={row.get('ref')} embedding dim={len(emb)} (expected 1024)")
        elif any(not isinstance(v, (int, float)) or math.isnan(v) for v in emb[:64]):
            dim_findings.append(
                f"ref={row.get('ref')} embedding contains non-numeric or NaN values"
            )
    if dim_findings:
        return _critical(dim_findings, _rerun_hint("embed", cfg.case_no))
    # All upstream_hash values should match
    hashes = {row.get("upstream_hash") for row in rows}
    if len(hashes) > 1:
        return _warning(
            [f"embeddings.jsonl has {len(hashes)} distinct upstream_hash values"],
            "Rows in the same artefact should share one upstream_hash. "
            + _rerun_hint("embed", cfg.case_no),
        )
    return _ok([f"embeddings.jsonl: {len(rows)} refs at 1024 dim"])


# ─── detect_2_1_to_2_4 ──────────────────────────────────────────────


def check_detect_2_1_to_2_4(cfg: CaseConfig) -> ModuleCheckResult:
    path = cfg.case_path / "working" / "detect_entities.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        return _critical(
            [f"detect_entities.jsonl missing or empty at {path}"],
            _rerun_hint("detect_2_1_to_2_4", cfg.case_no),
        )
    missing = _all_have(rows, ("ref", "entities", "upstream_hash"))
    if missing:
        return _critical(missing, _rerun_hint("detect_2_1_to_2_4", cfg.case_no))
    # Match against register refs
    register = _load_register(cfg)
    if register:
        register_refs = {entry["ref"] for entry in register.get("refs", [])}
        detect_refs = {row.get("ref") for row in rows}
        missing_refs = register_refs - detect_refs
        if missing_refs:
            return _critical(
                [f"detect rows missing {len(missing_refs)} register refs"],
                _rerun_hint("detect_2_1_to_2_4", cfg.case_no),
            )
    return _ok([f"detect_entities.jsonl: {len(rows)} rows"])


# ─── pii_discovery ──────────────────────────────────────────────────


def check_pii_discovery(cfg: CaseConfig) -> ModuleCheckResult:
    if not cfg.discovery_enabled:
        return _ok(["DISCOVERY_ENABLED=false; skipping"])
    path = cfg.case_path / "working" / "pii_discovery.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        return _critical(
            [f"pii_discovery.jsonl missing or empty at {path}"],
            _rerun_hint("pii_discovery", cfg.case_no),
        )
    missing = _all_have(rows, ("ref", "entities", "upstream_hash"))
    if missing:
        return _critical(missing, _rerun_hint("pii_discovery", cfg.case_no))
    return _ok([f"pii_discovery.jsonl: {len(rows)} rows"])


# ─── people_register ────────────────────────────────────────────────


def check_people_register(cfg: CaseConfig) -> ModuleCheckResult:
    path = cfg.case_path / "working" / "person_index.json"
    if not path.exists():
        return _critical(
            [f"person_index.json missing at {path}"],
            _rerun_hint("people_register", cfg.case_no),
        )
    try:
        obj = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return _critical(
            [f"person_index.json is not valid JSON: {e}"],
            _rerun_hint("people_register", cfg.case_no),
        )
    if "clusters" not in obj:
        return _critical(
            ["person_index.json missing required field: clusters"],
            _rerun_hint("people_register", cfg.case_no),
        )
    if "upstream_hash" not in obj:
        return _warning(
            ["person_index.json missing upstream_hash field"],
            _rerun_hint("people_register", cfg.case_no),
        )
    return _ok([f"person_index.json: {len(obj.get('clusters', []))} clusters"])


# ─── scope_prefilter ────────────────────────────────────────────────


def check_scope_prefilter(cfg: CaseConfig) -> ModuleCheckResult:
    path = cfg.case_path / "working" / "cosine_prefilter.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        return _critical(
            [f"cosine_prefilter.jsonl missing or empty at {path}"],
            _rerun_hint("scope_prefilter", cfg.case_no),
        )
    missing = _all_have(rows, ("ref", "cosine_score", "passes", "upstream_hash"))
    if missing:
        return _critical(missing, _rerun_hint("scope_prefilter", cfg.case_no))
    # Range check on cosine scores
    out_of_range = [
        row.get("ref")
        for row in rows
        if not isinstance(row.get("cosine_score"), (int, float))
        or not -1.001 <= row["cosine_score"] <= 1.001
    ]
    if out_of_range:
        return _critical(
            [f"cosine_score out of [-1, 1] for refs: {out_of_range[:5]}"],
            _rerun_hint("scope_prefilter", cfg.case_no),
        )
    return _ok([f"cosine_prefilter.jsonl: {len(rows)} rows scored"])


# ─── rerank ─────────────────────────────────────────────────────────


def check_rerank(cfg: CaseConfig) -> ModuleCheckResult:
    if cfg.rerank_mode == "off":
        return _ok(["RERANK_MODE=off; skipping"])
    path = cfg.case_path / "working" / "scope_rerank.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        return _critical(
            [f"scope_rerank.jsonl missing or empty at {path}"],
            _rerun_hint("rerank", cfg.case_no),
        )
    missing = _all_have(rows, ("ref", "rerank_score", "would_drop", "mode", "upstream_hash"))
    if missing:
        return _critical(missing, _rerun_hint("rerank", cfg.case_no))
    # Mode consistency check
    modes = {row.get("mode") for row in rows}
    if cfg.rerank_mode not in modes:
        return _warning(
            [f"rerank rows have modes={modes} but cfg.rerank_mode={cfg.rerank_mode}"],
            "Recorded mode in scope_rerank.jsonl should match the run-time mode. "
            + _rerun_hint("rerank", cfg.case_no),
        )
    # In SHADOW mode, no row should have would_drop=true affecting outcome.
    # In ENFORCE mode, would_drop can be true (the row gets dropped from
    # the downstream input set).
    return _ok([f"scope_rerank.jsonl: {len(rows)} rows in mode={cfg.rerank_mode}"])


# ─── scope_classify ─────────────────────────────────────────────────


def check_scope_classify(cfg: CaseConfig) -> ModuleCheckResult:
    """Validate scope_classify stage outputs.

    The toolkit's scope_check_stage writes ``scope_verdicts.jsonl``
    (per-ref scope determination: present / not_present / ambiguous).
    The conductor's adapter writes ``scope_classify_complete.jsonl``
    as the cascade anchor on top of it.

    Per-ref `_tags.json` files used to be listed here, but those are
    actually produced by pii_identification_stage downstream, not
    scope_check. The agent now validates the verdicts file + the
    cascade anchor only.
    """
    complete_path = cfg.case_path / "working" / "scope_classify_complete.jsonl"
    if not complete_path.exists():
        return _critical(
            [f"scope_classify_complete.jsonl missing at {complete_path}"],
            _rerun_hint("scope_classify", cfg.case_no),
        )
    rows = _load_jsonl(complete_path)
    if not rows:
        return _critical(
            ["scope_classify_complete.jsonl is empty"],
            _rerun_hint("scope_classify", cfg.case_no),
        )
    if "upstream_hash" not in rows[0]:
        return _warning(
            ["scope_classify_complete.jsonl first row missing upstream_hash"],
            _rerun_hint("scope_classify", cfg.case_no),
        )
    # The toolkit writes scope_verdicts.jsonl as the per-ref source of truth.
    verdicts_path = cfg.case_path / "working" / "scope_verdicts.jsonl"
    if not verdicts_path.exists():
        return _critical(
            [f"scope_verdicts.jsonl missing at {verdicts_path}"],
            _rerun_hint("scope_classify", cfg.case_no),
        )
    verdict_rows = _load_jsonl(verdicts_path)
    if not verdict_rows:
        return _warning(
            ["scope_verdicts.jsonl is empty"],
            _rerun_hint("scope_classify", cfg.case_no),
        )
    # Cross-check: every register ref should have a verdict.
    register = _load_register(cfg)
    if register:
        register_refs = {entry["ref"] for entry in register.get("refs", [])}
        verdict_refs = {row.get("ref") for row in verdict_rows}
        missing = register_refs - verdict_refs
        if missing:
            sample = sorted(missing)[:5]
            return _critical(
                [
                    f"scope_verdicts.jsonl missing verdicts for "
                    f"{len(missing)} refs (sample: {sample})"
                ],
                _rerun_hint("scope_classify", cfg.case_no),
            )
    return _ok([f"scope_classify complete: {len(verdict_rows)} verdicts"])


# ─── pii_classify ───────────────────────────────────────────────────


VALID_RECHECK_VERDICTS = {"confirmed", "disputed", "uncertain"}


def check_pii_classify(cfg: CaseConfig) -> ModuleCheckResult:
    if cfg.pii_classify_mode == "off":
        return _ok(["PII_CLASSIFY_MODE=off; skipping"])
    path = cfg.case_path / "working" / "pii_collection.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        return _critical(
            [f"pii_collection.jsonl missing or empty at {path}"],
            _rerun_hint("pii_classify", cfg.case_no),
        )
    missing = _all_have(rows, ("ref", "in_scope_recheck", "entities", "upstream_hash"))
    if missing:
        return _critical(missing, _rerun_hint("pii_classify", cfg.case_no))
    # Recheck verdict must be in the allowed set
    bad_verdicts: list[str] = []
    for row in rows:
        v = row.get("in_scope_recheck")
        if v not in VALID_RECHECK_VERDICTS:
            bad_verdicts.append(f"ref={row.get('ref')} in_scope_recheck={v!r}")
        if len(bad_verdicts) >= 5:
            break
    if bad_verdicts:
        return _critical(
            bad_verdicts + [f"Allowed: {sorted(VALID_RECHECK_VERDICTS)}"],
            _rerun_hint("pii_classify", cfg.case_no),
        )
    return _ok([f"pii_collection.jsonl: {len(rows)} refs"])


# ─── redact ─────────────────────────────────────────────────────────


def check_redact(cfg: CaseConfig) -> ModuleCheckResult:
    complete_path = cfg.case_path / "working" / "redact_complete.json"
    if not complete_path.exists():
        return _critical(
            [f"redact_complete.json missing at {complete_path}"],
            _rerun_hint("redact", cfg.case_no),
        )
    try:
        obj = json.loads(complete_path.read_text())
    except json.JSONDecodeError as e:
        return _critical(
            [f"redact_complete.json invalid JSON: {e}"],
            _rerun_hint("redact", cfg.case_no),
        )
    if "upstream_hash" not in obj:
        return _warning(
            ["redact_complete.json missing upstream_hash field"],
            _rerun_hint("redact", cfg.case_no),
        )
    # redacted/ must contain at least one file per register ref
    register = _load_register(cfg)
    redacted_dir = cfg.case_path / "redacted"
    if not redacted_dir.exists():
        return _critical(
            [f"redacted/ directory missing at {redacted_dir}"],
            _rerun_hint("redact", cfg.case_no),
        )
    redacted_files = list(redacted_dir.iterdir())
    if not redacted_files:
        return _critical(
            ["redacted/ directory is empty"],
            _rerun_hint("redact", cfg.case_no),
        )
    if register:
        expected = len(register.get("refs", []))
        if len(redacted_files) < expected:
            return _warning(
                [f"redacted/ has {len(redacted_files)} files; register has {expected} refs"],
                "Some refs may have been skipped (e.g., dispute halts). "
                "Cross-check with scope_recheck.jsonl.",
            )
    return _ok([f"redact: {len(redacted_files)} files in redacted/"])


# ─── redact_verify ──────────────────────────────────────────────────


def check_redact_verify(cfg: CaseConfig) -> ModuleCheckResult:
    if not cfg.redact_verify_enabled:
        return _ok(["REDACT_VERIFY_ENABLED=false; skipping"])
    audit_path = Path.home() / ".dsar-audit" / cfg.case_no / "redact_verify.jsonl"
    rows = _load_jsonl(audit_path)
    if not rows:
        return _critical(
            [f"redact_verify.jsonl missing or empty at {audit_path}"],
            _rerun_hint("redact_verify", cfg.case_no),
        )
    # Any row with passed=false should have caused a halt already; if
    # we see one here without a halt, the toolkit module is misbehaving.
    failures = [row for row in rows if row.get("passed") is False]
    if failures:
        return _critical(
            [
                f"{len(failures)} verifier failure(s) recorded but pipeline "
                f"continued — toolkit module may not be raising halt"
            ],
            "Check dsar_redact_verify implementation; " + _rerun_hint("redact_verify", cfg.case_no),
        )
    return _ok([f"redact_verify: {len(rows)} entries, all passed"])


# ─── export ─────────────────────────────────────────────────────────


def check_export(cfg: CaseConfig) -> ModuleCheckResult:
    manifest_path = cfg.case_path / "output" / "manifest.json"
    if not manifest_path.exists():
        return _critical(
            [f"output/manifest.json missing at {manifest_path}"],
            _rerun_hint("export", cfg.case_no),
        )
    try:
        obj = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return _critical(
            [f"output/manifest.json invalid JSON: {e}"],
            _rerun_hint("export", cfg.case_no),
        )
    if "upstream_hash" not in obj:
        return _warning(
            ["output/manifest.json missing upstream_hash field"],
            _rerun_hint("export", cfg.case_no),
        )
    output_dir = cfg.case_path / "output"
    output_files = [p for p in output_dir.iterdir() if p.suffix in (".pdf", ".PDF")]
    if not output_files:
        return _critical(
            ["output/ contains no PDF files"],
            _rerun_hint("export", cfg.case_no),
        )
    return _ok([f"export: {len(output_files)} PDFs in output/"])


# ─── shared helper ─────────────────────────────────────────────────


def _load_register(cfg: CaseConfig) -> dict | None:
    """Best-effort load of working/register.json; returns None if absent
    or invalid (downstream agents fall back to non-cross-checked
    validation in that case)."""
    p = cfg.case_path / "working" / "register.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


# ─── dispatch ───────────────────────────────────────────────────────


CHECKERS: dict[str, callable] = {
    "ingest": check_ingest,
    "embed": check_embed,
    "detect_2_1_to_2_4": check_detect_2_1_to_2_4,
    "pii_discovery": check_pii_discovery,
    "people_register": check_people_register,
    "scope_prefilter": check_scope_prefilter,
    "rerank": check_rerank,
    "scope_classify": check_scope_classify,
    "pii_classify": check_pii_classify,
    "redact": check_redact,
    "redact_verify": check_redact_verify,
    "export": check_export,
}


def check_work(cfg: CaseConfig, sub_stage: str) -> ModuleCheckResult:
    """Look up and invoke the agent for a sub-stage.

    Raises a ``ModuleCheckResult`` with severity=critical if the
    sub-stage has no agent — this should never happen at runtime
    because the orchestrator's STAGE_ORDER + SUB_STAGES_BY_STAGE
    matches the CHECKERS keys, but defending against drift is cheap.
    """
    fn = CHECKERS.get(sub_stage)
    if fn is None:
        return _critical(
            [f"No agent registered for sub_stage={sub_stage!r}"],
            "This is an orchestrator bug — CHECKERS is out of sync with "
            "STAGE_ORDER. File against dsar-orchestrator.",
        )
    return fn(cfg)
