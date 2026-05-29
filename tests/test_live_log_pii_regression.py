# tests/test_live_log_pii_regression.py
"""PII regression: every test feeds a synthetic event whose payload
contains a known PII canary; asserts the literal NEVER appears in
the serialised projected dict.

Spec §3.2, §7.4.
"""

from __future__ import annotations

import json

import pytest

from dsar_orchestrator.local_broker.live_log_projection import (
    project_for_browser,
)


_CANARIES = [
    "Jane Smith",
    "X Surname",
    "jane@example.com",
    "+44 7700 900123",
    "/Volumes/acme-engagement/case/foo.eml",
    "Acme Engagement Confidential",
    "leak-canary-string-1234567890",
]


@pytest.mark.parametrize("canary", _CANARIES)
def test_pii_canaries_never_appear_in_redact_completed(canary: str) -> None:
    out = project_for_browser(
        {
            "kind": "event",
            "source": "audit",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "REDACT_COMPLETED",
            "payload": {
                "refs_processed": 412,
                "redactions_applied": 89,
                "subject_protected_phrases": [canary],
                "example_tokens": [canary],
                "input_artefacts": [{"path": canary, "sha256": "x"}],
                "rationale": canary,
            },
        }
    )
    serialised = json.dumps(out, ensure_ascii=False)
    assert canary not in serialised, f"PII canary {canary!r} leaked: {serialised}"


@pytest.mark.parametrize("canary", _CANARIES)
def test_pii_canaries_never_appear_for_unknown_event_type(canary: str) -> None:
    out = project_for_browser(
        {
            "kind": "event",
            "source": "audit",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "FUTURE_EVENT_NOT_ALLOWLISTED",
            "payload": {
                "subject_protected_phrases": [canary],
                "name": canary,
                "everything": canary,
            },
        }
    )
    assert canary not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("canary", _CANARIES)
def test_l3_note_message_field_is_dropped(canary: str) -> None:
    """The critical L3 PII regression: `note().message` is free text
    and MUST be dropped by the projection. Operator-readable rationale
    strings written by PipelineAuditor.note() never reach the browser."""
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "note",
            "payload": {
                "event": "note",
                "kind": "info",
                "message": f"context says: {canary}",
            },
        }
    )
    serialised = json.dumps(out, ensure_ascii=False)
    assert canary not in serialised, f"L3 note().message leaked canary {canary!r}: {serialised}"
    assert "message" not in out


@pytest.mark.parametrize("canary", _CANARIES)
def test_l3_unknown_event_value_fails_closed(canary: str) -> None:
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "RunReport",
            "payload": {
                "event": "RunReport",
                "halt_reason": f"halted on subject {canary}",
            },
        }
    )
    assert canary not in json.dumps(out, ensure_ascii=False)


def test_severity_field_does_not_leak_freeform_text() -> None:
    out = project_for_browser(
        {
            "kind": "event",
            "source": "audit",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "REDACT_COMPLETED",
            "payload": {"severity": "leak-canary-9876"},
        }
    )
    assert "leak-canary-9876" not in json.dumps(out)
    assert out["severity"] == "<typeerror>"
