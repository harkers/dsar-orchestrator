"""R001-R010 + R-PENDING reason-code taxonomy and validation.

Every operator decision (leak-review, unextractable, blocker-toggle)
records a ``reason_code`` so the audit log answers *why* — not just
*what* — was decided. ``R-PENDING`` is the explicit escape hatch when
none of R001-R010 fits; it requires a free-text note and auto-escalates
24 h after recording (see ``is_r_pending_stale``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

REASON_CODES: dict[str, dict] = {
    "R001": {
        "label": "Correct DS match",
        "meaning": "Document correctly identified as personal data of the data subject.",
        "requires_note": False,
    },
    "R002": {
        "label": "Not DS personal data",
        "meaning": "Document does not contain personal data of the data subject.",
        "requires_note": False,
    },
    "R003": {
        "label": "Work-context only",
        "meaning": "Subject mentioned incidentally in a work / corporate role; not substantively relating to them.",
        "requires_note": False,
    },
    "R004": {
        "label": "Duplicate of reviewed item",
        "meaning": "Same content already reviewed under a canonical ref.",
        "requires_note": False,
    },
    "R005": {
        "label": "Third-party redaction required",
        "meaning": "Document contains third-party personal data needing redaction.",
        "requires_note": False,
    },
    "R006": {
        "label": "Special category — escalate",
        "meaning": "Special-category data (health, ethnic origin, etc.) — DPO review required.",
        "requires_note": True,
    },
    "R007": {
        "label": "Redaction confirmed accurate",
        "meaning": "Applied redactions verified correct on QA review.",
        "requires_note": False,
    },
    "R008": {
        "label": "Redaction incomplete",
        "meaning": "Applied redactions miss in-scope personal data; needs re-redaction or operator fix.",
        "requires_note": False,
    },
    "R009": {
        "label": "Technical extraction issue",
        "meaning": "Item could not be processed due to a technical limitation (codec, corruption, missing dep).",
        "requires_note": False,
    },
    "R010": {
        "label": "Withhold pending legal review",
        "meaning": "Item flagged for legal counsel review before release decision.",
        "requires_note": True,
    },
    "R-PENDING": {
        "label": "Pending classification",
        "meaning": "Operator cannot yet pick a code; free-text note required, auto-escalates to DPO after 24 h.",
        "requires_note": True,
        "escalate_after_hours": 24,
    },
}

_R_PENDING_WINDOW = timedelta(hours=REASON_CODES["R-PENDING"]["escalate_after_hours"])


def validate_reason_code(code: str, note: str) -> None:
    """Raise ``ValueError`` if ``code`` is missing, unknown, or the entry
    requires a note and ``note`` is empty / whitespace-only.
    """
    if not code:
        raise ValueError(f"reason_code is required; pick one of {sorted(REASON_CODES)}")
    if code not in REASON_CODES:
        raise ValueError(f"unknown reason_code {code!r}; pick one of {sorted(REASON_CODES)}")
    entry = REASON_CODES[code]
    if entry.get("requires_note") and not (note or "").strip():
        raise ValueError(f"{code} ({entry['label']}) requires a non-empty note")


def is_r_pending_stale(ts_iso: str, *, now: datetime | None = None) -> bool:
    """True if an R-PENDING decision timestamped at ``ts_iso`` is past
    its escalation window. Malformed timestamps return True (safer to
    escalate a missing timestamp than swallow it).
    """
    now = now or datetime.now(UTC)
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts) > _R_PENDING_WINDOW
