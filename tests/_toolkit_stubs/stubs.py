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


# ─── ingest ────────────────────────────────────────────────────────


def make_ingest_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_pipeline.ingest")

    def run(case_path: Path) -> None:
        src = case_path / "source"
        working = case_path / "working"
        working.mkdir(parents=True, exist_ok=True)

        # Hash the source tree, build a synthetic register.
        pairs = []
        refs = []
        if src.exists():
            for i, p in enumerate(sorted(src.rglob("*"))):
                if p.is_file():
                    rel = str(p.relative_to(src))
                    pairs.append((rel, sha256_file(p)))
                    refs.append(
                        {
                            "ref": f"{case_path.name}-{i + 1:04d}",
                            "text_path": str(p.relative_to(case_path)),
                        }
                    )
        register = {
            "case_no": case_path.name,
            "refs": refs,
            "upstream_hash": hash_pairs(pairs),
        }
        _write_json(working / "register.json", register)

    mod.run = run
    return mod


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


# ─── detect (2.1-2.4 + people_register + scope_prefilter + scope_classify) ──


def make_detect_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_pipeline.detect")

    def run_2_1_to_2_4(case_path: Path) -> None:
        # Read register, write per-ref tag stubs + a manifest.
        # In the toolkit, pii_identification_stage writes <ref>_tags.json
        # files; the stub bundles that here so downstream stub stages
        # (pii_classify) have something to read.
        register = json.loads((case_path / "working" / "register.json").read_text())
        upstream = compute_register_hash(case_path / "working" / "register.json")
        rows = []
        for entry in register["refs"]:
            rows.append(
                {
                    "ref": entry["ref"],
                    "entities": [],
                    "upstream_hash": upstream,
                }
            )
            tags_path = case_path / "working" / f"{entry['ref']}_tags.json"
            _write_json(tags_path, {"ref": entry["ref"], "in_scope": True})
        _write_jsonl(case_path / "working" / "detect_entities.jsonl", rows)

    def run_people_register(case_path: Path) -> None:
        emb_path = case_path / "working" / "embeddings.jsonl"
        upstream = sha256_file(emb_path)
        _write_json(
            case_path / "working" / "person_index.json",
            {"clusters": [], "upstream_hash": upstream},
        )

    def run_scope_prefilter(case_path: Path) -> None:
        emb_path = case_path / "working" / "embeddings.jsonl"
        upstream = sha256_file(emb_path)
        register = json.loads((case_path / "working" / "register.json").read_text())
        rows = []
        for entry in register["refs"]:
            rows.append(
                {
                    "ref": entry["ref"],
                    "cosine_score": 0.5,
                    "passes": True,
                    "upstream_hash": upstream,
                }
            )
        _write_jsonl(case_path / "working" / "cosine_prefilter.jsonl", rows)

    def run_scope_classify(case_path: Path) -> None:
        # Anchor file marking completion.
        cosine_path = case_path / "working" / "cosine_prefilter.jsonl"
        upstream = sha256_file(cosine_path)
        register = json.loads((case_path / "working" / "register.json").read_text())
        # Write per-ref tags.
        for entry in register["refs"]:
            tags_path = case_path / "working" / f"{entry['ref']}_tags.json"
            _write_json(tags_path, {"ref": entry["ref"], "in_scope": True})
        _write_jsonl(
            case_path / "working" / "scope_classify_complete.jsonl",
            [{"completed": True, "upstream_hash": upstream}],
        )

    mod.run_2_1_to_2_4 = run_2_1_to_2_4
    mod.run_people_register = run_people_register
    mod.run_scope_prefilter = run_scope_prefilter
    mod.run_scope_classify = run_scope_classify
    return mod


def make_people_register_stub() -> types.ModuleType:
    """Some callers do `from dsar_pipeline import people_register`."""
    mod = types.ModuleType("dsar_pipeline.people_register")

    def run(case_path: Path) -> None:
        # Delegate to detect_stub's version; just here to satisfy
        # alternative import paths.
        from tests._toolkit_stubs.stubs import make_detect_stub

        make_detect_stub().run_people_register(case_path)

    mod.run = run
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


# ─── redact / export ────────────────────────────────────────────────


def make_redact_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_pipeline.redact")

    def run(
        case_path: Path,
        *,
        prefer_llm_entities: bool = False,
        respect_dispute_halts: bool = True,
    ) -> None:
        # Write some "redacted" output files + a manifest with the
        # upstream_hash so redact_verify can verify it.
        redacted_dir = case_path / "redacted"
        redacted_dir.mkdir(parents=True, exist_ok=True)
        register = json.loads((case_path / "working" / "register.json").read_text())
        for entry in register["refs"]:
            (redacted_dir / f"{entry['ref']}.txt").write_text("[REDACTED]\n")

        # Manifest anchored on tags + pii_collection + enforce flag.
        from dsar_orchestrator.hash_chain import sha256_text

        tags_dir = case_path / "working"
        pairs = []
        for p in sorted(tags_dir.glob("*_tags.json")):
            pairs.append((p.name, sha256_file(p)))
        pii_file = tags_dir / "pii_collection.jsonl"
        pii_hash = sha256_file(pii_file) if pii_file.exists() else ""
        upstream = sha256_text(
            f"{hash_pairs(pairs)}\x1f{pii_hash}\x1fenforce={prefer_llm_entities}"
        )
        _write_json(
            case_path / "working" / "redact_complete.json",
            {"completed": True, "upstream_hash": upstream},
        )

    mod.run = run
    return mod


def make_export_stub() -> types.ModuleType:
    mod = types.ModuleType("dsar_pipeline.export")

    def run(case_path: Path) -> None:
        redacted_dir = case_path / "redacted"
        if not redacted_dir.exists():
            return
        out_dir = case_path / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Just copy each redacted file as the "exported" PDF surrogate.
        for p in redacted_dir.iterdir():
            (out_dir / (p.stem + ".pdf")).write_text(p.read_text())

        # Manifest with upstream = hash of redacted/ tree
        pairs: list[tuple[str, str]] = []
        for p in sorted(redacted_dir.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(redacted_dir))
                pairs.append((rel, sha256_file(p)))
        _write_json(
            out_dir / "manifest.json",
            {"completed": True, "upstream_hash": hash_pairs(pairs)},
        )

    mod.run = run
    return mod


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
        "dsar_pipeline.ingest": make_ingest_stub(),
        "dsar_pipeline.detect": make_detect_stub(),
        "dsar_pipeline.people_register": make_people_register_stub(),
        "dsar_pipeline.redact": make_redact_stub(),
        "dsar_pipeline.export": make_export_stub(),
        # The conductor's embed step calls dsar_clients.tei_embed_client
        # directly (adapter pattern per toolkit issue #1), so we stub
        # the HTTP client rather than the not-yet-shipped dsar_embed.core.
        "dsar_clients.tei_embed_client": make_tei_embed_client_stub(),
        "dsar_rerank.core": make_rerank_core_stub(),
        "dsar_pii_classifier.core": make_pii_classifier_stub(),
        "dsar_pii_discovery.core": make_pii_discovery_stub(),
        "dsar_redact_verify.core": make_redact_verify_stub(),
    }
