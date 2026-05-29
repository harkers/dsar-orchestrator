# tests/test_live_log_projection.py
"""Tests for the per-event-type field-allowlist projection that
guards PII from reaching the browser. Spec §3.2."""

from __future__ import annotations

import pytest

from dsar_orchestrator.local_broker.live_log_projection import (
    _ALLOWLIST,
    project_for_browser,
    scrub_value,
    summary_for,
)


def _event(source: str, event_type: str | None, payload: dict) -> dict:
    return {
        "kind": "event",
        "source": source,
        "ts": "2026-05-29T10:42:11.123Z",
        "event_type": event_type,
        "payload": payload,
    }


def test_unknown_event_type_falls_back_to_unrecognised_summary() -> None:
    out = project_for_browser(_event("audit", "TOTALLY_UNKNOWN_EVENT", {"secret": "leaked-name"}))
    assert out["kind"] == "event"
    assert out["summary"] == "(unrecognised event type)"
    for v in out.values():
        if isinstance(v, str):
            assert "leaked-name" not in v


def test_known_l1_event_projects_only_allowlisted_fields() -> None:
    out = project_for_browser(
        _event(
            "audit",
            "REDACT_COMPLETED",
            {
                "refs_processed": 412,
                "redactions_applied": 89,
                # PII-bearing fields that must NOT survive projection.
                "subject_protected_phrases": ["Jane Smith"],
                "example_tokens": ["jane@example.com"],
            },
        )
    )
    serialised = str(out)
    assert "Jane Smith" not in serialised
    assert "subject_protected_phrases" not in serialised
    assert "example_tokens" not in serialised
    assert "jane@example.com" not in serialised


def test_l3_stage_start_projects_stage_and_ts() -> None:
    """L3 row dispatches on `event` field, not `event_type`."""
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "stage_start",  # iter_live_events maps event→event_type
            "payload": {
                "event": "stage_start",
                "stage": "redact",
                "ts": "2026-05-29T10:42:11Z",
                "schema_version": "v1",
                "producer_version": "v1",
                "case": "CASE-123",
            },
        }
    )
    assert "stage started" in out["summary"]
    assert "redact" in out["summary"]


def test_l3_note_drops_message_field() -> None:
    """§3.2: `note().message` is free text → DROPPED by projection.
    Critical PII control — operator-readable rationale strings must
    not surface in the live feed."""
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "note",
            "payload": {
                "event": "note",
                "kind": "info",
                "message": "Jane Smith is the data subject, DoB 1985-03-14",
            },
        }
    )
    serialised = str(out)
    assert "Jane Smith" not in serialised
    assert "1985-03-14" not in serialised
    assert "message" not in out
    assert out["summary"] == "note (info)"


def test_l3_unknown_event_value_fails_closed() -> None:
    """A future `RunReport` row or any unrecognised event value flows
    through the fail-closed default."""
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "RunReport",
            "payload": {
                "event": "RunReport",
                "halt_reason": "Jane Smith vulnerable subject — paused",
            },
        }
    )
    assert "Jane Smith" not in str(out)
    assert out["summary"] == "(unrecognised event type)"


def test_scrub_value_replaces_malformed_enum_value() -> None:
    assert (
        scrub_value("severity", "info", "enum_string", {"info", "warn", "error", "debug"}) == "info"
    )
    assert scrub_value("severity", "<script>", "enum_string", {"info", "warn"}) == "<typeerror>"


def test_scrub_value_replaces_out_of_range_int() -> None:
    assert scrub_value("refs_processed", 412, "int_range", (0, 10_000_000)) == 412
    assert scrub_value("refs_processed", -1, "int_range", (0, 10_000_000)) == "<typeerror>"


def test_scrub_value_passes_iso8601() -> None:
    assert scrub_value("ts", "2026-05-29T10:42:11Z", "iso8601", None) == "2026-05-29T10:42:11Z"
    assert scrub_value("ts", "not a ts", "iso8601", None) == "<typeerror>"


def test_summary_for_uses_only_projected_fields() -> None:
    projected = {"refs_processed": 412, "redactions_applied": 89}
    s = summary_for("REDACT_COMPLETED", projected)
    assert "412" in s
    assert "89" in s


def test_unknown_event_output_has_only_minimal_keys() -> None:
    """Spec §3.2 fail-closed: unknown event types return ONLY
    {kind, ts, source, summary} — no event_type/stage/severity."""
    out = project_for_browser(_event("audit", "FUTURE_EVENT", {"refs_processed": 1}))
    assert set(out.keys()) == {"kind", "ts", "source", "summary"}


def test_known_event_output_has_full_shape() -> None:
    """Spec §4.5: known events carry the full wire shape."""
    out = project_for_browser(
        _event(
            "audit",
            "REDACT_COMPLETED",
            {"refs_processed": 412, "redactions_applied": 89},
        )
    )
    assert set(out.keys()) == {
        "kind",
        "ts",
        "source",
        "event_type",
        "stage",
        "severity",
        "summary",
    }
    assert out["event_type"] == "REDACT_COMPLETED"


def test_malformed_ts_is_scrubbed() -> None:
    """The envelope ts is scrubbed through iso8601 like any other value."""
    out = project_for_browser(
        _event("audit", "REDACT_COMPLETED", {"refs_processed": 1})
        | {"ts": "not-a-timestamp; <script>"}
    )
    assert out["ts"] == "<typeerror>"


def test_stage_skipped_reason_enum_matches_spec() -> None:
    """Spec §3.2 closed enum has exactly 4 reasons; anything else
    (e.g. the old `preflight_failed`) must scrub to <typeerror>."""
    ok = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "stage_skipped",
            "payload": {"event": "stage_skipped", "stage": "redact", "reason": "halted_upstream"},
        }
    )
    assert "halted_upstream" in ok["summary"]
    bad = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "stage_skipped",
            "payload": {"event": "stage_skipped", "stage": "redact", "reason": "preflight_failed"},
        }
    )
    assert "<typeerror>" in bad["summary"]


def test_redact_started_has_real_summary_not_unrecognised() -> None:
    """REDACT_STARTED is allowlisted, so its summary must NOT fall
    through to the fail-closed '(unrecognised event type)' string."""
    out = project_for_browser(_event("audit", "REDACT_STARTED", {"stage": "redact"}))
    assert out["summary"] != "(unrecognised event type)"
    assert "redact" in out["summary"]


def test_l1_event_surfaces_stage_when_present() -> None:
    """Spec §4.5: known L1 events carry `stage` when the producer emits it."""
    out = project_for_browser(
        _event(
            "audit",
            "REDACT_COMPLETED",
            {"stage": "redact", "refs_processed": 412, "redactions_applied": 89},
        )
    )
    assert out["stage"] == "redact"


def test_l3_stage_end_accepts_float_duration() -> None:
    """The L3 producer (PipelineAuditor) writes float `duration_s` via
    total_seconds(); it must survive the num_range scrubber, not become
    <typeerror>."""
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "stage_end",
            "payload": {
                "event": "stage_end",
                "stage": "redact",
                "ts": "2026-05-29T10:42:11Z",
                "duration_s": 12.345,
            },
        }
    )
    assert "<typeerror>" not in out["summary"]
    assert "12.345" in out["summary"]


def test_l3_stage_end_rejects_out_of_range_duration() -> None:
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "stage_end",
            "payload": {
                "event": "stage_end",
                "stage": "redact",
                "ts": "2026-05-29T10:42:11Z",
                "duration_s": 999_999.0,
            },
        }
    )
    assert "<typeerror>" in out["summary"]


def test_iso8601_accepts_numeric_offset() -> None:
    """The L3 producer writes `...+00:00`, not `...Z`. Both must pass."""
    assert (
        scrub_value("ts", "2026-05-29T10:42:11+00:00", "iso8601", None)
        == "2026-05-29T10:42:11+00:00"
    )
    assert scrub_value("ts", "2026-05-29T10:42:11Z", "iso8601", None) == "2026-05-29T10:42:11Z"


def test_missing_ts_scrubs_without_crashing() -> None:
    """A None/absent envelope ts must not raise — it scrubs to <typeerror>."""
    out = project_for_browser(
        {
            "kind": "event",
            "source": "audit",
            "event_type": "REDACT_COMPLETED",
            "payload": {"refs_processed": 1, "redactions_applied": 1},
        }
    )
    assert out["ts"] == "<typeerror>"


def test_non_dict_payload_does_not_crash() -> None:
    """A malformed envelope with a non-dict payload must scrub safely,
    not raise (spec §6.6 — projection is crash-free)."""
    for bad in ([1, 2, 3], "a string", 42, None):
        out = project_for_browser(
            {
                "kind": "event",
                "source": "audit",
                "ts": "2026-05-29T10:42:11Z",
                "event_type": "REDACT_COMPLETED",
                "payload": bad,
            }
        )
        assert out["kind"] == "event"


def test_l3_unknown_stage_scrubs_to_typeerror() -> None:
    out = project_for_browser(
        {
            "kind": "event",
            "source": "cond",
            "ts": "2026-05-29T10:42:11Z",
            "event_type": "stage_start",
            "payload": {
                "event": "stage_start",
                "stage": "not_a_real_stage",
                "ts": "2026-05-29T10:42:11Z",
            },
        }
    )
    assert out["stage"] == "<typeerror>"


def test_num_range_accepts_upper_boundary() -> None:
    assert scrub_value("duration_s", 86_400.0, "num_range", (0, 86_400)) == 86_400.0


def test_iso8601_accepts_arbitrary_offset() -> None:
    assert (
        scrub_value("ts", "2026-05-29T10:42:11+05:30", "iso8601", None)
        == "2026-05-29T10:42:11+05:30"
    )


def test_allowlist_table_has_no_freetext_string_shapes() -> None:
    """No allowlisted field is a free-text string. Every shape is a
    bounded validator — enum_string, int_range, num_range, or iso8601."""
    for event_type, fields in _ALLOWLIST.items():
        for fname, (kind, _arg) in fields.items():
            assert kind in {"enum_string", "int_range", "num_range", "iso8601"}, (
                f"{event_type}.{fname} declared as {kind!r}; PII risk"
            )
