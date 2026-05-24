"""Per-stage artefact registry — drives the resume cascade.

Each toolkit-side sub-stage maps to (a) the artefact it writes under
``<case>/working/`` (or another well-known path) and (b) a function
that computes the **current** upstream hash from the case directory.

The orchestrator's ``build_stage_plan()`` walks this registry: for
every stage, if the artefact is present AND its recorded
``upstream_hash`` matches the current upstream, the stage is skipped.
Otherwise it's added to the run list.

This module is a leaf. It imports from ``hash_chain`` + ``config``
+ ``exceptions`` only — no upward dependencies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import UpstreamHashMismatch
from dsar_orchestrator.hash_chain import (
    compute_register_hash,
    hash_pairs,
    sha256_file,
    sha256_text,
    verify_upstream,
)

UpstreamFn = Callable[[CaseConfig], str]


@dataclass(frozen=True)
class StageArtefact:
    """Per-sub-stage artefact + upstream description.

    ``artefact_relpath`` is relative to the case directory unless it
    begins with ``~`` (then it's resolved against ``$HOME``).
    ``None`` means the stage writes a directory tree (e.g., ``redacted/``)
    rather than a single tracked file — in that case the cascade falls
    back to a "directory exists + non-empty" presence check only.
    """

    sub_stage: str
    coarse_stage: str
    artefact_relpath: str | None
    upstream_fn: UpstreamFn


# ─── upstream-hash computers ─────────────────────────────────────


def _hash_source_tree(cfg: CaseConfig) -> str:
    """Upstream for ingest: every file under ``<case>/source/`` keyed
    by relative path."""
    source_dir = cfg.case_path / "source"
    if not source_dir.exists():
        return ""
    pairs: list[tuple[str, str]] = []
    for p in sorted(source_dir.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(source_dir))
            pairs.append((rel, sha256_file(p)))
    return hash_pairs(pairs)


def _hash_register(cfg: CaseConfig) -> str:
    """Upstream for ``embed`` + ``detect_2_1_to_2_4`` + ``pii_discovery``:
    the register + raw text per ref."""
    return compute_register_hash(cfg.case_path / "working" / "register.json")


def _hash_register_plus_scope(cfg: CaseConfig) -> str:
    """Upstream that combines register hash + the case scope string +
    the discovery model revision (when known)."""
    register = _hash_register(cfg)
    return sha256_text(register + "\x1f" + cfg.case_scope)


def _hash_embeddings(cfg: CaseConfig) -> str:
    p = cfg.case_path / "working" / "embeddings.jsonl"
    return sha256_file(p) if p.exists() else ""


def _hash_cosine_plus_scope(cfg: CaseConfig) -> str:
    p = cfg.case_path / "working" / "cosine_prefilter.jsonl"
    cosine = sha256_file(p) if p.exists() else ""
    return sha256_text(
        f"{cosine}\x1f{cfg.case_scope}\x1f"
        f"thr={cfg.rerank_threshold}\x1f"
        f"topN={cfg.rerank_top_n}\x1f"
        f"mode={cfg.rerank_mode}"
    )


def _hash_scope_inputs(cfg: CaseConfig) -> str:
    """Upstream for ``scope_classify``: the upstream set actually fed
    to the LLM. In shadow it's the cosine prefilter; in enforce it's
    the rerank output."""
    if cfg.rerank_mode == "enforce":
        p = cfg.case_path / "working" / "scope_rerank.jsonl"
    else:
        p = cfg.case_path / "working" / "cosine_prefilter.jsonl"
    return sha256_file(p) if p.exists() else ""


def _hash_pii_classify_inputs(cfg: CaseConfig) -> str:
    """Upstream for ``pii_classify``: the tags + subject_identifier +
    PII mode."""
    tags_dir = cfg.case_path / "working"
    pairs = []
    if tags_dir.exists():
        for p in sorted(tags_dir.glob("*_tags.json")):
            pairs.append((p.name, sha256_file(p)))
    tags_hash = hash_pairs(pairs) if pairs else ""
    subj = cfg.subject_identifier.primary_name if cfg.subject_identifier else ""
    return sha256_text(f"{tags_hash}\x1f{subj}\x1fmode={cfg.pii_classify_mode}")


def _hash_redact_inputs(cfg: CaseConfig) -> str:
    """Upstream for ``redact``: tags + (in enforce mode) pii_collection."""
    tags_dir = cfg.case_path / "working"
    pairs = []
    if tags_dir.exists():
        for p in sorted(tags_dir.glob("*_tags.json")):
            pairs.append((p.name, sha256_file(p)))
    pii_file = tags_dir / "pii_collection.jsonl"
    pii_hash = sha256_file(pii_file) if pii_file.exists() else ""
    return sha256_text(
        f"{hash_pairs(pairs)}\x1f{pii_hash}\x1fenforce={cfg.pii_classify_mode == 'enforce'}"
    )


def _hash_bake_inputs(cfg: CaseConfig) -> str:
    """Upstream for ``bake``: the redaction plan written by the redact
    stage (``working/redaction_input.jsonl``). Matches the hash the
    bake adapter records in ``bake_manifest.json``."""
    p = cfg.case_path / "working" / "redaction_input.jsonl"
    return sha256_file(p) if p.exists() else ""


def _hash_verify_spec_inputs(cfg: CaseConfig) -> str:
    """Upstream for ``verify_spec``: the redaction plan + the upstream
    PII evidence (what verify_spec reads). When either input changes,
    the verifier must re-run."""
    working = cfg.case_path / "working"
    plan = working / "redaction_input.jsonl"
    evidence = working / "pii_findings.jsonl"
    plan_hash = sha256_file(plan) if plan.exists() else ""
    evidence_hash = sha256_file(evidence) if evidence.exists() else ""
    return sha256_text(f"{plan_hash}\x1f{evidence_hash}")


def _hash_redacted_dir(cfg: CaseConfig) -> str:
    """Upstream for ``redact_verify``: every file under ``<case>/redacted/``."""
    redacted_dir = cfg.case_path / "redacted"
    if not redacted_dir.exists():
        return ""
    pairs: list[tuple[str, str]] = []
    for p in sorted(redacted_dir.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(redacted_dir))
            pairs.append((rel, sha256_file(p)))
    return hash_pairs(pairs)


# ─── the registry ─────────────────────────────────────────────────

STAGE_ARTEFACTS: dict[str, StageArtefact] = {
    # Cascade anchor for ingest: the conductor-owned meta sidecar (per
    # Contract A / issue #8 — register.json is toolkit-owned and doesn't
    # carry conductor metadata anymore).
    "ingest": StageArtefact("ingest", "ingest", "working/register_meta.json", _hash_source_tree),
    # Stage 2 parallel branches — each writes its own artefact.
    "embed": StageArtefact("embed", "stage_2_parallel", "working/embeddings.jsonl", _hash_register),
    "detect_2_1_to_2_4": StageArtefact(
        "detect_2_1_to_2_4",
        "stage_2_parallel",
        "working/detect_entities.jsonl",
        _hash_register,
    ),
    "pii_discovery": StageArtefact(
        "pii_discovery",
        "stage_2_parallel",
        "working/pii_discovery.jsonl",
        _hash_register_plus_scope,
    ),
    # Stage 3 parallel branches.
    "people_register": StageArtefact(
        "people_register",
        "stage_3_parallel",
        "working/person_index.json",
        _hash_embeddings,
    ),
    "scope_prefilter": StageArtefact(
        "scope_prefilter",
        "stage_3_parallel",
        "working/cosine_prefilter.jsonl",
        _hash_embeddings,
    ),
    "rerank": StageArtefact(
        "rerank",
        "stage_3_parallel",
        "working/scope_rerank.jsonl",
        _hash_cosine_plus_scope,
    ),
    "scope_classify": StageArtefact(
        "scope_classify",
        "scope_classify",
        # Per-doc tags are the actual scope-classify output; we use a
        # sentinel file (written by the scope-classify stage when it
        # finishes) to anchor the hash chain.
        "working/scope_classify_complete.jsonl",
        _hash_scope_inputs,
    ),
    "pii_classify": StageArtefact(
        "pii_classify",
        "pii_classify",
        "working/pii_collection.jsonl",
        _hash_pii_classify_inputs,
    ),
    # ``redact`` produces a directory; we anchor on a manifest file the
    # toolkit's redact stage will write to mark completion.
    "redact": StageArtefact(
        "redact",
        "redact",
        "working/redact_complete.json",
        _hash_redact_inputs,
    ),
    "verify_spec": StageArtefact(
        "verify_spec",
        "verify_spec",
        "working/verify_spec_findings.jsonl",  # toolkit-owned
        _hash_verify_spec_inputs,
    ),
    "bake": StageArtefact(
        "bake",
        "bake",
        "working/redact_v4/bake_manifest.json",
        _hash_bake_inputs,
    ),
    "verify_pdf": StageArtefact(
        "verify_pdf",
        "verify_pdf",
        "working/post_bake_findings.jsonl",  # toolkit-owned; was ~/.dsar-audit/<case>/redact_verify.jsonl
        _hash_redacted_dir,
    ),
    # Export produces output/ — anchored on a manifest similarly.
    "export": StageArtefact(
        "export",
        "export",
        "output/manifest.json",
        _hash_redacted_dir,
    ),
}


def resolve_artefact_path(case_no: str, case_path: Path, relpath: str) -> Path:
    """Translate the registry's ``artefact_relpath`` into an absolute
    path. Handles ``~`` and ``<case>`` substitution."""
    if relpath.startswith("~"):
        # ~/.dsar-audit/<case>/<...>
        expanded = relpath.replace("<case>", case_no)
        return Path(expanded).expanduser()
    return case_path / relpath


def is_artefact_fresh(cfg: CaseConfig, art: StageArtefact) -> tuple[bool, str]:
    """Return (is_fresh, reason).

    Fresh = artefact exists AND its recorded upstream_hash matches the
    current upstream state. Used by ``build_stage_plan`` to decide
    whether to skip a stage.
    """
    if art.artefact_relpath is None:
        # No tracked artefact — never claim fresh (always run).
        return False, "no tracked artefact"
    path = resolve_artefact_path(cfg.case_no, cfg.case_path, art.artefact_relpath)
    if not path.exists():
        return False, "artefact missing"
    try:
        current = art.upstream_fn(cfg)
    except FileNotFoundError as e:
        return False, f"upstream missing: {e}"
    if current == "":
        # Upstream computation couldn't produce a non-empty hash —
        # treat as "we can't tell"; safer to re-run.
        return False, "upstream empty"
    try:
        verify_upstream(path, current)
    except UpstreamHashMismatch as e:
        return False, f"hash mismatch: {e}"
    return True, "artefact present + hash matches"
