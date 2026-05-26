"""30-doc QA sampling — risk-weighted stratified sample of redacted
documents for operator review.

Per the v3 jury synthesis (writer + code-qwen25 consensus, with chat's
"stratified covers boring middle" dissent noted): 30 docs split into
three buckets — 10 high-risk + 10 medium + 10 random.

The QA stage doesn't pass until every sampled doc has a final operator
decision (approve / request re-redaction / mark false positive /
escalate). Decisions go through the same chain-first audit pipeline as
the other decision sites (see audit_chain.py).
"""

from __future__ import annotations

import json
import logging
import random
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("qa-sample")

_SAMPLE_LOCK = threading.Lock()

# Per chat-jury synthesis: 10 + 10 + 10 split.
_BUCKET_SIZE = 10
DEFAULT_SAMPLE_SIZE = _BUCKET_SIZE * 3  # 30

_DECISIONS = (
    "pending",
    "approve",
    "request_reredaction",
    "mark_false_positive",
    "mark_missed_redaction",
    "escalate",
)


@dataclass(frozen=True)
class _QASampleDoc:
    doc_ref: str
    filename: str
    bucket: str  # "high" | "medium" | "random"
    entity_count: int
    redact_count: int


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sample_path(case_dir: Path) -> Path:
    return case_dir / "audit" / "qa_sample.jsonl"


def _decisions_path(case_dir: Path) -> Path:
    return case_dir / "audit" / "qa_decisions.jsonl"


def _load_redacted_docs(case_dir: Path) -> list[dict]:
    """Read working/redaction_decisions.jsonl, enrich with entity_count
    from the per-doc tags file, return only status=redacted rows."""
    p = case_dir / "working" / "redaction_decisions.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("skipping malformed redaction_decisions row: %s", exc)
            continue
        if row.get("status") != "redacted":
            continue
        ref = row.get("doc_ref", "")
        if not ref:
            continue
        entity_count = int(row.get("redaction_count", 0) or 0)
        tags_path = case_dir / "working" / f"{ref}_tags.json"
        if tags_path.exists():
            try:
                tags = json.loads(tags_path.read_text(encoding="utf-8"))
                entity_count = int(tags.get("entity_count", entity_count))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("tags file %s unreadable: %s", tags_path, exc)
        out.append(
            {
                "doc_ref": ref,
                "filename": row.get("filename", ref),
                "entity_count": entity_count,
                "redact_count": int(row.get("redaction_count", entity_count) or 0),
            }
        )
    return out


def _stratify(docs: list[dict], *, size: int, seed: int) -> list[_QASampleDoc]:
    """Split docs into high / medium / random buckets per chat-jury synthesis.

    high: top by entity_count
    medium: docs around the median
    random: uniform draw from the rest
    """
    if not docs:
        return []
    if len(docs) <= size:
        # Tiny corpus — return everything as 'random'
        return [
            _QASampleDoc(
                doc_ref=d["doc_ref"],
                filename=d["filename"],
                bucket="random",
                entity_count=d["entity_count"],
                redact_count=d["redact_count"],
            )
            for d in docs
        ]
    rng = random.Random(seed)
    by_entity = sorted(docs, key=lambda d: d["entity_count"], reverse=True)
    per_bucket = size // 3
    high = by_entity[:per_bucket]
    high_refs = {d["doc_ref"] for d in high}
    # Medium: take a contiguous slice around the median that excludes high
    remainder = [d for d in by_entity if d["doc_ref"] not in high_refs]
    mid_start = max(0, len(remainder) // 2 - per_bucket // 2)
    medium = remainder[mid_start : mid_start + per_bucket]
    med_refs = {d["doc_ref"] for d in medium}
    # Random: from everything not yet picked
    pool = [d for d in docs if d["doc_ref"] not in high_refs and d["doc_ref"] not in med_refs]
    rand = rng.sample(pool, min(per_bucket, len(pool)))
    out: list[_QASampleDoc] = []
    for d in high:
        out.append(
            _QASampleDoc(
                doc_ref=d["doc_ref"],
                filename=d["filename"],
                bucket="high",
                entity_count=d["entity_count"],
                redact_count=d["redact_count"],
            )
        )
    for d in medium:
        out.append(
            _QASampleDoc(
                doc_ref=d["doc_ref"],
                filename=d["filename"],
                bucket="medium",
                entity_count=d["entity_count"],
                redact_count=d["redact_count"],
            )
        )
    for d in rand:
        out.append(
            _QASampleDoc(
                doc_ref=d["doc_ref"],
                filename=d["filename"],
                bucket="random",
                entity_count=d["entity_count"],
                redact_count=d["redact_count"],
            )
        )
    return out


def sample_for_qa(
    case_dir: Path,
    *,
    size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 42,
    force: bool = False,
) -> list[dict]:
    """Return the QA sample, creating + persisting it on first call.
    Pass ``force=True`` to discard the persisted sample and re-pick.
    """
    sp = _sample_path(case_dir)
    with _SAMPLE_LOCK:
        if sp.exists() and not force:
            return [json.loads(line) for line in sp.read_text().splitlines() if line.strip()]
        docs = _load_redacted_docs(case_dir)
        sample = _stratify(docs, size=size, seed=seed)
        sp.parent.mkdir(parents=True, exist_ok=True)
        with sp.open("w", encoding="utf-8") as f:
            for s in sample:
                f.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
        return [asdict(s) for s in sample]


def _load_decisions(case_dir: Path) -> dict[str, dict]:
    """Latest decision per doc_ref."""
    p = _decisions_path(case_dir)
    if not p.exists():
        return {}
    by_ref: dict[str, dict] = {}
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("skipping malformed qa_decisions row: %s", exc)
            continue
        if r.get("doc_ref"):
            by_ref[r["doc_ref"]] = r
    return by_ref


def list_qa_sample(case_dir: Path) -> list[dict]:
    """Sample rows enriched with the latest decision per doc_ref."""
    sample = sample_for_qa(case_dir)
    decisions = _load_decisions(case_dir)
    out: list[dict] = []
    for s in sample:
        d = decisions.get(s["doc_ref"], {})
        out.append(
            {
                **s,
                "decision": d.get("decision", "pending"),
                "reason_code": d.get("reason_code", ""),
                "note": d.get("note", ""),
                "ts": d.get("ts", ""),
            }
        )
    return out


def record_qa_decision(
    case_dir: Path,
    *,
    doc_ref: str,
    decision: str,
    reason_code: str,
    note: str = "",
) -> dict:
    """Record an operator QA decision for one sampled doc.

    Chain-first: emits a hash-chained REVIEWER_DECISION_MADE event,
    then appends to the user-visible qa_decisions.jsonl.
    """
    if decision not in _DECISIONS:
        raise ValueError(f"unknown qa decision {decision!r}; pick one of {list(_DECISIONS)}")
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    validate_reason_code(reason_code, note)
    row = {
        "ts": _iso_now(),
        "doc_ref": doc_ref,
        "decision": decision,
        "reason_code": reason_code,
        "note": note,
    }
    from dsar_orchestrator.local_broker.audit_chain import emit_for_case_dir

    emit_for_case_dir(
        case_dir,
        decision_kind="qa_sample",
        payload=row,
        item_id=doc_ref,
    )
    target = _decisions_path(case_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _SAMPLE_LOCK:
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def qa_sample_complete(case_dir: Path) -> bool:
    """True iff every sampled doc has a non-pending decision."""
    if not _sample_path(case_dir).exists():
        return False
    rows = list_qa_sample(case_dir)
    return bool(rows) and all(r["decision"] != "pending" for r in rows)


def summary_counts(case_dir: Path) -> dict[str, int]:
    if not _sample_path(case_dir).exists():
        return {"total": 0}
    rows = list_qa_sample(case_dir)
    out: dict[str, int] = {"total": len(rows)}
    for d in _DECISIONS:
        out[d] = sum(1 for r in rows if r["decision"] == d)
    return out
