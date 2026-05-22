"""Tests for the per-stage artefact registry + cascade primitives."""

from __future__ import annotations

import json
from pathlib import Path

from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.hash_chain import compute_register_hash, hash_pairs, sha256_file
from dsar_orchestrator.stages import (
    STAGE_ARTEFACTS,
    is_artefact_fresh,
    resolve_artefact_path,
)


def _make_cfg(case_no: str, case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_no,
        case_path=case_path,
        case_scope="test scope",
        subject_identifier=SubjectIdentifier(primary_name="Test Subject"),
        rerank_mode="shadow",
        pii_classify_mode="shadow",
    )


def _seed_ingest(case_path: Path) -> tuple[Path, str]:
    """Write a minimal source/ + working/register.json. Returns
    (register_path, upstream_hash of source tree)."""
    src = case_path / "source"
    src.mkdir(parents=True, exist_ok=True)
    (src / "doc1.txt").write_text("hello world")
    (src / "doc2.txt").write_text("another doc")

    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)

    # Synthesize the register.json that ingest would produce.
    register = {
        "case_no": case_path.name,
        "refs": [
            {"ref": case_path.name + "-0001", "text_path": "source/doc1.txt"},
            {"ref": case_path.name + "-0002", "text_path": "source/doc2.txt"},
        ],
    }
    register_path = working / "register.json"
    # The ingest stage's upstream is the source tree, so we record that
    # hash on the register.
    src_pairs = []
    for p in sorted(src.rglob("*")):
        if p.is_file():
            src_pairs.append((str(p.relative_to(src)), sha256_file(p)))
    upstream = hash_pairs(src_pairs)
    register["upstream_hash"] = upstream
    register_path.write_text(json.dumps(register))
    return register_path, upstream


# ─── registry shape ─────────────────────────────────────────────────


def test_registry_has_all_sub_stages() -> None:
    expected_subs = {
        "ingest",
        "embed",
        "detect_2_1_to_2_4",
        "pii_discovery",
        "people_register",
        "scope_prefilter",
        "rerank",
        "scope_classify",
        "pii_classify",
        "redact",
        "redact_verify",
        "export",
    }
    assert expected_subs.issubset(set(STAGE_ARTEFACTS.keys()))


def test_registry_entries_have_required_fields() -> None:
    for sub_stage, art in STAGE_ARTEFACTS.items():
        assert art.sub_stage == sub_stage
        assert art.coarse_stage
        assert callable(art.upstream_fn)


# ─── resolve_artefact_path ──────────────────────────────────────────


def test_resolve_artefact_path_case_relative(tmp_path: Path) -> None:
    case_path = tmp_path / "300001"
    p = resolve_artefact_path("300001", case_path, "working/embeddings.jsonl")
    assert p == case_path / "working" / "embeddings.jsonl"


def test_resolve_artefact_path_home_expansion(tmp_path: Path) -> None:
    # We don't actually need to control $HOME for this; just confirm
    # the substitution + expansion shape.
    p = resolve_artefact_path("300001", tmp_path, "~/.dsar-audit/<case>/x.jsonl")
    assert "300001" in str(p)
    assert ".dsar-audit" in str(p)


# ─── is_artefact_fresh ──────────────────────────────────────────────


def test_is_fresh_returns_false_when_artefact_missing(tmp_path: Path) -> None:
    case_path = tmp_path / "300001"
    case_path.mkdir()
    cfg = _make_cfg("300001", case_path)
    art = STAGE_ARTEFACTS["embed"]
    fresh, reason = is_artefact_fresh(cfg, art)
    assert fresh is False
    assert "missing" in reason


def test_is_fresh_returns_true_when_artefact_matches(tmp_path: Path) -> None:
    """Pre-seed register.json + a matching embeddings.jsonl with the
    correct upstream_hash; embed should be considered fresh."""
    case_path = tmp_path / "300001"
    case_path.mkdir()
    _seed_ingest(case_path)

    # The embed stage's upstream is the register-hash. Write an
    # embeddings.jsonl whose first row carries the matching upstream_hash.
    upstream = compute_register_hash(case_path / "working" / "register.json")
    embed_path = case_path / "working" / "embeddings.jsonl"
    row = {"ref": "x", "embedding": [0.1, 0.2], "upstream_hash": upstream}
    embed_path.write_text(json.dumps(row) + "\n")

    cfg = _make_cfg("300001", case_path)
    art = STAGE_ARTEFACTS["embed"]
    fresh, reason = is_artefact_fresh(cfg, art)
    assert fresh is True
    assert "fresh" in reason or "matches" in reason


def test_is_fresh_returns_false_on_hash_mismatch(tmp_path: Path) -> None:
    """Write an embeddings.jsonl with a STALE upstream_hash; it should
    not be considered fresh."""
    case_path = tmp_path / "300001"
    case_path.mkdir()
    _seed_ingest(case_path)

    embed_path = case_path / "working" / "embeddings.jsonl"
    row = {"ref": "x", "embedding": [0.1], "upstream_hash": "stale_hash_value"}
    embed_path.write_text(json.dumps(row) + "\n")

    cfg = _make_cfg("300001", case_path)
    art = STAGE_ARTEFACTS["embed"]
    fresh, reason = is_artefact_fresh(cfg, art)
    assert fresh is False
    assert "mismatch" in reason


def test_is_fresh_returns_false_when_upstream_missing(tmp_path: Path) -> None:
    """If register.json is missing (the upstream input), embed cannot
    be considered fresh even if an embeddings.jsonl exists."""
    case_path = tmp_path / "300001"
    (case_path / "working").mkdir(parents=True)
    # No register.json — but write an embeddings.jsonl anyway
    embed_path = case_path / "working" / "embeddings.jsonl"
    embed_path.write_text(json.dumps({"upstream_hash": "anything"}) + "\n")
    cfg = _make_cfg("300001", case_path)
    art = STAGE_ARTEFACTS["embed"]
    fresh, reason = is_artefact_fresh(cfg, art)
    assert fresh is False
    # Either "upstream missing" or "upstream empty" depending on path —
    # both mean "can't verify, treat as stale".
    assert "missing" in reason or "empty" in reason


def test_is_fresh_handles_none_relpath_safely(tmp_path: Path) -> None:
    """Stages with no tracked artefact (None relpath) should always
    report not-fresh so they re-run."""
    case_path = tmp_path / "300001"
    case_path.mkdir()
    cfg = _make_cfg("300001", case_path)
    from dsar_orchestrator.stages import StageArtefact

    none_art = StageArtefact("custom", "custom", None, lambda c: "")
    fresh, reason = is_artefact_fresh(cfg, none_art)
    assert fresh is False
    assert "no tracked" in reason
