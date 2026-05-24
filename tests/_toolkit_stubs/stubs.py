"""Stub module factories — build sys.modules entries that mimic the
toolkit's shape without needing dsar-pipeline installed.

Each factory returns a fake module; ``all_stubs()`` bundles them
into a {module_name: module} dict. Integration fixtures load the
dict and install it in sys.modules per-test.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from dsar_orchestrator.hash_chain import (
    compute_register_hash,
    hash_pairs,
    sha256_file,
    sha256_text,
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


def make_tei_rerank_client_stub() -> types.ModuleType:
    """Stub for `dsar_clients.tei_rerank_client` — the conductor's rerank
    adapter (Contract B / issue #11) calls this directly. Returns
    deterministic per-doc scores so resume-cascade tests stay stable."""
    mod = types.ModuleType("dsar_clients.tei_rerank_client")

    class RerankResult:
        def __init__(self, scores: list[float], *, error: str | None = None) -> None:
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

    def rerank_pairs(
        *,
        query: str,
        docs: list[str],
        timeout_s: int = 30,
        retries: int = 2,
        backoff_s: float = 1.0,
        raw_scores: bool = False,
        tei_url: str = "http://127.0.0.1:8084",
    ) -> RerankResult:
        # Deterministic per-doc score: first byte of UTF-8 as a value
        # in [0, 1). Avoids randomness in tests; doesn't pretend to be
        # meaningful rerank scores.
        scores = [(d.encode("utf-8")[0] if d else 0) / 255.0 for d in docs]
        return RerankResult(scores=scores)

    def health(tei_url: str = "http://127.0.0.1:8084") -> bool:
        return True

    mod.RerankResult = RerankResult
    mod.rerank_pairs = rerank_pairs
    mod.health = health
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


# NB: pii_discovery removed in Contract B / issue #10 (toolkit folded
# the discovery functionality into dsar_pii_classifier.core.discover_case
# which the pii_classify stage already calls; the conductor was duplicate
# work pointed at a fictional dsar_pii_discovery.core).


# NB: There is no `make_redact_stub` or `make_export_stub` anymore.
# The redact + export adapters shell out (to `dsar-redact`, `dsar-bake`,
# `python -m dsar_pipeline.export`) and the integration fixtures
# monkeypatch their subprocess runners directly.


# ─── post_bake_verify ──────────────────────────────────────────────


def make_post_bake_verify_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_pipeline.post_bake_verify")

    class Verdict:
        def __init__(self, case_path: Path) -> None:
            self.all_passed = True
            self.failed_doc_count = 0
            self.failed_verifier_summary = ""
            self.audit_log_path = case_path / "working" / "post_bake_findings.jsonl"

    def verify_for_conductor(case_path: Path) -> Verdict:
        # Stub: write a passing per-finding row to the audit log, matching
        # the real toolkit's post_bake_verify_stage.py row shape
        # (ref/page/gate/severity/issue/evidence/suggested_action/metadata/iteration/ts).
        import datetime

        redacted_dir = case_path / "redacted"
        pairs: list[tuple[str, str]] = []
        for p in sorted(redacted_dir.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(redacted_dir))
                pairs.append((rel, sha256_file(p)))
        upstream = hash_pairs(pairs)
        findings_path = case_path / "working" / "post_bake_findings.jsonl"
        findings_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(findings_path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "ref": "fake-ref-1",
                        "page": None,
                        "gate": "stub",
                        "severity": "low",
                        "issue": "stub verifier passes",
                        "evidence": None,
                        "suggested_action": None,
                        "upstream_hash": upstream,
                        "metadata": {},
                        "iteration": 1,
                        "ts": ts,
                    }
                )
                + "\n"
            )
        return Verdict(case_path)

    mod.verify_for_conductor = verify_for_conductor
    return mod


# ─── verify_spec ────────────────────────────────────────────────────


def make_verify_spec_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_pipeline.verify_spec")

    class Verdict:
        def __init__(self, case_path: Path) -> None:
            self.all_passed = True
            self.failed_doc_count = 0
            self.failed_verifier_summary = ""
            self.audit_log_path = case_path / "working" / "verify_spec_findings.jsonl"

    def verify_for_conductor(case_path: Path) -> Verdict:
        # Stub: write a passing per-finding row to the audit log, matching
        # the real toolkit's verify_spec row shape (check/ref/issue/...).
        import datetime

        working = case_path / "working"
        plan_path = working / "redaction_input.jsonl"
        evidence_path = working / "pii_findings.jsonl"
        # Hash the upstream the way the real verifier reads it (plan + evidence).
        plan_hash = sha256_file(plan_path) if plan_path.exists() else ""
        evidence_hash = sha256_file(evidence_path) if evidence_path.exists() else ""
        upstream = sha256_text(f"{plan_hash}\x1f{evidence_hash}")
        findings_path = working / "verify_spec_findings.jsonl"
        findings_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(findings_path, "a") as f:
            f.write(
                json.dumps(
                    {
                        "check": "stub",
                        "ref": None,
                        "severity": "low",
                        "issue": "stub spec-verifier passes",
                        "evidence": None,
                        "upstream_hash": upstream,
                        "metadata": {},
                        "ts": ts,
                    }
                )
                + "\n"
            )
        return Verdict(case_path)

    mod.verify_for_conductor = verify_for_conductor
    return mod


# ─── registry ───────────────────────────────────────────────────────


def all_stubs() -> dict[str, types.ModuleType]:
    return {
        "dsar_pipeline.people_register": make_people_register_stub(),
        # The conductor's embed step calls dsar_clients.tei_embed_client
        # directly (adapter pattern per toolkit issue #1), so we stub
        # the HTTP client rather than the not-yet-shipped dsar_embed.core.
        "dsar_clients.tei_embed_client": make_tei_embed_client_stub(),
        "dsar_clients.tei_rerank_client": make_tei_rerank_client_stub(),
        "dsar_pii_classifier.core": make_pii_classifier_stub(),
        "dsar_pipeline.verify_spec": make_verify_spec_stub(),
        "dsar_pipeline.post_bake_verify": make_post_bake_verify_stub(),
    }
