"""Conductor-owned scope-prefilter adapter — cosine gate before LLM.

Computes per-ref cosine similarity to the case-scope vector. Below the
threshold ⇒ ``not_relevant`` (excluded from the expensive LLM
scope-classify call downstream). Above ⇒ ``in_scope_candidate``
(forwarded).

**Retirement contract.** Toolkit ships
``dsar_pipeline.scope_prefilter.prefilter_scope`` already as a pure
function, but no per-case driver that reads ``embeddings.jsonl`` /
writes ``cosine_prefilter.jsonl`` with the conductor's
``upstream_hash`` chain. Eventually the toolkit will likely expose
``dsar_pipeline.scope_prefilter.run_for_case(case_path)`` (per the
prioritised adapter list posted on toolkit issue #1). When it does,
this adapter retires; the JSONL output shape is locked.
"""

from __future__ import annotations

import importlib
import json
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import sha256_file

PRODUCER_VERSION = "dsar_orchestrator.adapters.scope_prefilter 0.4.9"
SCHEMA_VERSION = "1.0"
DEFAULT_THRESHOLD = 0.30  # mirrors the toolkit's prefilter_scope default


# ─── injectable embedder ───────────────────────────────────────────


class _EmbedResultLike(Protocol):
    """Duck-type for `dsar_clients.tei_embed_client.EmbedResult`."""

    vectors: list[list[float]]
    error: str | None
    endpoint_url: str


EmbedFn = Callable[[list[str]], _EmbedResultLike]


def _default_embedder() -> EmbedFn:
    try:
        mod = importlib.import_module("dsar_clients.tei_embed_client")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_clients.tei_embed_client is not installed. The "
            "conductor's scope-prefilter adapter needs it to embed the "
            "case scope statement. Install dsar-toolkit "
            "(pip install -e ~/projects/dsar-toolkit/) and retry."
        ) from exc
    return mod.embed


# ─── public entry ──────────────────────────────────────────────────


def run_for_case(cfg: CaseConfig, *, embedder: EmbedFn | None = None) -> None:
    """Build the cosine-prefilter verdict for every embedded ref.

    Reads ``working/embeddings.jsonl``, embeds ``cfg.case_scope`` via
    ``tei_embed_client.embed()``, computes per-ref cosine, writes
    ``working/cosine_prefilter.jsonl`` with the cascade's required
    ``upstream_hash`` field.

    ``embedder`` injectable for tests; defaults to the live TEI client.
    """
    if embedder is None:
        embedder = _default_embedder()

    embeddings_path = cfg.case_path / "working" / "embeddings.jsonl"
    if not embeddings_path.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: embed output missing at {embeddings_path}. "
            f"Run embed first: dsar-conductor --case {cfg.case_no} --only embed"
        )

    refs, doc_vectors = _load_embeddings(embeddings_path)
    if not refs:
        raise DSARPipelineError(f"case={cfg.case_no}: embeddings.jsonl has no rows.")

    scope_text = cfg.case_scope or ""
    if not scope_text.strip():
        raise DSARPipelineError(
            f"case={cfg.case_no}: case_config.case_scope is empty. "
            f"Add a scope statement before running prefilter."
        )

    scope_result = embedder([scope_text])
    if scope_result.error:
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI embed of scope statement failed: "
            f"{scope_result.error}. Check TEI at {scope_result.endpoint_url}."
        )
    if not scope_result.vectors:
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI returned no vector for the scope statement."
        )
    case_vec = scope_result.vectors[0]

    threshold = cfg.rerank_threshold if cfg.rerank_threshold > 0 else DEFAULT_THRESHOLD

    # Hash of the embeddings.jsonl is the upstream we record on every
    # cosine_prefilter.jsonl row. Cascade uses this to invalidate
    # downstream if embeddings change.
    upstream_hash = sha256_file(embeddings_path)

    rows = []
    for ref, vec in zip(refs, doc_vectors, strict=True):
        if len(vec) != len(case_vec):
            verdict = "dimension_mismatch"
            score = 0.0
        else:
            score = _cosine(vec, case_vec)
            verdict = "in_scope_candidate" if score >= threshold else "not_relevant"
        rows.append(
            {
                "ref": ref,
                "cosine_score": score,
                "passes": verdict == "in_scope_candidate",
                "verdict": verdict,
                "threshold": threshold,
                "upstream_hash": upstream_hash,
                "schema_version": SCHEMA_VERSION,
                "producer_version": PRODUCER_VERSION,
            }
        )

    _atomic_write_jsonl(cfg.case_path / "working" / "cosine_prefilter.jsonl", rows)


# ─── helpers ───────────────────────────────────────────────────────


def _load_embeddings(path: Path) -> tuple[list[str], list[list[float]]]:
    """Return parallel lists (refs, vectors) preserving file order."""
    refs: list[str] = []
    vectors: list[list[float]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if "ref" not in row or "embedding" not in row:
            raise DSARPipelineError(f"embeddings.jsonl row missing ref/embedding: {row!r}")
        refs.append(row["ref"])
        vectors.append(list(row["embedding"]))
    return refs, vectors


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity for two equal-length vectors.

    Matches the toolkit's ``dsar_pipeline.embed.cosine`` shape — when
    the toolkit eventually ships ``scope_prefilter.run_for_case``, the
    math here is identical so the cascade hashes won't drift.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(y * y for y in b)) or 1e-12
    return dot / (na * nb)


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    tmp_path = path.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
