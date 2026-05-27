"""#117 — live document-flow funnel + lazy corpus-scale metrics.

Two recompute entry points:

- ``recompute_funnel(case_dir)`` — cheap (<100ms). Re-reads
  ``ingested_items.jsonl``, ``durant_verdicts.jsonl``,
  ``redaction_decisions.jsonl``, ``leak_review_decisions.jsonl``,
  ``qa_decisions.jsonl`` and returns the six headline counts:
  ``ingested``, ``in_scope``, ``redacted``, ``leak_excluded``,
  ``qa_decided``, ``final``. Called from each ``*_decide`` POST route
  after the chain emit so the funnel always reflects current operator
  decision state.

- ``recompute_corpus_scale(case_dir)`` — heavier (word/cell counts off
  the working ``*.txt`` files). Called lazily from ``/pipeline`` render,
  NOT from per-decision routes.

Both write into ``audit/corpus_metrics.json`` (atomic via tmp +
replace). The two sub-trees update independently so a funnel recompute
does not invalidate a recent scale recompute and vice versa.

Failure containment: a failure inside ``recompute_funnel`` MUST NOT
block an operator decision. The console wraps the call in
``_safe_recompute_funnel`` which swallows exceptions and logs them.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("metrics")

_SNAPSHOT_LOCK = threading.Lock()

_SNAPSHOT_PATH = "audit/corpus_metrics.json"
_SCHEMA_VERSION = 1


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    log.debug("metrics: skipping malformed line in %s", path)
                    continue
    except OSError as exc:
        log.warning("metrics: %s unreadable: %s", path, exc)
    return out


def _read_snapshot(case_dir: Path) -> dict:
    path = case_dir / _SNAPSHOT_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("metrics: snapshot at %s unreadable (%s) — starting fresh", path, exc)
        return {}


def _write_snapshot(case_dir: Path, snap: dict) -> None:
    path = case_dir / _SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with _SNAPSHOT_LOCK:
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def recompute_funnel(case_dir: Path) -> dict:
    """Recompute the document-flow funnel from current JSONL state and
    persist a snapshot. Returns the funnel dict.

    Cheap (<100ms): six small file reads plus arithmetic, no LLM, no
    broker. Safe to call from every operator decision route.
    """
    working = case_dir / "working"
    audit = case_dir / "audit"

    ingested = len(_read_jsonl(working / "ingested_items.jsonl"))

    durant = _read_jsonl(working / "durant_verdicts.jsonl")
    in_scope = sum(1 for r in durant if r.get("durant_verdict") == "biographical")

    redaction = _read_jsonl(working / "redaction_decisions.jsonl")
    redacted = sum(1 for r in redaction if r.get("status") == "redacted")

    leak = _read_jsonl(audit / "leak_review_decisions.jsonl")
    leak_excluded = sum(1 for r in leak if r.get("decision") == "accept_exclude")

    qa = _read_jsonl(audit / "qa_decisions.jsonl")
    qa_decided = sum(1 for r in qa if r.get("decision") and r.get("decision") != "pending")

    final = max(0, redacted - leak_excluded)

    funnel = {
        "ingested": ingested,
        "in_scope": in_scope,
        "redacted": redacted,
        "leak_excluded": leak_excluded,
        "qa_decided": qa_decided,
        "final": final,
    }

    snap = _read_snapshot(case_dir)
    snap["schema_version"] = _SCHEMA_VERSION
    snap["funnel"] = funnel
    snap["computed_at"] = _iso_now()
    _write_snapshot(case_dir, snap)

    return funnel


def recompute_corpus_scale(case_dir: Path) -> dict:
    """Recompute the heavier corpus scale (word and document counts) from
    the working ``*.txt`` extracts. Lazy — called from ``/pipeline``
    render, not from per-decision routes."""
    working = case_dir / "working"
    word_count = 0
    doc_count = 0
    if working.exists():
        for p in sorted(working.glob("*.txt")):
            doc_count += 1
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.warning("metrics: %s unreadable for word count: %s", p, exc)
                continue
            word_count += len(text.split())

    scale = {
        "word_count": word_count,
        "doc_count": doc_count,
        "computed_at": _iso_now(),
    }
    snap = _read_snapshot(case_dir)
    snap["schema_version"] = _SCHEMA_VERSION
    snap["scale"] = scale
    if "computed_at" not in snap:
        snap["computed_at"] = scale["computed_at"]
    _write_snapshot(case_dir, snap)
    return scale


def read_metrics_snapshot(case_dir: Path) -> dict | None:
    """Read the persisted ``audit/corpus_metrics.json`` snapshot without
    recomputing. Returns ``None`` if the file doesn't exist."""
    path = case_dir / _SNAPSHOT_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("metrics: snapshot at %s unreadable (%s)", path, exc)
        return None
