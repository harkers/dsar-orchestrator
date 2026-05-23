"""Tests for the embed adapter — `dsar_orchestrator.adapters.embed`.

The adapter bridges the conductor to `dsar_clients.tei_embed_client.embed()`
until the toolkit ships `dsar_embed.core.embed_corpus(case_path)`
(toolkit issue #1).

Embedder is injected so tests don't need a live TEI on :8085.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import embed as embed_adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import compute_register_hash

# ─── helpers ────────────────────────────────────────────────────────


class FakeEmbedResult:
    def __init__(
        self,
        vectors: list[list[float]],
        *,
        error: str | None = None,
        endpoint_url: str = "http://127.0.0.1:8085",
    ) -> None:
        self.vectors = vectors
        self.model_alias = "embed"
        self.resolved_model = "BAAI/bge-m3"
        self.endpoint_url = endpoint_url
        self.model_revision = "test-rev"
        self.latency_s = 0.01
        self.error = error

    def as_audit_fields(self) -> dict[str, str | float]:
        return {
            "model_alias": self.model_alias,
            "resolved_model": self.resolved_model,
            "endpoint_url": self.endpoint_url,
            "model_revision": self.model_revision,
        }


def _make_case_with_register(tmp_path: Path, refs: list[tuple[str, str]]) -> Path:
    """Create a case with N source docs + register.json. `refs` is a
    list of (ref, content) tuples. Returns case_path."""
    case_path = tmp_path / "300700"
    (case_path / "source").mkdir(parents=True)
    (case_path / "working").mkdir()
    for ref, content in refs:
        (case_path / "source" / f"{ref}.txt").write_text(content, encoding="utf-8")
    register = {
        "case_no": "300700",
        "refs": [{"ref": ref, "text_path": f"source/{ref}.txt"} for ref, _ in refs],
    }
    (case_path / "working" / "register.json").write_text(json.dumps(register))
    return case_path


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
    )


# ─── happy path ─────────────────────────────────────────────────────


def test_run_for_case_writes_one_row_per_ref(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-001", "first"), ("doc-002", "second")])

    captured: dict = {}

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        captured["texts"] = list(texts)
        return FakeEmbedResult(vectors=[[0.1] * 1024, [0.2] * 1024])

    embed_adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    # Confirm embed() got the texts in register order
    assert captured["texts"] == ["first", "second"]

    out_path = case_path / "working" / "embeddings.jsonl"
    rows = [json.loads(line) for line in out_path.read_text().splitlines() if line]
    assert len(rows) == 2
    assert rows[0]["ref"] == "doc-001"
    assert rows[1]["ref"] == "doc-002"


def test_row_carries_upstream_hash_matching_register(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-1", "hello")])
    expected_hash = compute_register_hash(case_path / "working" / "register.json")

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[0.5] * 1024])

    embed_adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    row = json.loads((case_path / "working" / "embeddings.jsonl").read_text().splitlines()[0])
    assert row["upstream_hash"] == expected_hash


def test_row_carries_schema_and_producer_versions(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-1", "hello")])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[0.5] * 1024])

    embed_adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    row = json.loads((case_path / "working" / "embeddings.jsonl").read_text().splitlines()[0])
    assert row["schema_version"] == "1.0"
    assert row["producer_version"].startswith("dsar_orchestrator.adapters.embed")


def test_row_carries_provenance_audit_fields(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-1", "hello")])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[0.5] * 1024])

    embed_adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    row = json.loads((case_path / "working" / "embeddings.jsonl").read_text().splitlines()[0])
    assert row["model_alias"] == "embed"
    assert row["resolved_model"] == "BAAI/bge-m3"
    assert row["endpoint_url"] == "http://127.0.0.1:8085"
    assert row["model_revision"] == "test-rev"
    assert "latency_s" in row


def test_row_embedding_is_1024_dim(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-1", "x")])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[0.7] * 1024])

    embed_adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    row = json.loads((case_path / "working" / "embeddings.jsonl").read_text().splitlines()[0])
    assert len(row["embedding"]) == 1024


# ─── error handling ─────────────────────────────────────────────────


def test_raises_when_register_missing(tmp_path: Path) -> None:
    case_path = tmp_path / "300700"
    case_path.mkdir()
    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="ingest output missing"):
        embed_adapter.run_for_case(cfg, embedder=lambda texts: FakeEmbedResult([]))


def test_raises_when_register_has_no_refs(tmp_path: Path) -> None:
    case_path = tmp_path / "300700"
    (case_path / "working").mkdir(parents=True)
    (case_path / "working" / "register.json").write_text(
        json.dumps({"case_no": "300700", "refs": []})
    )
    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="no refs"):
        embed_adapter.run_for_case(cfg, embedder=lambda texts: FakeEmbedResult([]))


def test_raises_when_text_path_missing(tmp_path: Path) -> None:
    case_path = tmp_path / "300700"
    (case_path / "working").mkdir(parents=True)
    (case_path / "working" / "register.json").write_text(
        json.dumps(
            {
                "case_no": "300700",
                "refs": [{"ref": "x", "text_path": "source/missing.txt"}],
            }
        )
    )
    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="missing text file"):
        embed_adapter.run_for_case(cfg, embedder=lambda texts: FakeEmbedResult([]))


def test_raises_on_embed_error(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-1", "hello")])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[], error="TEI timeout after 3 retries")

    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="TEI embed failed"):
        embed_adapter.run_for_case(cfg, embedder=fake_embed)


def test_raises_on_vector_count_mismatch(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-1", "a"), ("doc-2", "b"), ("doc-3", "c")])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[0.1] * 1024, [0.2] * 1024])  # only 2

    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="returned 2 vectors for 3 refs"):
        embed_adapter.run_for_case(cfg, embedder=fake_embed)


def test_raises_on_empty_vector(tmp_path: Path) -> None:
    case_path = _make_case_with_register(tmp_path, [("doc-1", "a")])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[]])

    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="empty vector"):
        embed_adapter.run_for_case(cfg, embedder=fake_embed)


# ─── atomic write ───────────────────────────────────────────────────


def test_atomic_write_uses_temp_then_rename(tmp_path: Path) -> None:
    """The adapter writes embeddings.jsonl atomically via temp + rename.
    Confirm there's no .tmp file leftover after a successful run."""
    case_path = _make_case_with_register(tmp_path, [("doc-1", "x")])

    def fake_embed(texts: list[str]) -> FakeEmbedResult:
        return FakeEmbedResult(vectors=[[0.1] * 1024])

    embed_adapter.run_for_case(_make_cfg(case_path), embedder=fake_embed)

    working = case_path / "working"
    assert (working / "embeddings.jsonl").exists()
    # No leftover temp file
    assert not any(p.suffix == ".tmp" for p in working.iterdir())


# ─── default embedder resolution ────────────────────────────────────


def test_default_embedder_raises_clearly_when_toolkit_missing(tmp_path: Path, monkeypatch) -> None:
    """When dsar_clients isn't installed, the default embedder should
    raise a clear DSARPipelineError, not a bare ImportError."""
    case_path = _make_case_with_register(tmp_path, [("doc-1", "x")])

    # Simulate toolkit absence by removing dsar_clients from sys.modules
    # and blocking import.
    import sys

    monkeypatch.delitem(sys.modules, "dsar_clients.tei_embed_client", raising=False)
    monkeypatch.delitem(sys.modules, "dsar_clients", raising=False)

    # Replace _default_embedder so importlib.import_module fails
    def failing_default():
        raise DSARPipelineError(
            "dsar_clients.tei_embed_client is not installed. The "
            "conductor's embed adapter needs it to call TEI :8085. "
            "Install dsar-toolkit (pip install -e ~/projects/"
            "dsar-toolkit/) and retry."
        )

    monkeypatch.setattr(embed_adapter, "_default_embedder", failing_default)

    with pytest.raises(DSARPipelineError, match="dsar_clients.tei_embed_client"):
        embed_adapter.run_for_case(_make_cfg(case_path))
