"""Hash-chained audit emission for operator-console decisions.

Wraps ``dsar_pipeline.audit.FileAuditStore.append_event`` so each
operator-recorded decision (leak-review, unextractable, blocker-toggle)
appends a tamper-evident ``REVIEWER_DECISION_MADE`` event under
``<case>/working/audit_events.jsonl``.

The existing per-decision JSONL files (``audit/leak_review_decisions.jsonl``
etc.) stay as the user-visible decision log; the hash chain is the
integrity layer the toolkit's ``audit_verify`` can later challenge.

Drift asymmetry — read this before changing the call order
---------------------------------------------------------
Callers run chain-first then JSONL-append. This prevents one direction of
drift (no JSONL row that lacks a chained event). The reverse direction —
chain has an event but JSONL append fails afterwards (disk full, EACCES
mid-call) — is NOT prevented here. A follow-up may emit a compensating
``REVIEWER_DECISION_FAILED`` event from the caller to close that gap.

Cross-process race — bounded, not eliminated
--------------------------------------------
``_EMIT_LOCK`` is a process-level lock. ``fcntl.flock`` on the audit
events file extends protection to a second console process. It does NOT
protect against the toolkit's own ``append_event`` callers (e.g. agents
emitting ``REDACTION_APPLIED``) which currently do not flock — that
deserves a separate change in the toolkit. Operator console + agents
running concurrently against the same case dir can still race.
"""

from __future__ import annotations

import fcntl
import json
import logging
import threading
from pathlib import Path

from dsar_pipeline.audit import AuditEventType, FileAuditStore

log = logging.getLogger("audit-chain")

_EMIT_LOCK = threading.Lock()


def resolve_case_id(case_dir: Path) -> str:
    """Return the case identifier for ``case_dir``.

    Reads ``working/data_subject.json::case_no`` (or ``case_id``).
    Falls back to ``case_dir.name`` and logs a warning on fallback, since
    a wrong-but-plausible case_id silently embedded into every chained
    event defeats the integrity guarantee.
    """
    ds_path = case_dir / "working" / "data_subject.json"
    if not ds_path.exists():
        log.warning("audit_chain: %s missing — case_id falls back to dir name", ds_path)
        return case_dir.name
    try:
        data = json.loads(ds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("audit_chain: %s unreadable (%s) — falling back to dir name", ds_path, exc)
        return case_dir.name
    case_id = data.get("case_id") or data.get("case_no")
    if not case_id:
        log.warning(
            "audit_chain: %s has neither case_id nor case_no — falling back to dir name", ds_path
        )
        return case_dir.name
    return str(case_id)


def _emit_typed(
    case_dir: Path,
    *,
    event_type: AuditEventType,
    decision_kind: str,
    payload: dict,
    case_id: str,
    item_id: str | None,
) -> str:
    working = case_dir / "working"
    working.mkdir(parents=True, exist_ok=True)
    events_path = working / "audit_events.jsonl"
    store = FileAuditStore(working)
    with _EMIT_LOCK:
        events_path.touch(exist_ok=True)
        with events_path.open("rb") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                return store.append_event(
                    event_type,
                    payload=payload,
                    case_id=case_id,
                    agent="operator_console",
                    item_id=item_id,
                    stage=decision_kind,
                )
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def emit_decision_event(
    case_dir: Path,
    *,
    decision_kind: str,
    payload: dict,
    case_id: str,
    item_id: str | None = None,
) -> str:
    """Append a hash-chained ``REVIEWER_DECISION_MADE`` event for an
    operator-console decision. Returns the canonical hash of the written
    event (suitable for ``parent_ref`` linkage). Raises on schema /
    IO failure — caller decides whether to swallow.

    Holds an in-process lock AND an advisory file lock on
    ``audit_events.jsonl`` for the duration of the append.
    """
    return _emit_typed(
        case_dir,
        event_type=AuditEventType.REVIEWER_DECISION_MADE,
        decision_kind=decision_kind,
        payload=payload,
        case_id=case_id,
        item_id=item_id,
    )


def emit_failure_event(
    case_dir: Path,
    *,
    decision_kind: str,
    payload: dict,
    case_id: str,
    item_id: str | None = None,
) -> str:
    """Append a hash-chained ``FAILURE_RECORDED`` compensating event when
    the post-chain user-visible write (JSONL row or state file) raises.
    Closes the chain-event-without-JSONL-row drift gap noted in the module
    docstring. Returns the canonical hash of the written event.
    """
    return _emit_typed(
        case_dir,
        event_type=AuditEventType.FAILURE_RECORDED,
        decision_kind=decision_kind,
        payload=payload,
        case_id=case_id,
        item_id=item_id,
    )


def emit_for_case_dir(
    case_dir: Path,
    *,
    decision_kind: str,
    payload: dict,
    item_id: str | None = None,
) -> str:
    """Convenience: auto-resolve case_id then emit a decision event."""
    return emit_decision_event(
        case_dir,
        decision_kind=decision_kind,
        payload=payload,
        case_id=resolve_case_id(case_dir),
        item_id=item_id,
    )


def emit_failure_for_case_dir(
    case_dir: Path,
    *,
    decision_kind: str,
    payload: dict,
    item_id: str | None = None,
) -> str:
    """Convenience: auto-resolve case_id then emit a compensating
    FAILURE_RECORDED event."""
    return emit_failure_event(
        case_dir,
        decision_kind=decision_kind,
        payload=payload,
        case_id=resolve_case_id(case_dir),
        item_id=item_id,
    )
