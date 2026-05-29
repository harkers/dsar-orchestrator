"""Per-event-type field-allowlist projection for the operator console
live-log feed.

Every event flowing into the SSE stream passes through
`project_for_browser` first. The result is the ONLY thing the browser
ever sees. Fail-closed default applies to unknown event types; values
that don't match their declared shape are replaced with `<typeerror>`.

L3 rows (from `~/.dsar-audit/<case>/pipeline.jsonl`) carry an `event`
field, not `event_type`; the iterator pre-maps `event` →
`event_type` so this module sees a uniform shape across all three
sources. `note().message` is free text and is DROPPED at projection.

Spec: 2026-05-29 operator-console-live-log design v2 §3.2, §6.6, §6.15.
"""

from __future__ import annotations

import re
from typing import Any


# Accept both `Z` and numeric-offset forms. The L3 producer
# (`dsar_orchestrator.audit.PipelineAuditor`) writes
# `datetime.now(timezone.utc).isoformat()` → `...+00:00`, while L1/L2
# producers use the `.replace("+00:00", "Z")` convention. Both must pass.
_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
_SEVERITY_ENUM = {"info", "warn", "error", "debug"}

_KNOWN_STAGES = {
    "fitness_preflight",
    "subject_protection_preflight",
    "people_register_preflight",
    "extraction_quality_gate",
    "threat_model_verify",
    "ingest",
    "stage_2_parallel",
    "stage_3_parallel",
    "sig_block_discovery",
    "scope_classify",
    "pii_classify",
    "redact",
    "presidio_anonymize",
    "pii_jury_review",
    "verify_spec",
    "bake",
    "verify_pdf",
    "export",
}

_NOTE_KINDS = {"info", "warn", "error", "debug"}

_STAGE_SKIPPED_REASONS = {
    # Closed enum per spec §3.2 — do NOT extend without a spec revision.
    "module_work_check",
    "halted_upstream",
    "manual_skip",
    "unknown",
}


# Each value is `{field_name: (shape_kind, shape_arg)}`.
_ALLOWLIST: dict[str, dict[str, tuple[str, Any]]] = {
    # ---- L1 audit_events (subset; extend as new events stabilise) ----
    # `stage` is allowlisted on L1 events too (spec §4.5 shows the wire
    # shape carrying it). It surfaces only when the producer emits it;
    # otherwise the top-level `stage` is null. Uses the shared
    # `_KNOWN_STAGES` enum for consistency with L3 events.
    "REDACT_STARTED": {
        "stage": ("enum_string", _KNOWN_STAGES),
    },
    "REDACT_COMPLETED": {
        "stage": ("enum_string", _KNOWN_STAGES),
        "refs_processed": ("int_range", (0, 10_000_000)),
        "redactions_applied": ("int_range", (0, 10_000_000)),
    },
    "PEOPLE_REGISTER_BUILT": {
        "stage": ("enum_string", _KNOWN_STAGES),
        "rows": ("int_range", (0, 10_000_000)),
        "clusters": ("int_range", (0, 1_000_000)),
    },
    "SIG_BLOCK_DISCOVERY_COMPLETED": {
        "stage": ("enum_string", _KNOWN_STAGES),
        "candidates_found": ("int_range", (0, 1_000_000)),
    },
    "PII_JURY_INFERENCE_RECORD": {
        "juror": ("enum_string", {"A", "B"}),
        "severity": ("enum_string", _SEVERITY_ENUM),
    },
    "PII_JURY_DISAGREEMENT": {
        "disagreement_kind": ("enum_string", {"boolean", "categories"}),
    },
    # ---- L3 pipeline.jsonl events (dispatched on `event` field) ----
    "stage_start": {
        "stage": ("enum_string", _KNOWN_STAGES),
        "ts": ("iso8601", None),
    },
    "stage_end": {
        "stage": ("enum_string", _KNOWN_STAGES),
        "ts": ("iso8601", None),
        "duration_s": ("num_range", (0, 86_400)),
    },
    "stage_skipped": {
        "stage": ("enum_string", _KNOWN_STAGES),
        "reason": ("enum_string", _STAGE_SKIPPED_REASONS),
    },
    "note": {
        # message is FREE TEXT — explicitly NOT in the allowlist.
        # The projection drops it; only `kind` surfaces.
        "kind": ("enum_string", _NOTE_KINDS),
    },
}


def scrub_value(fname: str, value: Any, kind: str, arg: Any) -> Any:
    """Return `value` if it matches the declared shape, else literal `<typeerror>`.

    Pure function, no I/O, no logging.
    """
    try:
        if kind == "enum_string":
            if isinstance(value, str) and value in arg:
                return value
            return "<typeerror>"
        if kind == "int_range":
            lo, hi = arg
            if isinstance(value, bool) or not isinstance(value, int):
                return "<typeerror>"
            return value if lo <= value <= hi else "<typeerror>"
        if kind == "num_range":
            # int OR float (rejects bool, which subclasses int). The L3
            # producer writes float `duration_s` via total_seconds().
            lo, hi = arg
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "<typeerror>"
            return value if lo <= value <= hi else "<typeerror>"
        if kind == "iso8601":
            if isinstance(value, str) and _ISO8601_RE.match(value):
                return value
            return "<typeerror>"
    except Exception:
        return "<typeerror>"
    return "<typeerror>"


def _project_payload(event_type: str, payload: dict) -> dict:
    fields = _ALLOWLIST.get(event_type)
    if fields is None:
        return {}
    projected = {}
    for fname, (kind, arg) in fields.items():
        if fname not in payload:
            continue
        projected[fname] = scrub_value(fname, payload[fname], kind, arg)
    return projected


def summary_for(event_type: str | None, projected: dict) -> str:
    """Compose a short human string from ALREADY-PROJECTED fields only."""
    if event_type is None or event_type not in _ALLOWLIST:
        return "(unrecognised event type)"
    if event_type == "REDACT_STARTED":
        return f"redaction started: {projected.get('stage', '<typeerror>')}"
    if event_type == "REDACT_COMPLETED":
        return (
            f"redacted {projected.get('redactions_applied', '<typeerror>')} of "
            f"{projected.get('refs_processed', '<typeerror>')} refs"
        )
    if event_type == "PEOPLE_REGISTER_BUILT":
        return (
            f"people register: {projected.get('rows', '<typeerror>')} rows, "
            f"{projected.get('clusters', '<typeerror>')} clusters"
        )
    if event_type == "SIG_BLOCK_DISCOVERY_COMPLETED":
        return f"sig-block candidates: {projected.get('candidates_found', '<typeerror>')}"
    if event_type == "PII_JURY_INFERENCE_RECORD":
        return f"PII jury {projected.get('juror', '<typeerror>')} ({projected.get('severity', '<typeerror>')})"
    if event_type == "PII_JURY_DISAGREEMENT":
        return f"PII jury disagreement ({projected.get('disagreement_kind', '<typeerror>')})"
    if event_type == "stage_start":
        return f"stage started: {projected.get('stage', '<typeerror>')}"
    if event_type == "stage_end":
        return (
            f"stage ended: {projected.get('stage', '<typeerror>')} "
            f"({projected.get('duration_s', '<typeerror>')}s)"
        )
    if event_type == "stage_skipped":
        return f"stage skipped: {projected.get('stage', '<typeerror>')} ({projected.get('reason', '<typeerror>')})"
    if event_type == "note":
        return f"note ({projected.get('kind', '<typeerror>')})"
    return "(unrecognised event type)"


def project_for_browser(event: dict) -> dict:
    """Apply the per-event-type allowlist + bounded-enum scrubber.

    Returns a dict safe to serialise to the SSE response. NEVER returns
    raw payload bytes.
    """
    event_type = event.get("event_type")
    source = event.get("source", "unknown")
    # Every value reaching the browser is scrubbed — the envelope `ts`
    # is no exception. Malformed/injected timestamps become <typeerror>.
    ts = scrub_value("ts", event.get("ts"), "iso8601", None)
    payload = event.get("payload")
    # Defend against a malformed envelope whose payload is not a dict
    # (list/str/None). §6.6 requires projection to be crash-free.
    if not isinstance(payload, dict):
        payload = {}

    # Fail-closed default (spec §3.2): unknown event types return ONLY
    # {kind, ts, source, summary} — no other fields, no payload values.
    if event_type not in _ALLOWLIST:
        return {
            "kind": "event",
            "ts": ts,
            "source": source,
            "summary": summary_for(event_type, {}),
        }

    projected_fields = _project_payload(event_type, payload)
    severity = scrub_value(
        "severity",
        payload.get("severity", "info"),
        "enum_string",
        _SEVERITY_ENUM,
    )

    return {
        "kind": "event",
        "ts": ts,
        "source": source,
        "event_type": event_type,
        "stage": projected_fields.get("stage"),
        "severity": severity,
        "summary": summary_for(event_type, projected_fields),
    }
