"""#115 — ambiguous-flag review screen (cluster mode + per-instance expand).

``pii_tagger_mini`` marks entities as ``redact='flag'`` whenever the
classifier is uncertain (e.g. the LLM said "organisation" but couldn't
tell whether it's the data subject's employer or an unrelated third
party). On a real case that's ~16k flagged entities across thousands of
docs — the operator can't realistically eyeball each one.

This module groups flags by ``(text, classification)`` so one decision
applies to every instance of e.g. "Acme" classified as
``organisation``. Three verdicts:

- ``redact``    — rewrite every matching entry to ``redact=True``
- ``preserve``  — rewrite every matching entry to ``redact=False``
- ``escalate``  — leave the tag entries at ``redact='flag'``; record an
  event documenting the deferred decision (e.g. for DPO review)

For the rare context-dependent cluster (same text, different meaning per
doc), the operator drills into the per-instance expand view served by
the ``/flag-review/cluster/<key>`` route — those decisions still flow
through ``decide_cluster`` but the operator chooses a subset of doc_refs
by re-running cluster decisions on smaller groupings (out of scope for
this module; see issue #115 follow-up).

Drift asymmetry note (mirrors ``audit_chain`` module docstring):
``decide_cluster`` is chain-first, then rewrites the tag files, then
appends to ``audit/flag_review_decisions.jsonl``. If the JSONL append
raises, we emit a compensating ``FAILURE_RECORDED`` event with the
original event hash so audit_verify can correlate the orphan. Tag file
rewrites that partially succeed before the JSONL append fails are NOT
rolled back — the rewrites are the source of truth for downstream redact
behaviour; we accept that a JSONL row may be missing on disk-full while
the rewrites stand.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("flag-review")

_DECISION_LOCK = threading.Lock()

VERDICTS = ("redact", "preserve", "escalate")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iter_tag_files(case_dir: Path):
    working = case_dir / "working"
    if not working.exists():
        return
    for p in sorted(working.glob("*_tags.json")):
        try:
            yield p, json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("flag-review: skipping unreadable %s: %s", p, exc)
            continue


def cluster_flags(case_dir: Path) -> list[dict]:
    """Return clusters of entities with ``redact == 'flag'`` grouped by
    ``(text, classification)``, sorted by instance count descending.

    Each cluster is a dict with::

        {
            "text": str,
            "classification": str,
            "instance_count": int,
            "doc_refs": list[str],     # sorted, unique
        }
    """
    buckets: dict[tuple[str, str], dict] = {}
    for path, payload in _iter_tag_files(case_dir):
        ref = payload.get("ref") or path.name.removesuffix("_tags.json")
        for e in payload.get("entities", []) or []:
            if not isinstance(e, dict) or e.get("redact") != "flag":
                continue
            text = str(e.get("text", ""))
            cls = str(e.get("classification", ""))
            key = (text, cls)
            b = buckets.setdefault(
                key, {"text": text, "classification": cls, "instance_count": 0, "_refs": set()}
            )
            b["instance_count"] += 1
            b["_refs"].add(ref)

    clusters = []
    for b in buckets.values():
        clusters.append(
            {
                "text": b["text"],
                "classification": b["classification"],
                "instance_count": b["instance_count"],
                "doc_refs": sorted(b["_refs"]),
            }
        )
    clusters.sort(key=lambda c: (-c["instance_count"], c["text"], c["classification"]))
    return clusters


def _apply_verdict_to_entities(entities: list[dict], text: str, cls: str, verdict: str) -> int:
    """Rewrite the ``redact`` field on every entry matching
    ``(text, cls)``. Returns the number of entries touched. ``escalate``
    leaves entries unchanged but still counts matches."""
    touched = 0
    for e in entities:
        if not isinstance(e, dict) or e.get("redact") != "flag":
            continue
        if str(e.get("text", "")) != text or str(e.get("classification", "")) != cls:
            continue
        touched += 1
        if verdict == "redact":
            e["redact"] = True
        elif verdict == "preserve":
            e["redact"] = False
        # escalate: no field rewrite
    return touched


def _decisions_path(case_dir: Path) -> Path:
    return case_dir / "audit" / "flag_review_decisions.jsonl"


def decide_cluster(
    case_dir: Path,
    *,
    text: str,
    classification: str,
    verdict: str,
    reason_code: str,
    note: str,
    operator_id: str,
) -> dict:
    """Apply ``verdict`` to every flag-cluster instance matching
    ``(text, classification)`` across the case's ``*_tags.json`` files.

    Order of operations:
      1. Validate inputs (verdict + reason code).
      2. Emit a hash-chained ``REVIEWER_DECISION_MADE`` event.
      3. Rewrite each matching tag file in place (atomic per file).
      4. Append a row to ``audit/flag_review_decisions.jsonl``.

    If step 4 fails, emit a compensating ``FAILURE_RECORDED`` event with
    ``original_event_hash`` so audit_verify can correlate the orphan.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r} (allowed: {VERDICTS})")
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    validate_reason_code(reason_code, note)

    # Pre-scan tag files to compute affected refs and instance count for the
    # chain payload — keeps the audit event self-describing without needing
    # to re-read the rewritten files.
    affected_refs: list[str] = []
    affected_paths: list[Path] = []
    instance_count = 0
    for path, payload in _iter_tag_files(case_dir):
        entities = payload.get("entities", []) or []
        matches = sum(
            1
            for e in entities
            if isinstance(e, dict)
            and e.get("redact") == "flag"
            and str(e.get("text", "")) == text
            and str(e.get("classification", "")) == classification
        )
        if matches:
            ref = payload.get("ref") or path.name.removesuffix("_tags.json")
            affected_refs.append(ref)
            affected_paths.append(path)
            instance_count += matches

    row = {
        "ts": _iso_now(),
        "text": text,
        "classification": classification,
        "verdict": verdict,
        "reason_code": reason_code,
        "note": note,
        "operator_id": operator_id,
        "doc_refs": sorted(affected_refs),
        "instance_count": instance_count,
    }

    from dsar_orchestrator.local_broker.audit_chain import (
        emit_failure_for_case_dir,
        emit_for_case_dir,
    )

    item_id = f"{text}\x1f{classification}"
    original_hash = emit_for_case_dir(
        case_dir,
        decision_kind="flag_review",
        payload=row,
        item_id=item_id,
    )

    # Tag-file rewrites (atomic per file via tmp + replace). Only rewrite if
    # the verdict actually changes redact state — escalate is a no-op on
    # disk. If a rewrite raises mid-loop the chain event has already
    # claimed N instances applied, so reality drifts from the chain. We
    # cannot roll back earlier writes without per-file backups; instead we
    # emit a compensating ``FAILURE_RECORDED`` naming the rewritten + failed
    # files so ``audit_verify`` can correlate, then re-raise.
    rewritten: list[str] = []
    failed: list[dict] = []
    if verdict in ("redact", "preserve"):
        for path in affected_paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                failed.append(
                    {"path": str(path), "phase": "read", "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            touched = _apply_verdict_to_entities(
                payload.get("entities", []) or [], text, classification, verdict
            )
            if not touched:
                continue
            payload["flag_count"] = sum(
                1
                for e in payload.get("entities", [])
                if isinstance(e, dict) and e.get("redact") == "flag"
            )
            payload["redact_count"] = sum(
                1
                for e in payload.get("entities", [])
                if isinstance(e, dict) and e.get("redact") is True
            )
            tmp = path.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                emit_failure_for_case_dir(
                    case_dir,
                    decision_kind="flag_review",
                    payload={
                        "phase": "tag-file-rewrite-partial",
                        "original_event_hash": original_hash,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "failed_path": str(path),
                        "rewritten_so_far": rewritten,
                        "remaining_paths": [
                            str(p) for p in affected_paths[affected_paths.index(path) + 1 :]
                        ],
                        "text": text,
                        "classification": classification,
                        "verdict": verdict,
                    },
                    item_id=item_id,
                )
                raise
            rewritten.append(str(path))

    if failed:
        # Read-side skips happened but writes that did go through stand —
        # log a compensating event so audit_verify can see the gap without
        # raising (a corrupt tag file shouldn't fail an otherwise-good
        # cluster decision).
        emit_failure_for_case_dir(
            case_dir,
            decision_kind="flag_review",
            payload={
                "phase": "tag-file-read-skipped",
                "original_event_hash": original_hash,
                "skipped": failed,
                "rewritten": rewritten,
                "text": text,
                "classification": classification,
                "verdict": verdict,
            },
            item_id=item_id,
        )

    # User-visible decision log.
    decisions_path = _decisions_path(case_dir)
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _DECISION_LOCK:
            with decisions_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        emit_failure_for_case_dir(
            case_dir,
            decision_kind="flag_review",
            payload={
                "phase": "post-chain-jsonl-write",
                "original_event_hash": original_hash,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "target_path": str(decisions_path),
                "text": text,
                "classification": classification,
                "verdict": verdict,
            },
            item_id=item_id,
        )
        raise

    return row


def load_decisions(case_dir: Path) -> list[dict]:
    """Return all rows from ``audit/flag_review_decisions.jsonl`` in
    write order. Empty list if the file doesn't exist."""
    path = _decisions_path(case_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
