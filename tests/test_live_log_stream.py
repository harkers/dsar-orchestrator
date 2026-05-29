# tests/test_live_log_stream.py
"""Unit tests for live_log_stream. Spec §4.3, §6."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsar_orchestrator.local_broker.live_log_stream import (
    INITIAL_ZERO_CURSOR,
    JsonlTail,
    LiveEvent,
    ResumeCursor,
    _l3_pipeline_jsonl_path,
    _PerSourceCursor,
    iter_live_events,
    open_sources,
    parse_composite_last_event_id,
)


def _ts(seconds_ago: float) -> str:
    return (
        (datetime.now(UTC) - timedelta(seconds=seconds_ago))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write(path: Path, payload: dict, *, mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def test_jsonl_tail_fstat_identity_stable_under_appends(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    initial = tail.fstat_identity()
    for i in range(50):
        _write(p, {"i": i})
    after = tail.fstat_identity()
    tail.close()
    assert initial == after


def test_jsonl_tail_stat_path_identity_changes_on_rotation(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    _write(p, {"i": 0}, mode="w")
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    before = tail.stat_path_identity()
    p.unlink()
    _write(p, {"i": 0}, mode="w")
    after = tail.stat_path_identity()
    tail.close()
    assert before != after


def test_jsonl_tail_fd_identity_pinned_after_rotation(tmp_path: Path) -> None:
    """The CORE fd-vs-path invariant (§6.7): after unlink+recreate the
    held fd still points at the OLD inode (fstat_identity unchanged)
    while stat_path_identity reports the NEW inode. Their divergence is
    exactly how rotation is detected."""
    p = tmp_path / "audit.jsonl"
    _write(p, {"i": 0}, mode="w")
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    fd_before = tail.fstat_identity()
    path_before = tail.stat_path_identity()
    assert fd_before == path_before  # same file pre-rotation
    p.unlink()
    _write(p, {"i": 0}, mode="w")
    fd_after = tail.fstat_identity()
    path_after = tail.stat_path_identity()
    tail.close()
    assert fd_after == fd_before, "held fd must stay pinned to old inode"
    assert path_after != fd_after, "path now points at the new inode"


def test_jsonl_tail_record_identity_populates_baseline(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    assert tail.identity_tuple is None
    tail.record_identity_tuple()
    assert tail.identity_tuple == tail.fstat_identity()
    tail.close()


def test_read_new_lines_streams_discard_oversize_line_without_oom(tmp_path: Path) -> None:
    """§6.2: an over-length line with no newline must NOT be buffered
    whole. It is stream-discarded (one line_too_long marker) and the
    internal buffer stays bounded, then a following valid line is
    recovered."""
    p = tmp_path / "audit.jsonl"
    with p.open("ab") as fh:
        fh.write(b"x" * (300_000))  # > max_line_bytes, NO newline yet
    tail = JsonlTail(p, max_line_bytes=100_000)
    first = list(tail.read_new_lines(max_lines=100, max_bytes=10_000_000))
    # Marker emitted, buffer not holding the 300 KiB blob.
    assert any(e.get("_kind") == "line_too_long" for e in first)
    assert len(tail._buffer) <= 65_536
    # Now finish the giant line and add a real event.
    with p.open("ab") as fh:
        fh.write(b"\n" + b'{"i": 7}\n')
    rest = list(tail.read_new_lines(max_lines=100, max_bytes=10_000_000))
    tail.close()
    assert [e["i"] for e in rest if "i" in e] == [7]


def test_read_new_lines_blank_burst_is_bounded_by_max_bytes(tmp_path: Path) -> None:
    """Blank lines consume the byte budget — a blank-line flood cannot
    process unbounded lines in one bounded batch (§6.9)."""
    p = tmp_path / "audit.jsonl"
    with p.open("ab") as fh:
        fh.write(b"\n" * 5000)
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    got = list(tail.read_new_lines(max_lines=1_000_000, max_bytes=100))
    tail.close()
    # Bounded: ~100 newline-bytes consumed, not all 5000.
    assert len(got) == 0  # blanks yield nothing
    assert tail._buffer.count(b"\n") > 0  # remainder left for next call


def test_reopen_clears_stale_buffer(tmp_path: Path) -> None:
    """reopen() must drop buffered bytes from the rotated-away file."""
    p = tmp_path / "audit.jsonl"
    with p.open("ab") as fh:
        fh.write(b'{"i": 0}\n{"i": 1')  # trailing partial buffered
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    assert tail._buffer  # partial '{"i": 1' buffered
    p.unlink()
    with p.open("ab") as fh:
        fh.write(b'{"i": 9}\n')
    tail.reopen()
    assert tail._buffer == b""
    got = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.close()
    assert [e["i"] for e in got] == [9]


def test_jsonl_tail_seek_to_repositions_for_reread(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    with p.open("ab") as fh:
        fh.write(b'{"i": 0}\n{"i": 1}\n')
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    first = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.seek_to(0)  # byte-exact rewind
    again = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.close()
    assert [e["i"] for e in first] == [0, 1]
    assert [e["i"] for e in again] == [0, 1]


def test_jsonl_tail_stat_path_identity_returns_none_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    p.unlink()
    assert tail.stat_path_identity() is None
    tail.close()


def test_jsonl_tail_fstat_size_tracks_appends(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    s0 = tail.fstat_size()
    _write(p, {"i": 0})
    s1 = tail.fstat_size()
    tail.close()
    assert s1 > s0


def test_jsonl_tail_tolerates_missing_file(tmp_path: Path) -> None:
    """A missing source must not crash construction."""
    p = tmp_path / "nope.jsonl"
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    assert tail.fstat_identity() is None
    assert tail.stat_path_identity() is None
    tail.close()


def test_read_new_lines_yields_only_complete_lines(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"i": 0}\n{"i": 1}\n{"i": 2')  # last incomplete
    got = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.close()
    assert [evt["i"] for evt in got] == [0, 1]


def test_read_new_lines_completes_partial_after_next_newline(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"i": 0}\n{"i": 1')
    first = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    with p.open("a", encoding="utf-8") as fh:
        fh.write('}\n{"i": 2}\n')
    second = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.close()
    assert [evt["i"] for evt in first] == [0]
    assert [evt["i"] for evt in second] == [1, 2]


def test_read_new_lines_respects_max_lines(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    with p.open("a", encoding="utf-8") as fh:
        for i in range(10):
            fh.write(json.dumps({"i": i}) + "\n")
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    got = list(tail.read_new_lines(max_lines=3, max_bytes=1_048_576))
    assert len(got) == 3
    rest = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.close()
    assert [evt["i"] for evt in got] + [evt["i"] for evt in rest] == list(range(10))


def test_read_new_lines_respects_max_bytes(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    with p.open("a", encoding="utf-8") as fh:
        for i in range(50):
            fh.write(json.dumps({"i": i, "pad": "x" * 50}) + "\n")
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    got = list(tail.read_new_lines(max_lines=100, max_bytes=200))
    tail.close()
    assert 0 < len(got) <= 5


def test_read_new_lines_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    with p.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"i": 1}) + "\n")
        fh.write("{still bad\n")
        fh.write(json.dumps({"i": 2}) + "\n")
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    got = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.close()
    assert [evt["i"] for evt in got] == [1, 2]


# ----------------------------------------------------------------------
# Block D — composite cursor + merging iterator
# ----------------------------------------------------------------------


def test_parse_composite_cursor_roundtrip() -> None:
    raw = "audit:1024:1:123|stage:redaction_decisions.jsonl:512:1:456"
    cursor = parse_composite_last_event_id(raw)
    assert cursor is not None
    assert cursor.is_valid()
    assert cursor.per_source["audit"].offset == 1024
    assert cursor.per_source["audit"].identity_tuple == (1, 123)
    assert cursor.per_source["stage:redaction_decisions.jsonl"].offset == 512


def test_parse_composite_cursor_none_on_empty() -> None:
    assert parse_composite_last_event_id(None) is None
    assert parse_composite_last_event_id("") is None


def test_parse_composite_cursor_none_on_garbage() -> None:
    assert parse_composite_last_event_id("garbage:not:numeric:here") is None
    assert parse_composite_last_event_id("nofields") is None
    assert parse_composite_last_event_id("a:1:2|b:") is None


def test_parse_composite_cursor_rejects_oversized_header() -> None:
    huge = "audit:1024:1:123|" * 10_000
    assert parse_composite_last_event_id(huge) is None


def test_initial_zero_cursor() -> None:
    assert isinstance(INITIAL_ZERO_CURSOR, ResumeCursor)
    assert INITIAL_ZERO_CURSOR.is_valid() is False


def test_live_event_composite_cursor() -> None:
    e = LiveEvent(
        kind="event",
        source="audit",
        ts="2026-05-29T10:42:11Z",
        event_type="REDACT_COMPLETED",
        payload={"refs_processed": 1},
        composite_cursor="audit:1024:1:123",
    )
    assert e.composite_cursor == "audit:1024:1:123"


def test_open_sources_opens_l1_and_l3_at_correct_paths(tmp_path: Path) -> None:
    case_no = "CASE-123"
    case_dir = tmp_path / case_no
    (case_dir / "working").mkdir(parents=True)

    sources = open_sources(case_dir, max_line_bytes=1_048_576, verbose_l2=False)
    assert "audit" in sources
    assert sources["audit"].path == case_dir / "working" / "audit_events.jsonl"
    assert "cond" in sources
    # L3 lives OUTSIDE case_dir under the user's audit root.
    assert sources["cond"].path == _l3_pipeline_jsonl_path(case_dir)
    for s in sources.values():
        s.close()


def test_l3_pipeline_jsonl_path_uses_case_dir_name_as_case_no(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-XYZ"
    case_dir.mkdir()
    expected = Path.home() / ".dsar-audit" / "CASE-XYZ" / "pipeline.jsonl"
    assert _l3_pipeline_jsonl_path(case_dir) == expected


def test_open_sources_verbose_l2_includes_stage_artefacts(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)

    sources = open_sources(case_dir, max_line_bytes=1_048_576, verbose_l2=True)
    # At least one stage-artefact source should be present.
    stage_keys = [k for k in sources if k.startswith("stage:")]
    assert stage_keys, f"verbose_l2=True did not enroll stage sources: {list(sources)}"
    for s in sources.values():
        s.close()


def test_phase_a_replay_yields_events_within_window(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(50), "event_type": "OLD"}) + "\n")
        fh.write(json.dumps({"ts": _ts(20), "event_type": "RECENT_1"}) + "\n")
        fh.write(json.dumps({"ts": _ts(10), "event_type": "RECENT_2"}) + "\n")

    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=None,
            skip_replay=False,
            replay_window_s=30,
            replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15,
            poll_interval=0.05,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            if e.kind == "event":
                seen.append(e)
                if len(seen) >= 2:
                    stop.set()
                    return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert [s.event_type for s in seen] == ["RECENT_1", "RECENT_2"]


def test_phase_a_replay_byte_cap_emits_gap(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        for i in range(100):
            fh.write(json.dumps({"ts": _ts(0.1 * i), "pad": "x" * 500}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=None,
            skip_replay=False,
            replay_window_s=30,
            replay_byte_cap=2_000,
            heartbeat_s=15,
            poll_interval=0.05,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "gap" and s.reason == "replay_truncated" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=3.0)
    assert any(e.kind == "gap" and e.reason == "replay_truncated" for e in seen)


def test_live_tail_yields_appended_events(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=None,
            skip_replay=False,
            replay_window_s=30,
            replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15,
            poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            seen.append(e)
            events = [s for s in seen if s.kind == "event"]
            if len(events) >= 2:
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "LIVE_A"}) + "\n")
        fh.write(json.dumps({"ts": _ts(0), "event_type": "LIVE_B"}) + "\n")
    t.join(timeout=3.0)
    events = [s for s in seen if s.kind == "event"]
    assert [e.event_type for e in events[:2]] == ["LIVE_A", "LIVE_B"]


def test_live_truncation_emits_gap(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E1"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=None,
            skip_replay=False,
            replay_window_s=30,
            replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15,
            poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "gap" and s.reason == "truncated_live" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    audit.write_text("")
    t.join(timeout=3.0)
    assert any(e.kind == "gap" and e.reason == "truncated_live" for e in seen)


def test_live_path_rotation_emits_gap(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E1"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=None,
            skip_replay=False,
            replay_window_s=30,
            replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15,
            poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "gap" and s.reason == "rotated_live" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    audit.unlink()
    with audit.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E2"}) + "\n")
    t.join(timeout=3.0)
    assert any(e.kind == "gap" and e.reason == "rotated_live" for e in seen)


def test_heartbeat_fires_under_silence(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=None,
            skip_replay=False,
            replay_window_s=30,
            replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=0.1,
            poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            if e.kind == "heartbeat":
                seen.append(e)
                if len(seen) >= 2:
                    stop.set()
                    return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=2.0)
    assert len(seen) >= 2


def test_resume_skips_replay_when_cursor_valid(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"ts": _ts(0.1 * i), "event_type": f"E{i}"}) + "\n")
    size = audit.stat().st_size
    st = audit.stat()
    cursor = ResumeCursor(
        per_source={
            "audit": _PerSourceCursor(offset=size, identity_tuple=(st.st_dev, st.st_ino)),
        }
    )
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=cursor,
            skip_replay=True,
            replay_window_s=30,
            replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15,
            poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "event" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "NEW"}) + "\n")
    t.join(timeout=2.0)
    events = [e.event_type for e in seen if e.kind == "event"]
    assert events == ["NEW"], events  # no replay


def test_resume_rotation_emits_gap(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "OLD"}) + "\n")
    st = audit.stat()
    cursor = ResumeCursor(
        per_source={
            "audit": _PerSourceCursor(
                offset=audit.stat().st_size, identity_tuple=(st.st_dev, st.st_ino)
            ),
        }
    )
    audit.unlink()
    with audit.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "NEW"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir,
            resume=cursor,
            skip_replay=True,
            replay_window_s=30,
            replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15,
            poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000,
            max_bytes_per_burst=1_048_576,
            stop=stop,
            verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "gap" and s.reason == "rotated" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=2.0)
    assert any(e.kind == "gap" and e.reason == "rotated" for e in seen)


def test_live_rotation_yields_post_rotation_event(tmp_path: Path) -> None:
    """Guards the record_identity_tuple crash: after rotation the loop
    must NOT die — it must emit the gap AND go on to yield the event
    from the rotated-in file."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E1"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "event" and s.event_type == "E2_NEW" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    audit.unlink()
    with audit.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E2_NEW"}) + "\n")
    t.join(timeout=3.0)
    assert not t.is_alive(), "loop crashed (record_identity_tuple?) — never yielded E2_NEW"
    assert any(e.kind == "gap" and e.reason == "rotated_live" for e in seen)
    assert any(e.kind == "event" and e.event_type == "E2_NEW" for e in seen)


def test_live_events_carry_distinct_per_line_cursors(tmp_path: Path) -> None:
    """§3.5/§7.5: each event in a burst gets its own byte-offset cursor,
    not a shared EOF cursor — otherwise mid-burst reconnect loses events."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            if e.kind == "event":
                seen.append(e)
                if len(seen) >= 3:
                    stop.set()
                    return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    with audit.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"ts": _ts(0), "event_type": f"E{i}"}) + "\n")
    t.join(timeout=3.0)
    cursors = [e.composite_cursor for e in seen]
    assert all(c is not None for c in cursors)
    assert len(set(cursors)) == 3, f"cursors not distinct: {cursors}"
    # Offsets must be strictly increasing.
    offsets = [int(c.split(":")[1]) for c in cursors]
    assert offsets == sorted(offsets) and len(set(offsets)) == 3


def test_gap_marker_carries_recent_cursor(tmp_path: Path) -> None:
    """§6.11: a gap after a live event carries the most-recent cursor."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "gap" and s.reason == "truncated_live" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E1"}) + "\n")
    time.sleep(0.1)
    audit.write_text("")  # truncate → truncated_live gap
    t.join(timeout=3.0)
    gap = next(e for e in seen if e.kind == "gap" and e.reason == "truncated_live")
    assert gap.composite_cursor is not None, "gap must carry the recent live cursor"


def test_iterator_exits_promptly_after_stop(tmp_path: Path) -> None:
    """Spec test plan: iterator exits within ~1s of stop.set()."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    (case_dir / "working" / "audit_events.jsonl").touch()
    stop = threading.Event()

    def consumer() -> None:
        for _e in iter_live_events(
            case_dir, resume=None, skip_replay=True,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.1,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            pass

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(timeout=1.5)
    assert not t.is_alive(), "iterator did not exit promptly after stop.set()"


def test_parse_composite_cursor_rejects_empty_name() -> None:
    assert parse_composite_last_event_id(":1:2:3") is None


import io  # noqa: E402

from dsar_orchestrator.local_broker.live_log_stream import (  # noqa: E402
    write_sse_frame,
)


def test_write_sse_frame_emits_id_event_data() -> None:
    buf = io.BytesIO()
    write_sse_frame(buf, id="audit:1024:1:123", data={"k": 1})
    raw = buf.getvalue().decode("utf-8")
    assert "id: audit:1024:1:123\n" in raw
    assert "event: live-log\n" in raw
    assert 'data: {"k":1}\n\n' in raw


def test_write_sse_frame_omits_id_when_none() -> None:
    buf = io.BytesIO()
    write_sse_frame(buf, id=None, data={"k": 1})
    raw = buf.getvalue().decode("utf-8")
    assert "id:" not in raw
    assert 'data: {"k":1}\n\n' in raw


def test_missing_source_tolerated_then_picked_up_when_it_appears(tmp_path: Path) -> None:
    """A source absent at start (e.g. L3 pipeline.jsonl before the
    conductor runs) must NOT emit source_vanished_live and must NOT be
    disabled — once it appears its events are read (§4.3)."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"  # exists, empty
    audit.touch()
    # A stage source that does not exist yet, enrolled via verbose.
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=True,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "event" and s.event_type == "LATE" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.15)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "LATE"}) + "\n")
    t.join(timeout=3.0)
    # The (missing) L3 'cond' source must NOT have produced a vanish gap.
    assert not any(
        e.kind == "gap" and e.source == "cond" and e.reason == "source_vanished_live"
        for e in seen
    ), "missing-from-start source wrongly reported as vanished"
    assert any(e.kind == "event" and e.event_type == "LATE" for e in seen)


def test_event_cursor_is_full_composite_across_all_sources(tmp_path: Path) -> None:
    """§3.5: the SSE id is a composite cursor over ALL sources, joined
    by '|' (audit + cond at minimum)."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            if e.kind == "event":
                seen.append(e)
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E"}) + "\n")
    t.join(timeout=3.0)
    cur = seen[0].composite_cursor
    assert "|" in cur, f"cursor not composite: {cur}"
    names = {tok.rsplit(":", 3)[0] for tok in cur.split("|")}
    assert {"audit", "cond"} <= names, names
    # And it round-trips through the parser.
    assert parse_composite_last_event_id(cur) is not None


def test_heartbeat_before_any_event_carries_zero_composite(tmp_path: Path) -> None:
    """§3.5 invariant 10: a heartbeat emitted before any event carries
    the all-zeros initial composite, never None."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    (case_dir / "working" / "audit_events.jsonl").touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=0.05, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            if e.kind == "heartbeat":
                seen.append(e)
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=2.0)
    assert seen, "no heartbeat emitted"
    hb = seen[0]
    assert hb.composite_cursor is not None
    # All offsets zero (no event seen yet).
    for tok in hb.composite_cursor.split("|"):
        assert tok.rsplit(":", 3)[1:] == ["0", "0", "0"], tok


def test_replay_events_have_distinct_increasing_cursors(tmp_path: Path) -> None:
    """§3.5/§6.11: each replay event carries its own per-line offset, not
    a shared EOF cursor — so mid-replay reconnect doesn't skip events."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"ts": _ts(5 - i), "event_type": f"R{i}"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.05,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            if e.kind == "event":
                seen.append(e)
                if len(seen) >= 3:
                    stop.set()
                    return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=3.0)
    # audit offset is the first token's offset.
    audit_offsets = [int(e.composite_cursor.split("|")[0].split(":")[1]) for e in seen]
    assert len(set(audit_offsets)) == 3, audit_offsets
    assert audit_offsets == sorted(audit_offsets)


def test_late_appearing_stage_source_is_picked_up(tmp_path: Path) -> None:
    """A verbose L2 stage source absent at start, created mid-stream,
    must be opened and its events emitted (the 'picked up' half of §4.3)."""
    from dsar_orchestrator.local_broker.live_log_stream import (
        _l2_stage_artefact_filenames,
    )

    stage_names = _l2_stage_artefact_filenames()
    if not stage_names:
        pytest.skip("no STAGE_ARTEFACTS available in this environment")
    fn = stage_names[0]

    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    (case_dir / "working" / "audit_events.jsonl").touch()
    stage_path = case_dir / "working" / fn  # does NOT exist yet
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=True,
        ):
            seen.append(e)
            if any(s.kind == "event" and s.event_type == "STAGE_LATE" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.15)
    with stage_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "STAGE_LATE"}) + "\n")
    t.join(timeout=3.0)
    assert not any(
        e.kind == "gap" and e.source == f"stage:{fn}" and e.reason == "source_vanished_live"
        for e in seen
    ), "late-appearing stage source wrongly reported vanished"
    assert any(e.kind == "event" and e.event_type == "STAGE_LATE" for e in seen)


def test_cursor_source_not_opened_emits_source_vanished(tmp_path: Path) -> None:
    """§3.5: a source named in the resume cursor but not opened this
    connection (e.g. a stage file when verbose is off) yields one
    source_vanished gap."""
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    st = audit.stat()
    cursor = ResumeCursor(per_source={
        "audit": _PerSourceCursor(offset=0, identity_tuple=(st.st_dev, st.st_ino)),
        "stage:redaction_decisions.jsonl": _PerSourceCursor(
            offset=10, identity_tuple=(1, 2),
        ),
    })
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=cursor, skip_replay=True,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,  # stage source NOT opened
        ):
            seen.append(e)
            if any(
                s.kind == "gap"
                and s.source == "stage:redaction_decisions.jsonl"
                and s.reason == "source_vanished"
                for s in seen
            ):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=2.0)
    assert any(
        e.kind == "gap"
        and e.source == "stage:redaction_decisions.jsonl"
        and e.reason == "source_vanished"
        for e in seen
    )
