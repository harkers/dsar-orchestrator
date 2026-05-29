"""Live-log streaming primitives for the operator console.

Powers `/live-log/stream` SSE: tails one or more JSONL source files,
merges them through a single iterator, yields `LiveEvent`s for the
SSE handler to project.

Single-thread design: one SSE worker thread owns the iterator,
which owns all source file handles in a try/finally.

Source files are opened in **binary** mode. The composite resume
cursor (§3.5) encodes byte offsets and `fstat_size()` returns bytes,
so all positioning must be byte-exact — text-mode seeks to arbitrary
byte offsets are undefined in CPython. Lines are split on b"\\n" and
decoded per-line with errors="replace".

Spec: 2026-05-29 operator-console-live-log design v2 §4.3, §6.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import BinaryIO
from pathlib import Path


@dataclass
class JsonlTail:
    path: Path
    max_line_bytes: int
    _fh: BinaryIO | None = field(default=None, init=False, repr=False)
    _buffer: bytes = field(default=b"", init=False, repr=False)
    # True while discarding the tail of an over-length line until the
    # next newline (streaming discard, §6.2 — no unbounded buffering).
    _discarding: bool = field(default=False, init=False, repr=False)
    identity_tuple: tuple[int, int] | None = field(default=None, init=False)
    last_known_size: int = field(default=0, init=False)
    disabled: bool = field(default=False, init=False)
    # True once the file has ever been successfully opened. Distinguishes
    # "never appeared yet" (tolerate, wait) from "was present, now gone"
    # (real source_vanished) — survives a failed reopen() (§4.3, §3.5).
    ever_opened: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._open()

    def _open(self) -> None:
        if self._fh is not None:
            return
        try:
            self._fh = self.path.open("rb")
            self.ever_opened = True
        except OSError:
            # Missing OR transiently inaccessible (e.g. PermissionError
            # mid-rotation). Tolerate it (§4.3): leave _fh None and let
            # the caller retry; never crash the SSE worker.
            self._fh = None

    @property
    def name(self) -> str:
        return self.path.name

    def fstat_identity(self) -> tuple[int, int] | None:
        """Identity of the OPEN fd — pinned to the inode held even after
        the path is unlinked/recreated. Compare against
        `stat_path_identity()` to detect rotation (§6.7)."""
        if self._fh is None:
            return None
        st = os.fstat(self._fh.fileno())
        return (st.st_dev, st.st_ino)

    def stat_path_identity(self) -> tuple[int, int] | None:
        """Identity of whatever the PATH points at now — diverges from
        `fstat_identity()` once the file is rotated (§6.7). None when the
        path no longer exists."""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def fstat_size(self) -> int:
        if self._fh is None:
            return 0
        return os.fstat(self._fh.fileno()).st_size

    def consumed_offset(self) -> int:
        """Byte offset of the boundary just past the last CONSUMED line
        — i.e. the fd read position minus what is still buffered but not
        yet yielded. This is the exact resume point for the composite
        cursor (§3.5), accurate per-line within a burst (not EOF)."""
        if self._fh is None:
            return 0
        return self._fh.tell() - len(self._buffer)

    def record_identity_tuple(self) -> None:
        """Pin the current fd identity as the rotation baseline (§6.8)."""
        self.identity_tuple = self.fstat_identity()

    def seek_to(self, offset: int) -> None:
        """Byte-exact seek for Phase A replay / resume (§6.8). Discards
        any buffered/discard state — it belongs to the old fd position."""
        if self._fh is not None:
            self._fh.seek(offset)
            self._buffer = b""
            self._discarding = False

    def reopen(self) -> None:
        """Close the stale fd and reopen the path — used on rotation.
        Drops buffered/discard state from the old file (else stale bytes
        from the rotated-away file would corrupt the new stream)."""
        self.close()
        self._buffer = b""
        self._discarding = False
        self._open()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def read_new_lines(self, *, max_lines: int, max_bytes: int):
        """Yield up to (max_lines, max_bytes) JSON objects from the
        tail. fd left at byte after last complete `\\n` yielded;
        partial trailing line buffered.

        Stateless bounded-batch contract (§6.9): caller MUST NOT
        `break` mid-iteration — that would discard buffered lines.
        """
        if self._fh is None:
            self._open()
            if self._fh is None:
                return
        lines_yielded = 0
        bytes_yielded = 0
        while lines_yielded < max_lines and bytes_yielded < max_bytes:
            # If mid-discard of an over-length line, drop bytes until the
            # next newline. Never let the buffer grow past one chunk (§6.2).
            if self._discarding:
                nl = self._buffer.find(b"\n")
                if nl < 0:
                    # Count discarded bytes toward the budget so a
                    # gigantic over-length line can't do unbounded I/O in
                    # one call (§6.9 bounded-batch).
                    bytes_yielded += len(self._buffer)
                    self._buffer = b""
                    chunk = self._fh.read(65_536)
                    if not chunk:
                        break
                    self._buffer += chunk
                    continue
                bytes_yielded += nl + 1
                self._buffer = self._buffer[nl + 1 :]
                self._discarding = False

            # Drain complete lines already buffered from a prior call
            # FIRST — a previous bounded batch may have left whole lines
            # in `self._buffer` after hitting max_lines/max_bytes. Reading
            # the fd first and breaking on EOF would silently drop them.
            # Every consumed line (yielded, blank, or skipped) counts
            # toward the byte budget so blank/garbage bursts stay bounded.
            while b"\n" in self._buffer and lines_yielded < max_lines and bytes_yielded < max_bytes:
                raw, self._buffer = self._buffer.split(b"\n", 1)
                bytes_yielded += len(raw) + 1
                if len(raw) > self.max_line_bytes:
                    yield {"_kind": "line_too_long"}
                    lines_yielded += 1
                    continue
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                yield obj
                lines_yielded += 1
            if lines_yielded >= max_lines or bytes_yielded >= max_bytes:
                break
            # No complete line buffered. If what we already hold exceeds
            # the per-line cap with no newline in sight, it's an
            # over-length line: emit one marker and stream-discard the
            # rest (rather than buffering it all → OOM, §6.2).
            if b"\n" not in self._buffer and len(self._buffer) > self.max_line_bytes:
                self._discarding = True
                bytes_yielded += len(self._buffer)
                self._buffer = b""
                yield {"_kind": "line_too_long"}
                lines_yielded += 1
                continue
            chunk = self._fh.read(65_536)
            if not chunk:
                break
            self._buffer += chunk


# ----------------------------------------------------------------------
# Block D — composite resume cursor
# ----------------------------------------------------------------------


@dataclass
class _PerSourceCursor:
    offset: int
    identity_tuple: tuple[int, int]


@dataclass
class ResumeCursor:
    per_source: dict[str, _PerSourceCursor]

    def is_valid(self) -> bool:
        if not self.per_source:
            return False
        return all(p.offset >= 0 for p in self.per_source.values())


INITIAL_ZERO_CURSOR = ResumeCursor(per_source={})


_MAX_LAST_EVENT_ID_BYTES = 4 * 1024


def parse_composite_last_event_id(header: str | None) -> ResumeCursor | None:
    """Exception-safe parser. Returns None on any malformation.

    Format per source: `<name>:<offset>:<st_dev>:<st_ino>`, joined by
    `|`. Source name may contain `:` (e.g. `stage:filename.jsonl`):
    the LAST THREE colon-tokens are offset/dev/ino; everything before
    is the name.
    """
    if not header:
        return None
    if len(header.encode("utf-8")) > _MAX_LAST_EVENT_ID_BYTES:
        return None
    try:
        per_source: dict[str, _PerSourceCursor] = {}
        for chunk in header.split("|"):
            parts = chunk.split(":")
            if len(parts) < 4:
                return None
            name = ":".join(parts[:-3])
            if not name:
                return None
            offset, dev, ino = int(parts[-3]), int(parts[-2]), int(parts[-1])
            if offset < 0 or dev < 0 or ino < 0:
                return None
            if offset > 2**63 or dev > 2**63 or ino > 2**63:
                return None
            per_source[name] = _PerSourceCursor(
                offset=offset,
                identity_tuple=(dev, ino),
            )
        cursor = ResumeCursor(per_source=per_source)
        if not cursor.is_valid():
            return None
        return cursor
    except (ValueError, OverflowError, AttributeError, TypeError):
        return None


@dataclass
class LiveEvent:
    kind: str  # "event" | "gap" | "heartbeat"
    source: str
    ts: str | None = None
    event_type: str | None = None
    payload: dict | None = None
    reason: str | None = None
    composite_cursor: str | None = None


# ----------------------------------------------------------------------
# Block D — source opener + merging iterator
# ----------------------------------------------------------------------

_AUDIT_FILENAME = "audit_events.jsonl"
_L3_AUDIT_ROOT_DIRNAME = ".dsar-audit"
_L3_FILENAME = "pipeline.jsonl"


def _l3_pipeline_jsonl_path(case_dir: Path) -> Path:
    """L3 source lives at ~/.dsar-audit/<case_no>/pipeline.jsonl,
    NOT under the case_dir. case_no = case_dir.name (the case folder
    is named after its case_no per dsar-orchestrator convention)."""
    case_no = Path(case_dir).name
    return Path.home() / _L3_AUDIT_ROOT_DIRNAME / case_no / _L3_FILENAME


def _l2_stage_artefact_filenames() -> list[str]:
    """STAGE_ARTEFACTS is a dict[phase, list[filename]]. Flatten + dedup."""
    try:
        from dsar_orchestrator.operator_console import STAGE_ARTEFACTS
    except (ImportError, AttributeError):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for filenames in STAGE_ARTEFACTS.values():
        for fn in filenames:
            # Defence-in-depth: only bare filenames join under working/.
            # Reject anything with a path separator or `..` so a stray
            # STAGE_ARTEFACTS entry can't escape the case directory.
            if Path(fn).name != fn or fn == "..":
                continue
            if fn not in seen and fn.endswith(".jsonl"):
                seen.add(fn)
                out.append(fn)
    return out


def open_sources(
    case_dir: Path,
    *,
    max_line_bytes: int,
    verbose_l2: bool,
) -> dict[str, JsonlTail]:
    """Open L1 (always) + L3 (always) + L2 (if verbose). Returns a
    dict keyed by source name: 'audit', 'cond', or 'stage:<filename>'.
    """
    working = Path(case_dir) / "working"
    sources: dict[str, JsonlTail] = {
        "audit": JsonlTail(
            working / _AUDIT_FILENAME,
            max_line_bytes=max_line_bytes,
        ),
        "cond": JsonlTail(
            _l3_pipeline_jsonl_path(case_dir),
            max_line_bytes=max_line_bytes,
        ),
    }
    if verbose_l2:
        for fn in _l2_stage_artefact_filenames():
            sources[f"stage:{fn}"] = JsonlTail(
                working / fn,
                max_line_bytes=max_line_bytes,
            )
    return sources


def _source_token(name: str, src: JsonlTail, *, offset: int | None = None) -> str:
    if offset is None:
        offset = src.consumed_offset()
    dev, ino = src.fstat_identity() or (0, 0)
    return f"{name}:{offset}:{dev}:{ino}"


def _composite_cursor(
    sources: dict[str, JsonlTail],
    *,
    overrides: dict[str, int] | None = None,
) -> str:
    """The SSE `id`: a `|`-joined composite cursor across ALL sources
    (§3.5). Each token is `name:offset:st_dev:st_ino`. `overrides` pins
    a specific byte offset for a source (e.g. the per-line offset of a
    replay event) instead of its current consumed-offset."""
    overrides = overrides or {}
    return "|".join(
        _source_token(name, src, offset=overrides.get(name)) for name, src in sources.items()
    )


def _initial_zero_cursor(sources: dict[str, JsonlTail]) -> str:
    """The all-zeros initial composite (§3.5, invariant 10) — carried by
    heartbeats/gaps emitted before any real event."""
    return "|".join(f"{name}:0:0:0" for name in sources)


def _phase_a_replay(
    sources: dict[str, JsonlTail],
    *,
    replay_window_s: float,
    replay_byte_cap: int,
):
    """Linear backwards scan from each source's EOF, capped at
    `replay_byte_cap`. Merge cross-source by timestamp. Yields
    LiveEvents (event or gap). Leaves each source's fd at EOF; Phase B
    continues from there.
    """
    import heapq
    from datetime import UTC, datetime, timedelta

    # Compare only the second-precision prefix (YYYY-MM-DDTHH:MM:SS),
    # which is uniform across the `Z` (L1/L2) and `+00:00` (L3 producer)
    # suffixes. Lexicographic comparison of the full strings would be
    # unreliable across those formats; second precision is ample for a
    # 30 s replay window.
    cutoff = (datetime.now(UTC) - timedelta(seconds=replay_window_s)).isoformat()[:19]

    # Each tuple: (ts, payload, name, end_offset) where end_offset is the
    # byte position just past this line's newline — its per-line cursor.
    per_source_events: dict[str, list[tuple[str, dict, str, int]]] = {}
    # Per-source resume floor: the offset replay began at (snapped to a
    # newline). A reconnecting client that has not yet seen an event from
    # a given source resumes from this floor, never EOF.
    replay_start: dict[str, int] = {}

    for name, src in sources.items():
        replay_start[name] = src.fstat_size()
        size = src.fstat_size()
        if size == 0 or src._fh is None:
            per_source_events[name] = []
            continue
        scan_start = max(0, size - replay_byte_cap)
        if scan_start > 0:
            yield LiveEvent(kind="gap", source=name, reason="replay_truncated")
        src._fh.seek(scan_start)
        chunk = src._fh.read(size - scan_start)
        pos = scan_start
        if scan_start > 0:
            # Re-sync to first newline: discard any partial line at start.
            nl = chunk.find(b"\n")
            if nl >= 0:
                pos += nl + 1
                chunk = chunk[nl + 1 :]
        # Drop a trailing partial line (no terminating newline): it is
        # buffered for the next \n, not replayed (invariant 1).
        raw_lines = chunk.split(b"\n")
        if chunk and not chunk.endswith(b"\n"):
            raw_lines = raw_lines[:-1]
        events: list[tuple[str, dict, str, int]] = []
        first_event_start: int | None = None
        for raw in raw_lines:
            line_start = pos
            pos += len(raw) + 1  # advance past this line + its newline
            if not raw.strip():
                continue
            if len(raw) > src.max_line_bytes:
                # Don't json.loads a multi-MB blob; surface it (§6.2).
                yield LiveEvent(kind="gap", source=name, reason="line_too_long")
                continue
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            ts = payload.get("ts") or payload.get("timestamp")
            if not isinstance(ts, str):
                continue
            if ts[:19] >= cutoff:
                if first_event_start is None:
                    first_event_start = line_start
                events.append((ts, payload, name, line_start + len(raw) + 1))
        per_source_events[name] = events
        # Resume floor for a client that has seen no event from this
        # source yet: the start of its first in-window event. If there
        # were NO in-window events, everything scanned is pre-window
        # noise — resume from EOF so it is never re-streamed as "live".
        replay_start[name] = first_event_start if first_event_start is not None else size
        # Leave fd at EOF for Phase B.
        src._fh.seek(0, 2)

    # Walk the merged stream maintaining each source's running resume
    # offset so every emitted cursor is an accurate composite across ALL
    # sources (not EOF for the non-emitting ones).
    running = dict(replay_start)
    for ts, payload, name, end_offset in heapq.merge(
        *[iter(events) for events in per_source_events.values()],
        key=lambda t: t[0],
    ):
        running[name] = end_offset
        # L3 dispatch: L3 rows carry `event`, L1/L2 carry `event_type`.
        event_type = payload.get("event_type") or payload.get("event") or payload.get("template_id")
        yield LiveEvent(
            kind="event",
            source=name,
            ts=ts,
            event_type=event_type,
            payload=payload,
            composite_cursor=_composite_cursor(sources, overrides=dict(running)),
        )


def _phase_a_skip_to_cursor(
    sources: dict[str, JsonlTail],
    *,
    resume: ResumeCursor,
    replay_byte_cap: int,
):
    """Identity-validate before seek; emit gap markers for rotated /
    truncated / window-exceeded / vanished sources. Leaves each
    source's fd positioned ready for Phase B.
    """
    for name, src in sources.items():
        cursor_state = resume.per_source.get(name)
        if cursor_state is None:
            if src._fh is not None:
                src._fh.seek(0, 2)
            continue
        live_id = src.stat_path_identity()
        if live_id is None:
            yield LiveEvent(kind="gap", source=name, reason="source_vanished")
            src.disabled = True
            continue
        if live_id != cursor_state.identity_tuple:
            # The file the cursor referenced was rotated away. reopen()
            # guarantees the fd points at the CURRENT inode (not a stale
            # held one), then read from its head.
            yield LiveEvent(kind="gap", source=name, reason="rotated")
            src.reopen()
            src.record_identity_tuple()
            continue
        size = src.fstat_size()
        if cursor_state.offset > size:
            yield LiveEvent(kind="gap", source=name, reason="truncated")
            src.seek_to(0)
            continue
        if size - cursor_state.offset > replay_byte_cap:
            yield LiveEvent(
                kind="gap",
                source=name,
                reason="resume_window_exceeded",
            )
            target = max(0, size - replay_byte_cap)
            if src._fh is not None:
                src._fh.seek(target)
                tail = src._fh.read(min(replay_byte_cap, size - target))
                nl = tail.find(b"\n")
                if nl >= 0:
                    src.seek_to(target + nl + 1)
                else:
                    src.seek_to(0)
            continue
        src.seek_to(cursor_state.offset)

    # Sources named in the cursor but not opened this connection (e.g. an
    # L2 stage file that was enrolled under verbose last time but not now)
    # yield one source_vanished gap each (§3.5).
    for name in resume.per_source:
        if name not in sources:
            yield LiveEvent(kind="gap", source=name, reason="source_vanished")


def iter_live_events(
    case_dir,
    *,
    resume,
    skip_replay,
    replay_window_s,
    replay_byte_cap,
    heartbeat_s,
    poll_interval,
    max_line_bytes,
    max_lines_per_burst,
    max_bytes_per_burst,
    stop,
    verbose_l2,
):
    """See module docstring + spec §4.3. Single-thread merged iterator:
    Phase A (replay or skip-to-cursor) then Phase B (live tail)."""
    import time as _time

    sources: dict[str, JsonlTail] = {}
    try:
        sources = open_sources(
            Path(case_dir),
            max_line_bytes=max_line_bytes,
            verbose_l2=verbose_l2,
        )
        # Every non-heartbeat event must carry a composite_cursor, and
        # gaps/heartbeats carry the MOST RECENT live cursor (§6.10,
        # §6.11) so a reconnecting client never desyncs. `_stamp`
        # centralises this: events already set their own cursor; gaps
        # and heartbeats inherit `last_cursor`. Before any event is
        # emitted, that is the all-zeros initial composite (invariant 10).
        last_cursor: str = _initial_zero_cursor(sources)

        def _stamp(ev: LiveEvent) -> LiveEvent:
            nonlocal last_cursor
            if ev.composite_cursor is not None:
                last_cursor = ev.composite_cursor
            elif ev.kind in ("gap", "heartbeat"):
                ev.composite_cursor = last_cursor
            return ev

        if skip_replay and resume is not None and resume.is_valid():
            for ev in _phase_a_skip_to_cursor(
                sources,
                resume=resume,
                replay_byte_cap=replay_byte_cap,
            ):
                yield _stamp(ev)
        elif not skip_replay:
            for ev in _phase_a_replay(
                sources,
                replay_window_s=replay_window_s,
                replay_byte_cap=replay_byte_cap,
            ):
                yield _stamp(ev)
        else:
            # skip_replay requested but no valid cursor: start the live
            # tail from CURRENT EOF (not byte 0) so pre-existing history
            # is not re-streamed as "live".
            for src in sources.values():
                if src._fh is not None:
                    src._fh.seek(0, 2)

        # Initialise identity baselines for the live-tail loop.
        for src in sources.values():
            src.record_identity_tuple()
            src.last_known_size = src.fstat_size()

        last_heartbeat_ts = _time.monotonic()
        while not stop.is_set():
            for name, src in sources.items():
                if stop.is_set():
                    return
                if src.disabled:
                    continue
                path_id = src.stat_path_identity()
                if path_id is None:
                    # Not present right now. Only a *disappearance* of a
                    # source that was previously seen is a real gap. A
                    # source that has never appeared yet (e.g. the L3
                    # pipeline.jsonl before the conductor first runs) is
                    # tolerated and retried — §4.3 "wait for sources to
                    # appear". Disabling it here would make it invisible
                    # forever once it finally shows up. `ever_opened`
                    # distinguishes the two cases and survives a failed
                    # reopen() (rotate-then-immediately-deleted).
                    if src.ever_opened:
                        yield _stamp(
                            LiveEvent(
                                kind="gap",
                                source=name,
                                reason="source_vanished_live",
                            )
                        )
                        src.disabled = True
                    continue
                if src._fh is None:
                    # A previously-missing source has now appeared.
                    src._open()
                    src.record_identity_tuple()
                    src.last_known_size = 0
                elif src.identity_tuple is not None and path_id != src.identity_tuple:
                    yield _stamp(
                        LiveEvent(
                            kind="gap",
                            source=name,
                            reason="rotated_live",
                        )
                    )
                    src.reopen()
                    src.record_identity_tuple()
                    src.last_known_size = src.fstat_size()
                elif src.identity_tuple is None:
                    src.identity_tuple = path_id
                fd_size = src.fstat_size()
                if fd_size < src.last_known_size:
                    yield _stamp(
                        LiveEvent(
                            kind="gap",
                            source=name,
                            reason="truncated_live",
                        )
                    )
                    src.seek_to(0)
                src.last_known_size = fd_size

                line_count = 0
                for evt in src.read_new_lines(
                    max_lines=max_lines_per_burst,
                    max_bytes=max_bytes_per_burst,
                ):
                    if evt.get("_kind") == "line_too_long":
                        # No explicit cursor: mid-discard the fd may be
                        # inside the over-length line (non-newline-aligned),
                        # so let _stamp carry the last good composite cursor
                        # rather than snapshot a mid-line offset (invariant 3).
                        yield _stamp(
                            LiveEvent(
                                kind="gap",
                                source=name,
                                reason="line_too_long",
                            )
                        )
                        continue
                    ts = evt.get("ts") or evt.get("timestamp")
                    event_type = evt.get("event_type") or evt.get("event") or evt.get("template_id")
                    yield _stamp(
                        LiveEvent(
                            kind="event",
                            source=name,
                            ts=ts,
                            event_type=event_type,
                            payload=evt,
                            composite_cursor=_composite_cursor(sources),
                        )
                    )
                    line_count += 1
                    if line_count % 100 == 0:
                        if stop.is_set():
                            return
                        now = _time.monotonic()
                        if now - last_heartbeat_ts >= heartbeat_s:
                            yield _stamp(LiveEvent(kind="heartbeat", source=name))
                            last_heartbeat_ts = now
            now = _time.monotonic()
            if now - last_heartbeat_ts >= heartbeat_s:
                yield _stamp(LiveEvent(kind="heartbeat", source="*"))
                last_heartbeat_ts = now
            if stop.wait(poll_interval):
                return
    finally:
        for src in sources.values():
            src.close()


# ----------------------------------------------------------------------
# SSE framing helper
# ----------------------------------------------------------------------


def write_sse_frame(wfile, *, id: str | None, data: dict) -> None:
    """Write one SSE frame (id, event, data) to wfile. Raises
    BrokenPipeError / OSError on dead connections. Caller flushes.
    """
    buf = []
    if id is not None:
        buf.append(f"id: {id}\n")
    buf.append("event: live-log\n")
    buf.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n")
    wfile.write("".join(buf).encode("utf-8"))
