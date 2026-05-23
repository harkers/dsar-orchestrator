"""Tests for the scope-prefilter adapter — `adapters.scope_prefilter`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import scope_prefilter as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


class FakeEmbedResult:
    def __init__(
        self,
        vectors: list[list[float]],
        *,
        error: str | None = None,
        endpoint_url: str = "http://127.0.0.1:8085",
    ) -> None:
        self.vectors = vectors
        self.error = error
        self.endpoint_url = endpoint_url


def _make_cfg(
    case_path: Path,
    *,
    case_scope: str = "All personal data about James Carter.",
    rerank_threshold: float = 0.30,
) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope=case_scope,
        subject_identifier=SubjectIdentifier(primary_name="James Carter"),
        rerank_threshold=rerank_threshold,
    )


def _seed_embeddings(case_path: Path, rows: list[dict]) -> Path:
    """Write embeddings.jsonl with the rows the adapter will consume."""
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    path = working / "embeddings.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


# ─── happy path ────────────────────────────────────────────────────


def test_writes_one_row_per_embedding(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(
        case_path,
        [
            {"ref": "doc-1", "embedding": [1.0, 0.0, 0.0]},
            {"ref": "doc-2", "embedding": [0.0, 1.0, 0.0]},
            {"ref": "doc-3", "embedding": [0.0, 0.0, 1.0]},
        ],
    )

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0, 0.0]])

    adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert [r["ref"] for r in rows] == ["doc-1", "doc-2", "doc-3"]


def test_pass_when_above_threshold(tmp_path: Path) -> None:
    """doc-1 is identical to case scope vec → cosine 1.0 → passes."""
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "doc-1", "embedding": [1.0, 0.0]}])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0]])

    adapter.run_for_case(_make_cfg(case_path, rerank_threshold=0.5), embedder=fake_embed)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert rows[0]["passes"] is True
    assert rows[0]["verdict"] == "in_scope_candidate"
    assert rows[0]["cosine_score"] == pytest.approx(1.0)


def test_fail_when_below_threshold(tmp_path: Path) -> None:
    """doc-1 is orthogonal to case scope vec → cosine 0.0 → fails."""
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "doc-1", "embedding": [0.0, 1.0]}])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0]])

    adapter.run_for_case(_make_cfg(case_path, rerank_threshold=0.5), embedder=fake_embed)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert rows[0]["passes"] is False
    assert rows[0]["verdict"] == "not_relevant"
    assert rows[0]["cosine_score"] == pytest.approx(0.0)


def test_dimension_mismatch_recorded(tmp_path: Path) -> None:
    """A doc with the wrong dim gets a dimension_mismatch verdict, no crash."""
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(
        case_path,
        [{"ref": "doc-1", "embedding": [1.0, 0.0]}, {"ref": "doc-2", "embedding": [1.0, 0.0, 0.0]}],
    )

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0]])

    adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert rows[0]["verdict"] == "in_scope_candidate"
    assert rows[1]["verdict"] == "dimension_mismatch"
    assert rows[1]["passes"] is False


def test_uses_cfg_rerank_threshold(tmp_path: Path) -> None:
    """Cosine = 0.6; threshold 0.5 passes, threshold 0.7 fails."""
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "x", "embedding": [0.6, 0.8]}])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0]])

    # threshold 0.5
    adapter.run_for_case(_make_cfg(case_path, rerank_threshold=0.5), embedder=fake_embed)
    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert rows[0]["passes"] is True

    # threshold 0.7
    adapter.run_for_case(_make_cfg(case_path, rerank_threshold=0.7), embedder=fake_embed)
    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert rows[0]["passes"] is False


def test_upstream_hash_matches_embeddings_file_sha(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    embed_path = _seed_embeddings(case_path, [{"ref": "x", "embedding": [1.0, 0.0]}])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0]])

    adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    from dsar_orchestrator.hash_chain import sha256_file

    expected = sha256_file(embed_path)
    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert rows[0]["upstream_hash"] == expected


def test_row_carries_schema_producer_threshold(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "x", "embedding": [1.0, 0.0]}])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0]])

    adapter.run_for_case(_make_cfg(case_path, rerank_threshold=0.42), embedder=fake_embed)

    rows = [
        json.loads(line)
        for line in (case_path / "working" / "cosine_prefilter.jsonl").read_text().splitlines()
        if line
    ]
    assert rows[0]["schema_version"] == "1.0"
    assert rows[0]["producer_version"].startswith("dsar_orchestrator.adapters.scope_prefilter")
    assert rows[0]["threshold"] == 0.42


# ─── error handling ────────────────────────────────────────────────


def test_raises_when_embeddings_missing(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    with pytest.raises(DSARPipelineError, match="embed output missing"):
        adapter.run_for_case(_make_cfg(case_path), embedder=lambda t: FakeEmbedResult([]))


def test_raises_when_embeddings_empty(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    (case_path / "working").mkdir(parents=True)
    (case_path / "working" / "embeddings.jsonl").write_text("")
    with pytest.raises(DSARPipelineError, match="no rows"):
        adapter.run_for_case(_make_cfg(case_path), embedder=lambda t: FakeEmbedResult([]))


def test_raises_when_case_scope_empty(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "x", "embedding": [1.0, 0.0]}])
    cfg = _make_cfg(case_path, case_scope="   ")
    with pytest.raises(DSARPipelineError, match="case_scope is empty"):
        adapter.run_for_case(cfg, embedder=lambda t: FakeEmbedResult([[1.0, 0.0]]))


def test_raises_on_embedder_error(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "x", "embedding": [1.0, 0.0]}])

    def failing_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[], error="TEI down")

    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="TEI embed of scope statement failed"):
        adapter.run_for_case(cfg, embedder=failing_embed)


def test_raises_when_embedder_returns_no_vectors(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "x", "embedding": [1.0, 0.0]}])

    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="no vector for the scope statement"):
        adapter.run_for_case(cfg, embedder=lambda t: FakeEmbedResult(vectors=[]))


def test_raises_when_embedding_row_malformed(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    (case_path / "working").mkdir(parents=True)
    (case_path / "working" / "embeddings.jsonl").write_text(json.dumps({"only_ref": "x"}) + "\n")
    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="missing ref/embedding"):
        adapter.run_for_case(cfg, embedder=lambda t: FakeEmbedResult([[1.0, 0.0]]))


# ─── atomic write ──────────────────────────────────────────────────


def test_atomic_write_no_temp_leftover(tmp_path: Path) -> None:
    case_path = tmp_path / "300800"
    case_path.mkdir()
    _seed_embeddings(case_path, [{"ref": "x", "embedding": [1.0, 0.0]}])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[1.0, 0.0]])

    adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    working = case_path / "working"
    assert (working / "cosine_prefilter.jsonl").exists()
    assert not any(p.suffix == ".tmp" for p in working.iterdir())
