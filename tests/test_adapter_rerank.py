"""Tests for adapters/rerank.py (Contract B / issue #11).

Mirrors tests/test_adapter_embed.py / tests/test_adapter_verify_spec.py:
inject a fake client to assert the adapter's reading + writing behaviour
without needing a live TEI service.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import rerank
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier


def _make_cfg(case_path: Path, *, scope: str = "test scope", threshold: float = 0.5) -> CaseConfig:
    return CaseConfig(
        case_no="rerank-test",
        case_path=case_path,
        case_scope=scope,
        subject_identifier=SubjectIdentifier(primary_name="Test Person"),
        rerank_mode="shadow",
        rerank_threshold=threshold,
        rerank_top_n=20,
        rerank_sample_rate=0.05,
        pii_classify_mode="shadow",
        pii_budget_usd=5.0,
        discovery_enabled=False,
        redact_verify_enabled=True,
        llm_concurrency=5,
    )


class _FakeRerankResult:
    def __init__(self, scores: list[float], error: str | None = None) -> None:
        self.scores = scores
        self.model_alias = "rerank"
        self.resolved_model = "BAAI/bge-reranker-large"
        self.endpoint_url = "http://127.0.0.1:8084"
        self.model_revision = "stub-rev"
        self.latency_s = 0.001
        self.error = error

    def as_audit_fields(self) -> dict[str, str | float]:
        return {
            "model_alias": self.model_alias,
            "resolved_model": self.resolved_model,
            "endpoint_url": self.endpoint_url,
            "model_revision": self.model_revision,
        }


def _seed_cosine_prefilter(case_path: Path, refs_and_scores: list[tuple[str, float]]) -> None:
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ref": ref,
            "cosine_score": score,
            "passes": True,
            "verdict": "in_scope_candidate",
            "threshold": 0.01,
            "upstream_hash": "stub-upstream",
            "schema_version": "1.0",
            "producer_version": "test-stub",
        }
        for ref, score in refs_and_scores
    ]
    (working / "cosine_prefilter.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    for ref, _score in refs_and_scores:
        (working / f"{ref}.txt").write_text(f"text for {ref}", encoding="utf-8")


def test_rerank_happy_path_writes_scope_rerank_jsonl(tmp_path) -> None:
    cfg = _make_cfg(tmp_path)
    _seed_cosine_prefilter(tmp_path, [("ref-1", 0.8), ("ref-2", 0.2)])

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        return _FakeRerankResult(scores=[0.9, 0.1])

    rerank.run_for_case(cfg, reranker=fake_reranker)

    out = tmp_path / "working" / "scope_rerank.jsonl"
    assert out.exists()
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert {r["ref"] for r in rows} == {"ref-1", "ref-2"}
    for row in rows:
        assert "rerank_score" in row
        assert "would_drop" in row
        assert "mode" in row
        assert "upstream_hash" in row
        assert "schema_version" in row
        assert "producer_version" in row


def test_rerank_would_drop_uses_threshold(tmp_path) -> None:
    cfg = _make_cfg(tmp_path, threshold=0.5)
    _seed_cosine_prefilter(tmp_path, [("hi", 1.0), ("lo", 1.0)])

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        return _FakeRerankResult(scores=[0.9, 0.1])

    rerank.run_for_case(cfg, reranker=fake_reranker)
    rows = [
        json.loads(line)
        for line in (tmp_path / "working" / "scope_rerank.jsonl").read_text().splitlines()
        if line.strip()
    ]
    by_ref = {r["ref"]: r for r in rows}
    assert by_ref["hi"]["would_drop"] is False  # 0.9 >= 0.5
    assert by_ref["lo"]["would_drop"] is True  # 0.1 < 0.5


def test_rerank_empty_cosine_prefilter_writes_empty_output(tmp_path) -> None:
    cfg = _make_cfg(tmp_path)
    (tmp_path / "working").mkdir(parents=True)
    (tmp_path / "working" / "cosine_prefilter.jsonl").write_text("")

    called = {"count": 0}

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        called["count"] += 1
        return _FakeRerankResult(scores=[])

    rerank.run_for_case(cfg, reranker=fake_reranker)
    out = tmp_path / "working" / "scope_rerank.jsonl"
    assert out.exists()
    assert out.read_text() == ""
    assert called["count"] == 0


def test_rerank_propagates_client_error(tmp_path) -> None:
    from dsar_orchestrator.exceptions import DSARPipelineError

    cfg = _make_cfg(tmp_path)
    _seed_cosine_prefilter(tmp_path, [("ref-1", 0.5)])

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        return _FakeRerankResult(scores=[], error="connection refused")

    with pytest.raises(DSARPipelineError, match="TEI rerank failed"):
        rerank.run_for_case(cfg, reranker=fake_reranker)


def test_rerank_missing_cosine_prefilter_raises(tmp_path) -> None:
    from dsar_orchestrator.exceptions import DSARPipelineError

    cfg = _make_cfg(tmp_path)
    (tmp_path / "working").mkdir(parents=True)

    with pytest.raises(DSARPipelineError, match="cosine_prefilter.jsonl"):
        rerank.run_for_case(cfg, reranker=lambda q, d: _FakeRerankResult([]))
