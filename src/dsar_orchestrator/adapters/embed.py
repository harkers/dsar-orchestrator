"""Conductor-owned embed adapter — Phase 1, bridge for toolkit issue #1.

Until `dsar_embed.core.embed_corpus(case_path)` lands in the toolkit
(per `harkers/dsar-toolkit#1`), the conductor handles the corpus-
level embed flow itself: reads `working/register.json`, batches the
raw text per ref, calls `dsar_clients.tei_embed_client.embed()` as
the HTTP leaf, writes `working/embeddings.jsonl` with the proper
`upstream_hash` + schema/producer versioning.

**Retirement contract.** When the toolkit ships its `dsar_embed.core`
module, this adapter is deleted; `pipeline._run_embed` switches its
import to `dsar_embed.core.embed_corpus`. The output JSONL shape
matches what the toolkit will eventually write, so downstream
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
from dsar_orchestrator.hash_chain import compute_register_hash

PRODUCER_VERSION = "dsar_orchestrator.adapters.embed 0.4.9"
SCHEMA_VERSION = "1.0"


# ─── injectable HTTP-client protocol ────────────────────────────────


class _EmbedResultLike(Protocol):
    """Duck-type for `dsar_clients.tei_embed_client.EmbedResult`.

    Pulled out as a Protocol so tests can pass a simple object; we
    don't need to import the real dataclass at type-check time.
    """

    vectors: list[list[float]]
    model_alias: str
    resolved_model: str
    endpoint_url: str
    model_revision: str
    latency_s: float
    error: str | None

    def as_audit_fields(self) -> dict[str, str | float]: ...


EmbedFn = Callable[[list[str]], _EmbedResultLike]


def _default_embedder() -> EmbedFn:
    """Resolve the live `tei_embed_client.embed` callable lazily.

    Importing at call time keeps the conductor installable without
    `dsar-pipeline` (`dsar-toolkit`) on the path; if the operator
    actually runs a real case, the toolkit must be installed.
    """
    try:
        mod = importlib.import_module("dsar_clients.tei_embed_client")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_clients.tei_embed_client is not installed. The "
            "conductor's embed adapter needs it to call TEI :8085. "
            "Install dsar-toolkit (pip install -e ~/projects/"
            "dsar-toolkit/) and retry."
        ) from exc
    return mod.embed


# ─── public entry ──────────────────────────────────────────────────


def run_for_case(cfg: CaseConfig, *, embedder: EmbedFn | None = None) -> None:
    """Embed every ref under `cfg.case_path` and write
    `working/embeddings.jsonl`.

    `embedder` is injectable for tests; in production the default
    resolves to `dsar_clients.tei_embed_client.embed`.
    """
    if embedder is None:
        embedder = _default_embedder()

    register_path = cfg.case_path / "working" / "register.json"
    if not register_path.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: ingest output missing at {register_path}. "
            f"Run ingest first: dsar-conductor --case {cfg.case_no} "
            f"--only ingest"
        )

    from dsar_orchestrator.hash_chain import read_register

    refs = read_register(register_path)
    if not refs:
        raise DSARPipelineError(
            f"case={cfg.case_no}: register has no refs. Confirm source/ "
            f"has documents and re-run ingest."
        )

    texts = _load_texts(cfg.case_path, refs)
    upstream_hash = compute_register_hash(register_path)
    result = embedder(texts)

    if result.error:
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI embed failed: {result.error}. "
            f"Check that TEI is running at {result.endpoint_url}."
        )
    if len(result.vectors) != len(refs):
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI returned {len(result.vectors)} "
            f"vectors for {len(refs)} refs (mismatch)."
        )
    if any(not v for v in result.vectors):
        raise DSARPipelineError(f"case={cfg.case_no}: TEI returned at least one empty vector.")

    _write_embeddings_jsonl(cfg.case_path, refs, result, upstream_hash)


def _load_texts(case_path: Path, refs: list[dict]) -> list[str]:
    """Read extracted text per ref in the order given. Per Contract A
    (issue #8) the toolkit writes extracted text to working/<ref>.txt;
    we derive the path from the entry's ``ref`` field. Bytes are
    decoded as UTF-8 with replacement to keep one bad byte from killing
    a 100-doc run; the embedding step is a transform, not a validator."""
    from dsar_orchestrator.hash_chain import text_path_for_ref

    texts: list[str] = []
    for entry in refs:
        ref = entry.get("ref")
        if not ref:
            raise DSARPipelineError(f"register entry missing ref: {entry!r}")
        text_path = text_path_for_ref(case_path, ref)
        if not text_path.exists():
            raise DSARPipelineError(f"missing text file for ref={ref}: {text_path}")
        texts.append(text_path.read_text(encoding="utf-8", errors="replace"))
    return texts


def _write_embeddings_jsonl(
    case_path: Path,
    refs: list[dict],
    result: _EmbedResultLike,
    upstream_hash: str,
) -> None:
    """Write the per-ref rows with the shape `dsar_embed.core` will
    eventually write. Atomic temp-file + rename + fsync per the
    Operational semantics in spec v3."""
    out_path = case_path / "working" / "embeddings.jsonl"
    tmp_path = out_path.with_suffix(".jsonl.tmp")
    audit = result.as_audit_fields()

    with open(tmp_path, "w", encoding="utf-8") as f:
        for entry, vector in zip(refs, result.vectors, strict=True):
            row = {
                "ref": entry["ref"],
                "embedding": vector,
                "upstream_hash": upstream_hash,
                "schema_version": SCHEMA_VERSION,
                "producer_version": PRODUCER_VERSION,
                "latency_s": result.latency_s,
                **audit,
            }
            f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
