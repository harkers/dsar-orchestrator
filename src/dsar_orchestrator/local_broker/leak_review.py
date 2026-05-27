"""Operator review for documents that failed leak-detection in stage 7.

agent06_redaction marks a doc ``status=failed`` when
``verify_redacted_artifact`` finds entity text still appearing in the
redacted output after redaction was applied. v0.4.4 (#148) added
``subject_protected_phrases`` honouring; the remaining failures are
genuinely tricky cases the operator must triage per document.

Each row gets four actions:

- **accept_exclude** — record "tried, couldn't safely redact, exclude
  from disclosure pack with documented exemption rationale".
- **include_with_note** — keep in disclosure pack with an operator-
  added rationale (e.g. "leak text is the requester's own role
  description, preserved as personal data of the subject").
- **retry** — re-invoke ``redact_document`` on this doc. Useful after
  the operator has manually edited ``<ref>_tags.json`` (e.g. forced
  ``redact=False`` on a flagged entity) or added the leaking term to
  ``data_subject.json::subject_protected_phrases``.
- **manual_fix_done** — operator has manually edited the file in
  ``redacted/`` outside the console; mark as resolved.

Decisions append to ``<case-dir>/audit/leak_review_decisions.jsonl``.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("leak-review")

_DECISION_LOCK = threading.Lock()

DECISIONS = (
    "pending",
    "accept_exclude",
    "include_with_note",
    "retried_ok",
    "retried_fail",
    "manual_fix_done",
)


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


def _decisions_file(ctx: _CaseShim) -> Path:
    return ctx.audit / "leak_review_decisions.jsonl"


def _load_decisions(ctx: _CaseShim) -> dict[str, dict]:
    p = _decisions_file(ctx)
    if not p.exists():
        return {}
    by_ref: dict[str, dict] = {}
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("doc_ref"):
                by_ref[r["doc_ref"]] = r
    return by_ref


def list_leaks(ctx: _CaseShim) -> list[dict]:
    """Return one dict per failed-redaction doc with operator-review
    context: ref, filename, leak terms, status, decision."""
    decisions = _load_decisions(ctx)
    out = []
    for row in _read_jsonl(ctx.working / "redaction_decisions.jsonl"):
        if row.get("status") != "failed":
            continue
        ref = row.get("doc_ref", "")
        # leakcheck JSON written by redact.py:1918 for failed docs
        leakcheck_path = ctx.working / f"{ref}_leakcheck.json"
        leaks: list[str] = []
        leak_checked_at = ""
        if leakcheck_path.exists():
            try:
                lc = json.loads(leakcheck_path.read_text())
                leaks = lc.get("leaks", []) or []
                leak_checked_at = lc.get("checked_at", "")
            except (OSError, json.JSONDecodeError):
                pass
        # tags file for context
        tags_path = ctx.working / f"{ref}_tags.json"
        entity_count = 0
        redact_count = 0
        if tags_path.exists():
            try:
                t = json.loads(tags_path.read_text())
                entity_count = t.get("entity_count", 0)
                redact_count = t.get("redact_count", 0)
            except (OSError, json.JSONDecodeError):
                pass
        d = decisions.get(ref, {})
        out.append(
            {
                "doc_ref": ref,
                "filename": row.get("filename", ""),
                "leaks_count": len(leaks),
                "leaks_sample": leaks[:8],  # top 8 unique-ish for display
                "leaks_all_distinct": sorted(set(leaks)),
                "leak_checked_at": leak_checked_at,
                "entity_count": entity_count,
                "redact_count": redact_count,
                "decision": d.get("decision", "pending"),
                "decision_reason_code": d.get("reason_code", ""),
                "decision_note": d.get("note", ""),
                "decision_ts": d.get("ts", ""),
            }
        )
    return out


def record_decision(
    ctx: _CaseShim,
    *,
    doc_ref: str,
    decision: str,
    reason_code: str,
    note: str = "",
) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision: {decision!r}")
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    validate_reason_code(reason_code, note)
    row = {
        "ts": _iso_now(),
        "doc_ref": doc_ref,
        "decision": decision,
        "reason_code": reason_code,
        "note": note,
    }
    # Chain-first: if schema/IO breaks, the user-visible JSONL row is not written.
    from dsar_orchestrator.local_broker.audit_chain import (
        emit_failure_for_case_dir,
        emit_for_case_dir,
    )

    original_hash = emit_for_case_dir(
        ctx.case_dir,
        decision_kind="leak_review",
        payload=row,
        item_id=doc_ref,
    )
    target = _decisions_file(ctx)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _DECISION_LOCK:
            with target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        # Chain has the decision event but the JSONL append failed; emit a
        # compensating FAILURE_RECORDED event referencing the original hash
        # so audit_verify can correlate the orphan event with its cause.
        emit_failure_for_case_dir(
            ctx.case_dir,
            decision_kind="leak_review",
            payload={
                "phase": "post-chain-jsonl-write",
                "original_event_hash": original_hash,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "target_path": str(target),
                "doc_ref": doc_ref,
            },
            item_id=doc_ref,
        )
        raise
    return row


def retry_redaction(ctx: _CaseShim, *, doc_ref: str) -> dict:
    """Re-invoke ``redact_document`` on a single doc. Reads the existing
    register entry + tag file for the ref. Updates redaction_decisions.jsonl
    with the new outcome and records a retried_ok/retried_fail decision.

    NOTE: the toolkit's ``redact.CASE_DIR`` is captured at module import
    time from ``Path.cwd()``. To make retries hit the right paths, change
    directory into ``ctx.case_dir`` before importing redact (or rely on
    the operator running this from the case dir — the operator console
    already shells into the case dir for orchestrator calls; we replicate
    that here).
    """
    import os

    try:
        register = json.loads((ctx.working / "register.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"register.json unreadable: {exc}"
        record_decision(ctx, doc_ref=doc_ref, decision="retried_fail", note=msg)
        return {"ok": False, "error": msg}
    entry = next((e for e in register if e.get("ref") == doc_ref), None)
    if not entry:
        msg = f"ref {doc_ref!r} not in register"
        record_decision(ctx, doc_ref=doc_ref, decision="retried_fail", note=msg)
        return {"ok": False, "error": msg}
    # Normalise extension (REDACTORS keys have leading dot)
    ext = entry.get("extension", "")
    if ext and not ext.startswith("."):
        ext = "." + ext
    doc_entry = {
        "ref": entry["ref"],
        "filename": entry.get("filename", ""),
        "path": entry.get("path", ""),
        "extension": ext.lower(),
    }
    # cwd-sensitive import of redact.py
    cwd_before = os.getcwd()
    try:
        os.chdir(ctx.case_dir)
        # Re-import to refresh module-level CASE_DIR
        import importlib

        from dsar_pipeline import redact as redact_mod

        importlib.reload(redact_mod)
        try:
            count = redact_mod.redact_document(doc_entry)
        except Exception as exc:  # noqa: BLE001
            msg = f"redact_document raised {type(exc).__name__}: {exc}"
            record_decision(ctx, doc_ref=doc_ref, decision="retried_fail", note=msg)
            return {"ok": False, "error": msg}
    finally:
        os.chdir(cwd_before)

    # count == -1 → still failed; >= 0 → success (count = redactions applied)
    if count < 0:
        msg = f"redact_document returned -1 (status=failed); leakcheck.json updated"
        record_decision(ctx, doc_ref=doc_ref, decision="retried_fail", note=msg)
        return {"ok": False, "error": msg, "count": count}
    # Update redaction_decisions.jsonl with the new outcome
    decisions_path = ctx.working / "redaction_decisions.jsonl"
    rows = _read_jsonl(decisions_path)
    updated = False
    for r in rows:
        if r.get("doc_ref") == doc_ref:
            r["status"] = "redacted"
            r["redaction_count"] = count
            updated = True
    if updated:
        tmp = decisions_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(decisions_path)
    record_decision(
        ctx,
        doc_ref=doc_ref,
        decision="retried_ok",
        note=f"redactions_applied={count}",
    )
    return {"ok": True, "count": count}


def summary_counts(ctx: _CaseShim) -> dict[str, int]:
    items = list_leaks(ctx)
    out = {d: 0 for d in DECISIONS}
    out["total"] = len(items)
    for it in items:
        out[it["decision"]] = out.get(it["decision"], 0) + 1
    return out
