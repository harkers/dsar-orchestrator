"""Tests for the in-process per-sub-stage validation agents.

Each agent is a deterministic function that reads what its sub-stage
produced and returns a ``ModuleCheckResult``. These tests cover the
happy path + the headline sad paths for every agent.

Pipeline-level integration (audit-row writing + PipelineHalt on
critical) is exercised in ``test_module_checks_integration.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.module_agents import (
    CHECKERS,
    ModuleCheckResult,
    check_detect_2_1_to_2_4,
    check_embed,
    check_export,
    check_ingest,
    check_people_register,
    check_pii_classify,
    check_pii_discovery,
    check_redact,
    check_redact_verify,
    check_rerank,
    check_scope_classify,
    check_scope_prefilter,
    check_work,
)

# ─── helpers ────────────────────────────────────────────────────────


def _make_cfg(
    case_path: Path,
    case_no: str = "300400",
    *,
    rerank_mode: str = "shadow",
    pii_classify_mode: str = "shadow",
    discovery_enabled: bool = True,
    redact_verify_enabled: bool = True,
) -> CaseConfig:
    return CaseConfig(
        case_no=case_no,
        case_path=case_path,
        case_scope="test scope",
        subject_identifier=SubjectIdentifier(primary_name="Test"),
        rerank_mode=rerank_mode,
        pii_classify_mode=pii_classify_mode,
        discovery_enabled=discovery_enabled,
        redact_verify_enabled=redact_verify_enabled,
    )


def _make_case(tmp_path: Path, case_no: str = "300400") -> Path:
    case_path = tmp_path / case_no
    (case_path / "source").mkdir(parents=True)
    (case_path / "working").mkdir()
    (case_path / "redacted").mkdir()
    (case_path / "output").mkdir()
    return case_path


def _write_register(case_path: Path, refs: list[str] | None = None) -> None:
    refs = refs or ["doc-0001", "doc-0002"]
    for ref in refs:
        (case_path / "source" / f"{ref}.txt").write_text(f"content of {ref}")
    register = {
        "case_no": case_path.name,
        "refs": [{"ref": ref, "text_path": f"source/{ref}.txt"} for ref in refs],
        "upstream_hash": "abc123",
    }
    (case_path / "working" / "register.json").write_text(json.dumps(register))


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# ─── ingest ─────────────────────────────────────────────────────────


def test_ingest_critical_when_register_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_ingest(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_ingest_critical_on_invalid_json(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "working" / "register.json").write_text("not valid json {")
    result = check_ingest(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"
    assert "not valid JSON" in result.findings[0]


def test_ingest_critical_when_no_refs(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "working" / "register.json").write_text(
        json.dumps({"case_no": case_path.name, "refs": [], "upstream_hash": "abc"})
    )
    result = check_ingest(_make_cfg(case_path))
    assert result.ok is False
    assert "no refs" in result.findings[0]


def test_ingest_warning_when_upstream_hash_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "source" / "doc-0001.txt").write_text("x")
    (case_path / "working" / "register.json").write_text(
        json.dumps(
            {
                "case_no": case_path.name,
                "refs": [{"ref": "doc-0001", "text_path": "source/doc-0001.txt"}],
            }
        )
    )
    result = check_ingest(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "warning"


def test_ingest_critical_when_text_path_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "working" / "register.json").write_text(
        json.dumps(
            {
                "case_no": case_path.name,
                "refs": [{"ref": "x", "text_path": "source/does-not-exist.txt"}],
                "upstream_hash": "abc",
            }
        )
    )
    result = check_ingest(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"
    assert any("missing" in f for f in result.findings)


def test_ingest_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _write_register(case_path)
    result = check_ingest(_make_cfg(case_path))
    assert result.ok is True
    assert result.severity == "info"


# ─── embed ──────────────────────────────────────────────────────────


def test_embed_critical_when_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_embed(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_embed_critical_when_dim_wrong(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "embeddings.jsonl",
        [
            {
                "ref": "x",
                "embedding": [0.1, 0.2, 0.3],  # only 3 dims
                "upstream_hash": "h",
            }
        ],
    )
    result = check_embed(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"
    assert "dim=3" in result.findings[0]


def test_embed_critical_when_missing_fields(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(case_path / "working" / "embeddings.jsonl", [{"ref": "x"}])
    result = check_embed(_make_cfg(case_path))
    assert result.ok is False
    assert "missing required fields" in result.findings[0]


def test_embed_warning_on_mixed_upstream_hashes(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    rows = [
        {"ref": "a", "embedding": [0.1] * 1024, "upstream_hash": "one"},
        {"ref": "b", "embedding": [0.1] * 1024, "upstream_hash": "two"},
    ]
    _jsonl(case_path / "working" / "embeddings.jsonl", rows)
    result = check_embed(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "warning"


def test_embed_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "embeddings.jsonl",
        [
            {"ref": "x", "embedding": [0.1] * 1024, "upstream_hash": "h"},
            {"ref": "y", "embedding": [0.2] * 1024, "upstream_hash": "h"},
        ],
    )
    result = check_embed(_make_cfg(case_path))
    assert result.ok is True


# ─── detect_2_1_to_2_4 ──────────────────────────────────────────────


def test_detect_critical_when_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_detect_2_1_to_2_4(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_detect_critical_when_register_refs_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _write_register(case_path, refs=["doc-0001", "doc-0002"])
    _jsonl(
        case_path / "working" / "detect_entities.jsonl",
        [{"ref": "doc-0001", "entities": [], "upstream_hash": "h"}],
    )
    # doc-0002 is missing
    result = check_detect_2_1_to_2_4(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_detect_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _write_register(case_path, refs=["a", "b"])
    _jsonl(
        case_path / "working" / "detect_entities.jsonl",
        [
            {"ref": "a", "entities": [], "upstream_hash": "h"},
            {"ref": "b", "entities": [], "upstream_hash": "h"},
        ],
    )
    result = check_detect_2_1_to_2_4(_make_cfg(case_path))
    assert result.ok is True


# ─── pii_discovery ─────────────────────────────────────────────────


def test_pii_discovery_skipped_when_disabled(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    cfg = _make_cfg(case_path, discovery_enabled=False)
    result = check_pii_discovery(cfg)
    assert result.ok is True
    assert "skipping" in result.findings[0]


def test_pii_discovery_critical_when_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_pii_discovery(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_pii_discovery_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "pii_discovery.jsonl",
        [{"ref": "x", "entities": [], "upstream_hash": "h"}],
    )
    result = check_pii_discovery(_make_cfg(case_path))
    assert result.ok is True


# ─── people_register ────────────────────────────────────────────────


def test_people_register_critical_when_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_people_register(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_people_register_critical_when_no_clusters_key(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "working" / "person_index.json").write_text(json.dumps({"other": []}))
    result = check_people_register(_make_cfg(case_path))
    assert result.ok is False
    assert "clusters" in result.findings[0]


def test_people_register_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "working" / "person_index.json").write_text(
        json.dumps({"clusters": [], "upstream_hash": "h"})
    )
    result = check_people_register(_make_cfg(case_path))
    assert result.ok is True


# ─── scope_prefilter ────────────────────────────────────────────────


def test_scope_prefilter_critical_on_out_of_range(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "cosine_prefilter.jsonl",
        [
            {
                "ref": "x",
                "cosine_score": 1.5,  # out of [-1, 1]
                "passes": True,
                "upstream_hash": "h",
            }
        ],
    )
    result = check_scope_prefilter(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_scope_prefilter_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "cosine_prefilter.jsonl",
        [
            {"ref": "x", "cosine_score": 0.5, "passes": True, "upstream_hash": "h"},
            {"ref": "y", "cosine_score": -0.2, "passes": False, "upstream_hash": "h"},
        ],
    )
    result = check_scope_prefilter(_make_cfg(case_path))
    assert result.ok is True


# ─── rerank ─────────────────────────────────────────────────────────


def test_rerank_skipped_when_mode_off(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    cfg = _make_cfg(case_path, rerank_mode="off")
    result = check_rerank(cfg)
    assert result.ok is True
    assert "skipping" in result.findings[0]


def test_rerank_critical_when_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_rerank(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_rerank_warning_on_mode_mismatch(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    cfg = _make_cfg(case_path, rerank_mode="enforce")
    _jsonl(
        case_path / "working" / "scope_rerank.jsonl",
        [
            {
                "ref": "x",
                "rerank_score": 0.5,
                "would_drop": False,
                "mode": "shadow",  # mismatch with cfg
                "upstream_hash": "h",
            }
        ],
    )
    result = check_rerank(cfg)
    assert result.ok is False
    assert result.severity == "warning"


def test_rerank_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "scope_rerank.jsonl",
        [
            {
                "ref": "x",
                "rerank_score": 0.1,
                "would_drop": False,
                "mode": "shadow",
                "upstream_hash": "h",
            }
        ],
    )
    result = check_rerank(_make_cfg(case_path))
    assert result.ok is True


# ─── scope_classify ─────────────────────────────────────────────────


def test_scope_classify_critical_when_missing_complete(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_scope_classify(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_scope_classify_critical_when_tags_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _write_register(case_path, refs=["a", "b"])
    _jsonl(
        case_path / "working" / "scope_classify_complete.jsonl",
        [{"completed": True, "upstream_hash": "h"}],
    )
    # tags files not written
    result = check_scope_classify(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_scope_classify_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _write_register(case_path, refs=["a", "b"])
    _jsonl(
        case_path / "working" / "scope_classify_complete.jsonl",
        [{"completed": True, "upstream_hash": "h"}],
    )
    for ref in ("a", "b"):
        (case_path / "working" / f"{ref}_tags.json").write_text(
            json.dumps({"ref": ref, "in_scope": True})
        )
    result = check_scope_classify(_make_cfg(case_path))
    assert result.ok is True


# ─── pii_classify ───────────────────────────────────────────────────


def test_pii_classify_skipped_when_mode_off(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    cfg = _make_cfg(case_path, pii_classify_mode="off")
    result = check_pii_classify(cfg)
    assert result.ok is True


def test_pii_classify_critical_on_bad_recheck_verdict(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "pii_collection.jsonl",
        [
            {
                "ref": "x",
                "in_scope_recheck": "bogus_verdict",
                "entities": [],
                "upstream_hash": "h",
            }
        ],
    )
    result = check_pii_classify(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_pii_classify_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _jsonl(
        case_path / "working" / "pii_collection.jsonl",
        [
            {
                "ref": "x",
                "in_scope_recheck": "confirmed",
                "entities": [],
                "upstream_hash": "h",
            },
            {
                "ref": "y",
                "in_scope_recheck": "uncertain",
                "entities": [],
                "upstream_hash": "h",
            },
        ],
    )
    result = check_pii_classify(_make_cfg(case_path))
    assert result.ok is True


# ─── redact ─────────────────────────────────────────────────────────


def test_redact_critical_when_complete_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_redact(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_redact_critical_when_redacted_dir_empty(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "working" / "redact_complete.json").write_text(json.dumps({"upstream_hash": "h"}))
    result = check_redact(_make_cfg(case_path))
    assert result.ok is False
    assert "empty" in result.findings[0]


def test_redact_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "working" / "redact_complete.json").write_text(json.dumps({"upstream_hash": "h"}))
    (case_path / "redacted" / "doc-0001.txt").write_text("[REDACTED]")
    result = check_redact(_make_cfg(case_path))
    assert result.ok is True


# ─── redact_verify ──────────────────────────────────────────────────


def test_redact_verify_skipped_when_disabled(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    cfg = _make_cfg(case_path, redact_verify_enabled=False)
    result = check_redact_verify(cfg)
    assert result.ok is True


def test_redact_verify_critical_when_log_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    case_path = _make_case(tmp_path)
    result = check_redact_verify(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_redact_verify_happy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    case_path = _make_case(tmp_path)
    audit_dir = tmp_path / ".dsar-audit" / case_path.name
    audit_dir.mkdir(parents=True)
    _jsonl(
        audit_dir / "redact_verify.jsonl",
        [{"event": "verify_complete", "passed": True, "upstream_hash": "h"}],
    )
    result = check_redact_verify(_make_cfg(case_path))
    assert result.ok is True


def test_redact_verify_critical_on_recorded_failure(tmp_path: Path, monkeypatch) -> None:
    """If the verifier wrote a passed=false row but the pipeline kept
    running, the toolkit is misbehaving — flag it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    case_path = _make_case(tmp_path)
    audit_dir = tmp_path / ".dsar-audit" / case_path.name
    audit_dir.mkdir(parents=True)
    _jsonl(
        audit_dir / "redact_verify.jsonl",
        [
            {"event": "verify", "passed": True, "ref": "a"},
            {"event": "verify", "passed": False, "ref": "b"},
        ],
    )
    result = check_redact_verify(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


# ─── export ─────────────────────────────────────────────────────────


def test_export_critical_when_manifest_missing(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_export(_make_cfg(case_path))
    assert result.ok is False
    assert result.severity == "critical"


def test_export_critical_when_no_pdfs(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "output" / "manifest.json").write_text(json.dumps({"upstream_hash": "h"}))
    result = check_export(_make_cfg(case_path))
    assert result.ok is False
    assert "no PDF" in result.findings[0]


def test_export_happy(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    (case_path / "output" / "manifest.json").write_text(json.dumps({"upstream_hash": "h"}))
    (case_path / "output" / "doc-0001.pdf").write_text("fake pdf")
    result = check_export(_make_cfg(case_path))
    assert result.ok is True


# ─── dispatch ──────────────────────────────────────────────────────


def test_check_work_dispatches_by_sub_stage(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    _write_register(case_path)
    result = check_work(_make_cfg(case_path), "ingest")
    assert result.ok is True


def test_check_work_unknown_sub_stage_returns_critical(tmp_path: Path) -> None:
    case_path = _make_case(tmp_path)
    result = check_work(_make_cfg(case_path), "not_a_real_stage")
    assert result.ok is False
    assert result.severity == "critical"
    assert "No agent registered" in result.findings[0]


def test_checkers_dict_has_all_known_sub_stages() -> None:
    """The CHECKERS registry must cover every sub-stage the
    orchestrator runs."""
    from dsar_orchestrator.pipeline import SUB_STAGES_BY_STAGE

    all_subs: set[str] = set()
    for subs in SUB_STAGES_BY_STAGE.values():
        all_subs.update(subs)
    missing = all_subs - set(CHECKERS.keys())
    assert not missing, f"CHECKERS missing agents for: {missing}"


def test_module_check_result_dataclass() -> None:
    r = ModuleCheckResult(ok=True, severity="info")
    assert r.findings == []
    assert r.recommendation == ""
