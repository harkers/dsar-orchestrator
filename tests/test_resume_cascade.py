"""Tests for the resume cascade — build_stage_plan's freshness logic."""

from __future__ import annotations

import json
from pathlib import Path

from dsar_orchestrator.config import load_case_config
from dsar_orchestrator.hash_chain import compute_register_hash, hash_pairs, sha256_file
from dsar_orchestrator.pipeline import build_stage_plan


def _seed_minimum_case(tmp_path: Path, case_no: str = "300100") -> Path:
    """Write a minimum case dir with source/ + working/ + case_config.json
    so the cascade has something to chew on."""
    case_path = tmp_path / case_no
    case_path.mkdir()

    src = case_path / "source"
    src.mkdir()
    (src / "doc1.txt").write_text("hello world")
    (src / "doc2.txt").write_text("more text")

    working = case_path / "working"
    working.mkdir()
    (case_path / "redacted").mkdir()
    (case_path / "output").mkdir()

    # Seed register.json (the ingest output) with an upstream_hash that
    # matches the source tree.
    src_pairs = []
    for p in sorted(src.rglob("*")):
        if p.is_file():
            src_pairs.append((str(p.relative_to(src)), sha256_file(p)))
    src_hash = hash_pairs(src_pairs)

    # Per Contract A (issue #8): register is a flat list; conductor
    # metadata (upstream_hash) lives in working/register_meta.json.
    ref1 = f"{case_no}-0001"
    ref2 = f"{case_no}-0002"
    register = [
        {"ref": ref1, "filename": "doc1.txt", "path": str(src / "doc1.txt")},
        {"ref": ref2, "filename": "doc2.txt", "path": str(src / "doc2.txt")},
    ]
    (working / "register.json").write_text(json.dumps(register))
    # Extracted text per ref at working/<ref>.txt (toolkit convention)
    (working / f"{ref1}.txt").write_text("hello world", encoding="utf-8")
    (working / f"{ref2}.txt").write_text("another doc", encoding="utf-8")
    (working / "register_meta.json").write_text(
        json.dumps({"upstream_hash": src_hash, "schema_version": "1.0"})
    )

    config = {
        "case_no": case_no,
        "case_scope": "test scope",
        "subject_identifier": {
            "primary_name": "Test Subject",
            "disambiguation_notes": "for testing",
        },
        "rerank_mode": "shadow",
        "pii_classify_mode": "shadow",
    }
    (case_path / "case_config.json").write_text(json.dumps(config))
    return case_path


def _write_fresh_embeddings(case_path: Path) -> None:
    """Write an embeddings.jsonl with the CORRECT upstream_hash so
    `embed` is considered fresh."""
    register_path = case_path / "working" / "register.json"
    upstream = compute_register_hash(register_path)
    embed_path = case_path / "working" / "embeddings.jsonl"
    row = {"ref": "x", "embedding": [0.1, 0.2], "upstream_hash": upstream}
    embed_path.write_text(json.dumps(row) + "\n")


def _write_stale_embeddings(case_path: Path) -> None:
    """Write an embeddings.jsonl with a WRONG upstream_hash so embed
    is stale."""
    embed_path = case_path / "working" / "embeddings.jsonl"
    embed_path.write_text(json.dumps({"upstream_hash": "stale"}) + "\n")


# ─── cascade: no artefacts → run everything ─────────────────────────


def test_cascade_runs_all_stages_when_no_artefacts(tmp_path: Path) -> None:
    case_path = _seed_minimum_case(tmp_path)
    # Remove the conductor-meta sidecar so even ingest is stale (the
    # cascade anchor for ingest is register_meta.json post-issue-#8).
    (case_path / "working" / "register_meta.json").unlink()

    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, None, None, None)

    # All 8 coarse stages should be in the plan
    assert "ingest" in plan.stages
    assert "stage_2_parallel" in plan.stages
    assert "export" in plan.stages


# ─── cascade: skip when fresh ───────────────────────────────────────


def test_cascade_skips_ingest_when_register_fresh(tmp_path: Path) -> None:
    """register.json with a valid source-tree hash → ingest is fresh.
    But downstream stages have no artefacts → still run."""
    case_path = _seed_minimum_case(tmp_path)
    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, None, None, None)

    # ingest gets skipped (register.json present + fresh)
    skipped_stages = [s for s, _ in plan.skipped]
    assert "ingest" in skipped_stages
    # stage_2_parallel runs (no embed/detect/discovery artefacts yet)
    assert "stage_2_parallel" in plan.stages


def test_cascade_skip_reason_is_clear(tmp_path: Path) -> None:
    case_path = _seed_minimum_case(tmp_path)
    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, None, None, None)

    skipped_reasons = dict(plan.skipped)
    assert "ingest" in skipped_reasons
    assert "fresh" in skipped_reasons["ingest"]


# ─── cascade: downstream-forced rule ────────────────────────────────


def test_cascade_forces_downstream_after_first_stale(tmp_path: Path) -> None:
    """Once one upstream stage is stale, ALL downstream stages run
    regardless of whether their artefacts happen to exist (which would
    be meaningless once upstream re-runs)."""
    case_path = _seed_minimum_case(tmp_path)
    _write_stale_embeddings(case_path)

    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, None, None, None)

    # ingest is fresh → skipped
    skipped_stages = [s for s, _ in plan.skipped]
    assert "ingest" in skipped_stages
    # stage_2_parallel has stale embed → must run
    assert "stage_2_parallel" in plan.stages
    # downstream of stage_2 must ALL be in the plan
    assert "stage_3_parallel" in plan.stages
    assert "scope_classify" in plan.stages
    assert "export" in plan.stages


def test_cascade_skips_when_fresh_embed_and_no_downstream_artefacts(
    tmp_path: Path,
) -> None:
    """Both ingest + embed fresh → both skipped. But ALL of stage 2's
    sub-stages need to be fresh for stage_2_parallel to be skipped
    (detect_2_1_to_2_4 + pii_discovery would still have missing
    artefacts), so stage_2_parallel still runs."""
    case_path = _seed_minimum_case(tmp_path)
    _write_fresh_embeddings(case_path)

    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, None, None, None)

    skipped_stages = [s for s, _ in plan.skipped]
    assert "ingest" in skipped_stages
    # detect + discovery artefacts still missing → stage_2_parallel runs
    assert "stage_2_parallel" in plan.stages


# ─── cascade: opt-out ───────────────────────────────────────────────


def test_run_force_flag_bypasses_cascade(tmp_path: Path) -> None:
    """Operator-facing --force flag → pipeline.run(force=True) → all
    stages run regardless of artefact freshness."""
    from dsar_orchestrator.pipeline import run

    case_path = _seed_minimum_case(tmp_path)
    _write_fresh_embeddings(case_path)
    # --force + --check together: prints the plan with everything in it
    report = run(case_path.name, case_root=case_path, check=True, force=True)
    # With force=True + check=True, the would-be-run stages are all 8.
    # (stages_skipped here is the orchestrator's "would have run" list
    # when check=True; we just confirm it's the full set.)
    assert "ingest" in report.stages_skipped
    assert "stage_2_parallel" in report.stages_skipped
    assert "export" in report.stages_skipped


def test_skip_fresh_artefacts_false_includes_everything(tmp_path: Path) -> None:
    """Operator passes the equivalent of --if-exists overwrite — every
    stage runs regardless of artefact freshness."""
    case_path = _seed_minimum_case(tmp_path)
    _write_fresh_embeddings(case_path)

    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, None, None, None, skip_fresh_artefacts=False)

    assert plan.stages == [
        "ingest",
        "stage_2_parallel",
        "stage_3_parallel",
        "scope_classify",
        "pii_classify",
        "redact",
        "bake",
        "redact_verify",
        "export",
    ]
    # Nothing skipped via the cascade; only phase-disabled exclusions
    # would have been (none in this fixture).
    assert plan.skipped == []


# ─── cascade interacts with --from / --through ──────────────────────


def test_cascade_within_from_through_range(tmp_path: Path) -> None:
    """--from + --through clip the range; cascade applies within the
    clipped range. Stages outside the range are simply absent from
    the candidate, not in `skipped` (that's reserved for active
    skips inside the range)."""
    case_path = _seed_minimum_case(tmp_path)
    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, "scope_classify", "redact", None)

    # Range is [scope_classify, pii_classify, redact]
    candidate_or_skipped = set(plan.stages) | {s for s, _ in plan.skipped}
    assert candidate_or_skipped <= {"scope_classify", "pii_classify", "redact"}
    assert "ingest" not in candidate_or_skipped
    assert "export" not in candidate_or_skipped


def test_only_flag_bypasses_cascade(tmp_path: Path) -> None:
    """--only short-circuits before the cascade — operator wants this
    one stage, full stop."""
    case_path = _seed_minimum_case(tmp_path)
    _write_fresh_embeddings(case_path)

    cfg = load_case_config(case_path.name, case_root=case_path)
    plan = build_stage_plan(case_path, cfg, None, None, "embed")
    assert plan.stages == ["embed"]
    # No cascade-driven skips
    assert plan.skipped == []
