"""Operator review for documents that couldn't be ingested.

Lists every source path in ``working/agent01_input.jsonl`` that doesn't
appear in ``working/ingested_items.jsonl`` — those are the files where
extraction failed. The operator gets three actions per file:

- **accept** — record the failure as "tried, can't extract, document the
  exclusion in the disclosure-letter audit trail". The item stays out of
  the disclosure pack with an operator-confirmed rationale.
- **reject** — operator judgment call: this item is outside scope or
  irrelevant; permanently exclude.
- **retry** — re-invoke ``dsar_pipeline.ingest_v3.ingest(path)`` on the
  source file. If it succeeds (e.g. after installing a missing optional
  dep), append a record to ``ingested_items.jsonl`` so the rest of the
  pipeline can pick the file up.

Decisions are appended to ``<case-dir>/audit/unextractable_decisions.jsonl``
— latest decision per ``source_path`` wins on read.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("unextractable")

_DECISION_LOCK = threading.Lock()


@dataclass(frozen=True)
class _CaseShim:
    case_dir: Path

    @property
    def working(self) -> Path:
        return self.case_dir / "working"

    @property
    def audit(self) -> Path:
        return self.case_dir / "audit"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    try:
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def list_unextractable(ctx: _CaseShim) -> list[dict]:
    """Diff input-paths against ingested-items; return one dict per
    unextractable source file:

      {"source_path": str, "filename": str, "extension": str,
       "decision": "pending|accept|reject|retried_ok|retried_fail",
       "decision_note": str, "decision_ts": str | ""}
    """
    input_paths: list[str] = []
    for r in _read_jsonl(ctx.working / "agent01_input.jsonl"):
        p = r.get("path")
        if p:
            input_paths.append(p)

    ingested_paths: set[str] = set()
    for r in _read_jsonl(ctx.working / "ingested_items.jsonl"):
        p = r.get("source_location", {}).get("path")
        if p:
            ingested_paths.add(p)

    decisions = _load_decisions(ctx)
    unextractable = []
    for path in input_paths:
        if path in ingested_paths:
            continue
        d = decisions.get(path, {})
        pp = Path(path)
        unextractable.append(
            {
                "source_path": path,
                "filename": pp.name,
                "extension": pp.suffix.lower(),
                "decision": d.get("decision", "pending"),
                "decision_note": d.get("note", ""),
                "decision_ts": d.get("ts", ""),
            }
        )
    return unextractable


def _decisions_file(ctx: _CaseShim) -> Path:
    return ctx.audit / "unextractable_decisions.jsonl"


def _load_decisions(ctx: _CaseShim) -> dict[str, dict]:
    """Latest decision per source_path."""
    p = _decisions_file(ctx)
    if not p.exists():
        return {}
    by_path: dict[str, dict] = {}
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("source_path"):
                by_path[r["source_path"]] = r
    return by_path


def record_decision(ctx: _CaseShim, *, source_path: str, decision: str, note: str = "") -> dict:
    """Append a new decision row. Accepts ``decision`` in
    {accept, reject, retried_ok, retried_fail, pending}."""
    if decision not in ("accept", "reject", "retried_ok", "retried_fail", "pending"):
        raise ValueError(f"unknown decision: {decision!r}")
    row = {
        "ts": _iso_now(),
        "source_path": source_path,
        "decision": decision,
        "note": note,
    }
    target = _decisions_file(ctx)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _DECISION_LOCK:
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def retry_extract(ctx: _CaseShim, *, source_path: str, case_id: str) -> dict:
    """Re-invoke ingest_v3.ingest on a single source file. If it succeeds,
    append a new record to ingested_items.jsonl and record a retried_ok
    decision. If it fails, record a retried_fail decision with the error.
    """
    try:
        from dsar_pipeline import ingest_v3  # lazy: optional in this venv
        from dsar_pipeline.ingest_v3.source_context import parse_source_context
    except ImportError as exc:
        return {
            "ok": False,
            "error": f"dsar_pipeline not importable: {exc}",
            "error_type": "ImportError",
        }
    src = Path(source_path)
    if not src.exists():
        msg = f"source file does not exist: {src}"
        record_decision(ctx, source_path=source_path, decision="retried_fail", note=msg)
        return {"ok": False, "error": msg, "error_type": "FileNotFoundError"}
    try:
        doc = ingest_v3.ingest(src)
    except Exception as exc:  # noqa: BLE001 — surface any ingest_v3 error
        msg = f"{type(exc).__name__}: {exc}"
        record_decision(ctx, source_path=source_path, decision="retried_fail", note=msg)
        return {"ok": False, "error": msg, "error_type": type(exc).__name__}

    # Build the same item shape agent01 emits (v0.4.5 includes message_id)
    ctx_src = parse_source_context(src)
    item: dict = {
        "case_id": case_id,
        "source_file_id": doc.provenance.sha256,
        "item_id": doc.provenance.sha256,
        "item_type": doc.provenance.extension.lstrip("."),
        "source_location": {
            "path": str(src),
            "size_bytes": doc.provenance.size_bytes,
        },
        "lineage": {
            "ingested_by": "operator_console.retry",
            "ingested_at": doc.provenance.ingested_at.isoformat(),
        },
        "extracted_text_chars": doc.char_count(),
        "yield_ratio": doc.yield_ratio(),
        "source_kind": ctx_src.source_kind,
        "mailbox_owner_email": ctx_src.mailbox_owner_email,
        "mailbox_owner_display": ctx_src.mailbox_owner_display,
        "mailbox_owner_slug": ctx_src.mailbox_owner_slug,
        "message_id": doc.metadata.extras.get("message_id"),
    }
    # Append to ingested_items.jsonl (atomic write per row)
    ingested_path = ctx.working / "ingested_items.jsonl"
    with _DECISION_LOCK:
        with ingested_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    record_decision(
        ctx,
        source_path=source_path,
        decision="retried_ok",
        note=f"chars={doc.char_count()} yield={doc.yield_ratio():.3f}",
    )
    return {"ok": True, "item": item}


def summary_counts(ctx: _CaseShim) -> dict[str, int]:
    """Headline counts for the page header."""
    items = list_unextractable(ctx)
    out = {
        "total": len(items),
        "pending": 0,
        "accept": 0,
        "reject": 0,
        "retried_ok": 0,
        "retried_fail": 0,
    }
    for it in items:
        out[it["decision"]] = out.get(it["decision"], 0) + 1
    # Note retried_ok items DISAPPEAR from list_unextractable (because they
    # now appear in ingested_items.jsonl). To surface the historical count,
    # also count retried_ok decisions in the audit log.
    historical = _load_decisions(ctx)
    out["retried_ok_total"] = sum(
        1 for d in historical.values() if d.get("decision") == "retried_ok"
    )
    return out
