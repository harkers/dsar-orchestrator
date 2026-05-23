"""Stub module factories — build sys.modules entries that mimic the
toolkit's shape without needing dsar-pipeline installed.

Each factory returns a (module_name, ModuleStub) tuple. The
``install_toolkit_stubs`` fixture in conftest.py registers them all.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from dsar_orchestrator.hash_chain import (
    compute_register_hash,
    hash_pairs,
    sha256_file,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))


def _read_case_scope(case_path: Path) -> str:
    """Read case_scope from case_config.json, falling back to the
    pre-synthesis default so old fixtures keep working."""
    cfg_path = case_path / "case_config.json"
    if not cfg_path.exists():
        return "test scope"
    try:
        return json.loads(cfg_path.read_text()).get("case_scope", "test scope")
    except json.JSONDecodeError:
        return "test scope"


# NB: There is no `make_ingest_stub` anymore — the ingest adapter
# shells out to `python -m dsar_pipeline.ingest` and the integration
# fixtures monkeypatch its subprocess runner directly.


# ─── embed ──────────────────────────────────────────────────────────


def make_tei_embed_client_stub() -> types.ModuleType:
    """Stub for `dsar_clients.tei_embed_client` — the conductor's embed
    adapter (per toolkit issue #1) calls this directly. Returns
    deterministic 1024-d vectors so resume-cascade tests stay stable."""
    mod = types.ModuleType("dsar_clients.tei_embed_client")

    class EmbedResult:
        def __init__(
            self,
            vectors: list[list[float]],
            *,
            error: str | None = None,
        ) -> None:
            self.vectors = vectors
            self.model_alias = "embed"
            self.resolved_model = "BAAI/bge-m3"
            self.endpoint_url = "http://127.0.0.1:8085"
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

    def embed(
        texts: list[str],
        *,
        timeout_s: int = 30,
        retries: int = 2,
        backoff_s: float = 1.0,
        tei_url: str = "http://127.0.0.1:8085",
    ) -> EmbedResult:
        # Deterministic per-text vector: first byte of UTF-8 as a
        # repeating value, padded to 1024 dims. Avoids randomness in
        # tests; doesn't pretend to be meaningful embeddings.
        vectors: list[list[float]] = []
        for text in texts:
            seed = (text.encode("utf-8")[0] if text else 0) / 255.0
            vectors.append([seed] * 1024)
        return EmbedResult(vectors=vectors)

    def health(tei_url: str = "http://127.0.0.1:8085") -> bool:
        return True

    mod.EmbedResult = EmbedResult
    mod.embed = embed
    mod.health = health
    return mod


# NB: There is no make_detect_stub anymore. The detect /
# scope_prefilter / scope_classify adapters all bypass
# dsar_pipeline.detect (subprocess CLI or direct TEI client), and
# people_register has its own toolkit module.


def make_people_register_stub() -> types.ModuleType:
    """Stub for the conductor's people_register adapter, which calls
    ``dsar_pipeline.people_register.build_people_register(working_dir)``."""
    mod = types.ModuleType("dsar_pipeline.people_register")

    def build_people_register(working_dir: Path) -> dict:
        # Minimal toolkit-shaped result; the adapter wraps this into
        # working/person_index.json with the cascade fields.
        return {
            "clusters": [],
            "alias_to_id": {},
            "threshold": 0.75,
            "model": "stub-model",
            "generated_at": "2026-05-23T00:00:00Z",
        }

    mod.build_people_register = build_people_register
    return mod


# ─── rerank ─────────────────────────────────────────────────────────


def make_rerank_core_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_rerank.core")

    def rerank_case(
        case_path: Path,
        *,
        mode: str = "shadow",
        threshold: float = 0.01,
        top_n: int = 20,
        sample_rate: float = 0.05,
    ) -> None:
        cosine_path = case_path / "working" / "cosine_prefilter.jsonl"
        from dsar_orchestrator.hash_chain import sha256_text

        cosine_hash = sha256_file(cosine_path)
        upstream = sha256_text(
            f"{cosine_hash}\x1f{_read_case_scope(case_path)}\x1f"
            f"thr={threshold}\x1ftopN={top_n}\x1fmode={mode}"
        )
        rows = []
        cosine_rows = [
            json.loads(line) for line in cosine_path.read_text().splitlines() if line.strip()
        ]
        for r in cosine_rows:
            rows.append(
                {
                    "ref": r["ref"],
                    "rerank_score": 0.5,
                    "would_drop": False,
                    "mode": mode,
                    "upstream_hash": upstream,
                }
            )
        _write_jsonl(case_path / "working" / "scope_rerank.jsonl", rows)

    mod.rerank_case = rerank_case
    return mod


# ─── pii_classifier ─────────────────────────────────────────────────


def make_pii_classifier_stub() -> types.ModuleType:
    """Stub for the toolkit's `dsar_pii_classifier.core`.

    The conductor's pii-classify adapter calls `discover_case(case_dir,
    mode=...)` and gets back `{stage_no: [Finding, ...]}`. The adapter
    then aggregates findings by ref and writes pii_collection.jsonl.

    Stub returns one Finding per ref (one per *_tags.json file present
    in working/) so the adapter's aggregation has something to produce.
    """
    mod = types.ModuleType("dsar_pii_classifier.core")

    def discover_case(
        case_path: Path,
        *,
        stages: tuple[int, ...] = (1, 2, 3),
        plan=None,
        mode: str = "shadow",
    ) -> dict[int, list[dict]]:
        tags_dir = case_path / "working"
        findings_by_stage: dict[int, list[dict]] = {s: [] for s in stages}
        for p in sorted(tags_dir.glob("*_tags.json")):
            ref = json.loads(p.read_text())["ref"]
            # Drop one stub finding per ref into the first requested stage.
            findings_by_stage[stages[0]].append(
                {
                    "ref": ref,
                    "surface": "James Carter",
                    "type": "subject_name",
                    "detector": "stub-detector",
                    "confidence": 0.99,
                }
            )
        return findings_by_stage

    mod.discover_case = discover_case
    return mod


# ─── pii_discovery ──────────────────────────────────────────────────


def make_pii_discovery_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_pii_discovery.core")

    def discover_entities(case_path: Path) -> None:
        from dsar_orchestrator.hash_chain import sha256_text

        register_path = case_path / "working" / "register.json"
        register_hash = compute_register_hash(register_path)
        upstream = sha256_text(f"{register_hash}\x1f{_read_case_scope(case_path)}")
        register = json.loads(register_path.read_text())
        rows = []
        for entry in register["refs"]:
            rows.append(
                {
                    "ref": entry["ref"],
                    "entities": [],
                    "upstream_hash": upstream,
                }
            )
        _write_jsonl(case_path / "working" / "pii_discovery.jsonl", rows)

    mod.discover_entities = discover_entities
    return mod


# NB: There is no `make_redact_stub` or `make_export_stub` anymore.
# The redact + export adapters shell out (to `dsar-redact`, `dsar-bake`,
# `python -m dsar_pipeline.export`) and the integration fixtures
# monkeypatch their subprocess runners directly.


# ─── redact_verify ─────────────────────────────────────────────────


def make_redact_verify_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_redact_verify.core")

    class Verdict:
        def __init__(self) -> None:
            self.all_passed = True
            self.failed_doc_count = 0
            self.failed_verifier_summary = ""

    def verify_case(case_path: Path) -> Verdict:
        # Stub: write a passing verdict to the audit log.

        redacted_dir = case_path / "redacted"
        pairs: list[tuple[str, str]] = []
        for p in sorted(redacted_dir.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(redacted_dir))
                pairs.append((rel, sha256_file(p)))
        upstream = hash_pairs(pairs)
        audit_path = Path.home() / ".dsar-audit" / case_path.name / "redact_verify.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "event": "verify_complete",
                        "passed": True,
                        "upstream_hash": upstream,
                    }
                )
                + "\n"
            )
        return Verdict()

    mod.verify_case = verify_case
    return mod


# ─── registry ───────────────────────────────────────────────────────


def all_stubs() -> dict[str, types.ModuleType]:
    return {
        "dsar_pipeline.people_register": make_people_register_stub(),
        # The conductor's embed step calls dsar_clients.tei_embed_client
        # directly (adapter pattern per toolkit issue #1), so we stub
        # the HTTP client rather than the not-yet-shipped dsar_embed.core.
        "dsar_clients.tei_embed_client": make_tei_embed_client_stub(),
        "dsar_rerank.core": make_rerank_core_stub(),
        "dsar_pii_classifier.core": make_pii_classifier_stub(),
        "dsar_pii_discovery.core": make_pii_discovery_stub(),
        "dsar_redact_verify.core": make_redact_verify_stub(),
    }
