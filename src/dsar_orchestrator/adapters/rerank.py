"""Conductor-owned rerank adapter — replaces the lazy-import to the
non-existent `dsar_rerank.core` (Contract B / issue #11; the toolkit's
`dsar_rerank` module was never written; rerank lives at
`dsar_clients.tei_rerank_client`).

Reads `working/cosine_prefilter.jsonl`, calls TEI's bge-reranker-large
via `dsar_clients.tei_rerank_client.rerank_pairs(query=case_scope,
docs=[texts])`, writes `working/scope_rerank.jsonl` with the cascade's
required `upstream_hash` field. Row shape locked by Contract A helpers
+ the existing stub fixture.

**Retirement contract.** When the toolkit ships
`dsar_pipeline.rerank.run_for_case(case_path)`, this adapter retires;
`pipeline._run_scope_filter_chain` switches its import. The output
JSONL shape must match what the toolkit eventually writes so downstream
artefacts + the resume cascade are unaffected.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import sha256_file, sha256_text

PRODUCER_VERSION = "dsar_orchestrator.adapters.rerank 0.4.9"
SCHEMA_VERSION = "1.0"


# ─── injectable HTTP-client protocol ────────────────────────────────


class _RerankResultLike(Protocol):
    """Duck-type for `dsar_clients.tei_rerank_client.RerankResult`."""

    scores: list[float]
    model_alias: str
    resolved_model: str
    endpoint_url: str
    model_revision: str
    latency_s: float
    error: str | None

    def as_audit_fields(self) -> dict[str, str | float]: ...


RerankerFn = Callable[[str, list[str]], _RerankResultLike]


def _default_reranker() -> RerankerFn:
    """Resolve the live `tei_rerank_client.rerank_pairs` callable lazily."""
    try:
        mod = importlib.import_module("dsar_clients.tei_rerank_client")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_clients.tei_rerank_client is not installed. The "
            "conductor's rerank adapter needs it to call TEI :8084. "
            "Install dsar-toolkit (pip install -e ~/projects/"
            "dsar-toolkit/) and retry."
        ) from exc

    def _adapt(query: str, docs: list[str]) -> _RerankResultLike:
        return mod.rerank_pairs(query=query, docs=docs)

    return _adapt


# ─── public entry ──────────────────────────────────────────────────


def run_for_case(cfg: CaseConfig, *, reranker: RerankerFn | None = None) -> None:
    """Rerank every cosine-prefilter row under `cfg.case_path` and write
    `working/scope_rerank.jsonl`.

    `reranker` is injectable for tests; in production the default
    resolves to `dsar_clients.tei_rerank_client.rerank_pairs`.
    """
    cosine_path = cfg.case_path / "working" / "cosine_prefilter.jsonl"
    if not cosine_path.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: cosine_prefilter.jsonl missing at {cosine_path}. "
            f"Run scope_prefilter first: dsar-conductor --case {cfg.case_no} "
            f"--only scope_prefilter"
        )

    cosine_rows = [
        json.loads(line) for line in cosine_path.read_text().splitlines() if line.strip()
    ]

    # Empty input — write empty output (deterministic; cascade has the
    # right anchor file even if nothing came through).
    if not cosine_rows:
        _atomic_write(cfg.case_path / "working" / "scope_rerank.jsonl", "")
        return

    refs = [r["ref"] for r in cosine_rows]
    texts = _load_texts(cfg.case_path, refs)
    upstream_hash = _compute_upstream_hash(cfg)

    if reranker is None:
        reranker = _default_reranker()

    result = reranker(cfg.case_scope, texts)

    if result.error:
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI rerank failed: {result.error}. "
            f"Check that TEI is running at {result.endpoint_url}."
        )
    if len(result.scores) != len(refs):
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI returned {len(result.scores)} scores "
            f"for {len(refs)} refs (mismatch)."
        )

    audit = result.as_audit_fields()
    out_rows = []
    for ref, score in zip(refs, result.scores, strict=True):
        out_rows.append(
            {
                "ref": ref,
                "rerank_score": score,
                "would_drop": score < cfg.rerank_threshold,
                "mode": cfg.rerank_mode,
                "upstream_hash": upstream_hash,
                "schema_version": SCHEMA_VERSION,
                "producer_version": PRODUCER_VERSION,
                "latency_s": result.latency_s,
                **audit,
            }
        )

    _atomic_write(
        cfg.case_path / "working" / "scope_rerank.jsonl",
        "\n".join(json.dumps(r) for r in out_rows) + "\n",
    )


def _load_texts(case_path: Path, refs: list[str]) -> list[str]:
    """Read working/<ref>.txt per Contract A. Missing text files raise."""
    from dsar_orchestrator.register import text_path_for_ref

    texts: list[str] = []
    for ref in refs:
        text_path = text_path_for_ref(case_path, ref)
        if not text_path.exists():
            raise DSARPipelineError(f"missing text file for ref={ref}: {text_path}")
        texts.append(text_path.read_text(encoding="utf-8", errors="replace"))
    return texts


def _compute_upstream_hash(cfg: CaseConfig) -> str:
    """Mirror of stages._hash_cosine_plus_scope so this adapter records
    the cascade-correct upstream hash without importing stages."""
    cosine_path = cfg.case_path / "working" / "cosine_prefilter.jsonl"
    cosine = sha256_file(cosine_path) if cosine_path.exists() else ""
    return sha256_text(
        f"{cosine}\x1f{cfg.case_scope}\x1f"
        f"thr={cfg.rerank_threshold}\x1f"
        f"topN={cfg.rerank_top_n}\x1f"
        f"mode={cfg.rerank_mode}"
    )


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
