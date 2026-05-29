# Operator console live-log feed — design v1

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-05-29 | First written spec. Synthesised from a 7-round brainstorm-jury process (Kimi / Gemini / Qwen3-Coder via LiteLLM); 2/3 approve reached at round 7. Architecture sections (§Goal, §Sources, §Architecture, §Implementation invariants) are the merged final design; §Open questions captures the small set of remaining narrow concerns (deferred to implementation discipline + tests, not blocking). |

## 1. Goal

A live, in-play event view inside the dsar-orchestrator operator console
(`src/dsar_orchestrator/operator_console.py`). Today the console only
shows post-hoc aggregates: counts, summaries, decision queues. Operators
working a UK GDPR Article-15 DSAR want to open a page and watch the
pipeline work in real time as it processes the case — the analogue is
Plex's server console "Now playing / now transcoding" view.

The live feed is a **live observation surface, not an audit-substantiation
surface.** The hash-chained `working/audit_events.jsonl` and per-stage
decision/finding files remain the canonical audit record. The browser
view is a *derived window* onto those files. This framing justifies the
backpressure stance (§5.4) and the absence of cryptographic chain
verification in the live path.

## 2. Context constraints

- Console is **single-case**, launched with `--case-dir <path>`. One
  process serves one case.
- HTTP server is **stdlib only**: `http.server.ThreadingHTTPServer` +
  `BaseHTTPRequestHandler`. Bound to `127.0.0.1` by default. NO Flask /
  FastAPI / aiohttp.
- Auth posture: same-origin / localhost bind. No new auth surface.
- `working/audit_events.jsonl` is hash-chained, Article-30 / ROPA-bearing,
  rich payloads. Some payload fields carry PII
  (`subject_protected_phrases`, `example_tokens`, doc excerpts, file
  paths under `/Volumes/<client>/` client-bundle prefixes).
- Per-stage decision/finding jsonls are catalogued in
  `operator_console._RUNNING_STATE_FILE_HINTS` and live in the same
  `working/` directory.
- The conductor (dsar-conductor) currently prints to stdout only; no log
  file exists on disk.

## 3. Decisions (locked)

### 3.1 Sources (three, layered)

- **L1** `working/audit_events.jsonl` — primary. The Article-30
  canonical event log.
- **L2** per-stage jsonls — secondary, behind a verbose toggle (off by
  default). Filenames sourced from
  `operator_console._RUNNING_STATE_FILE_HINTS`. Examples:
  `redaction_decisions.jsonl`, `context_classifications.jsonl`,
  `qc_findings_07a.jsonl`.
- **L3** `working/conductor_events.jsonl` — **NEW**. Structured JSON
  Lines, not free text. One row per conductor banner / milestone, of
  shape:
  ```json
  {"ts":"…","level":"INFO","template_id":"STAGE_STARTED",
   "fields":{"stage":"redact","phase":"redaction_running","ts_start":"…"}}
  ```
  `template_id` indexes a bounded registry of conductor message
  templates (e.g. `STAGE_STARTED`, `STAGE_COMPLETED`, `GATE_OPENED`,
  `MODULE_WORK_CHECK_FAIL`). `fields` are bounded-shape values
  (enums / integers / ISO-8601 timestamps). Adding a new template
  requires editing the registry, which is a code-review surface. PII
  by construction impossible for L3.

L3 uses the **same `project_for_browser` allowlist machinery** as L1/L2
— no special path.

### 3.2 PII projection

Per-event-type **field allowlist**.

- **Fail-closed default.** If `event_type` is not in `_ALLOWLIST`, the
  projection returns
  `{kind: "event", ts, source, summary: "(unrecognised event type)"}`
  — no other fields, no payload values.
- **Bounded-enum value scrubbing.** Each allowlisted field declares an
  expected shape (regex / numeric range / enum literal set). At
  projection time, values that don't match the declared shape are
  replaced with the literal string `<typeerror>` and a debug record is
  appended to a server-side dev log (PII-safe — see §6.6).
- The `summary` string is composed by `summary_for(event_type,
  projected_fields)` from **already-projected (scrubbed)** values
  only. Never from raw payload.
- The browser NEVER receives raw payload bodies. There is no
  `detail_url` and no detail endpoint. Operators wanting the full
  payload read the JSONL files via the existing `/file` route (which
  applies its own redaction discipline).

### 3.3 Replay window on first connect

Last **30 seconds** across all sources, single merged iterator (no
separate replay-then-live handoff). Linear backwards-from-EOF scan;
each source capped at **16 MiB** of replay scan. If the cap is hit,
emit `{kind:"gap", source, reason:"replay_truncated"}` once and start
from the 16 MiB head.

### 3.4 Backpressure / hung-client safety

In the single-thread design there is **no in-stream drop**. The
handler reads from disk and writes to socket on the same thread.

- `SO_KEEPALIVE` on the accepted socket.
- `socket.settimeout(30 s)` — a stalled `wfile.write` raises
  `socket.timeout` rather than blocking the worker forever. Treated
  identically to `BrokenPipeError`: handler exits, stop event set.
- **Heartbeat** runs on an **independent monotonic schedule** every
  15 s. Each heartbeat write doubles as a dead-client probe. Heartbeat
  cadence is enforced both between source iterations AND between
  bursts of lines from a single high-throughput source — see §6.
- Gap markers are emitted only when source-file state diverges from the
  cursor's expectation (rotation, truncation, source vanished, resume
  backlog exceeded, line-too-long, replay-truncated). They are never
  used to signal a slow client.

### 3.5 Reconnect resumption

Via SSE-native `Last-Event-ID` header. The `id` field on every SSE
frame is a **composite cursor** encoding offsets for ALL sources, e.g.
```
audit:1024:1:123|stage:redaction_decisions.jsonl:512:1:456|cond:2048:1:789
```
Format per source: `<name>:<offset>:<st_dev>:<st_ino>`.

- **`parse_composite_last_event_id` is exception-safe.** A malformed
  header for ANY reason (missing source, non-numeric, malformed
  identity) MUST be caught and treated as `resume=None` → full Phase
  A replay. Server NEVER 500s on a bad client cursor.
- **Identity-validate BEFORE seek.** `_phase_a_skip_to_cursor` opens
  each source, reads `(st_dev, st_ino)` of the opened file, compares
  to the cursor's stored tuple. On mismatch, yield
  `{kind:"gap", source, reason:"rotated"}` and start that source from
  head. No seek into stale offsets.
- **Resume-skips-replay.** If `Last-Event-ID` is present and valid,
  Phase A 30 s replay is skipped entirely; the server resumes from
  cursor offsets directly.
- **Resume backlog cap.** Same 16 MiB cap as initial replay. If
  `current_file_size - cursor.offset > 16 MiB`, emit
  `{kind:"gap", source, reason:"resume_window_exceeded"}` and seek to
  `current_file_size - 16 MiB` (snapped to next `\n`).
- **Source-vanished tolerance.** A source listed in the cursor that
  no longer exists yields one
  `{kind:"gap", source, reason:"source_vanished"}` and stops being
  tracked.
- **Heartbeat carries cursor.** `LiveEvent(kind="heartbeat")` carries
  the most recent non-heartbeat composite cursor so a long quiet
  period does not desync the client. If no event has yet been
  emitted, the cursor is the all-zeros initial composite.

### 3.6 Identity-tuple semantics (the v5/v6 bug)

Rotation identity tuple is **`(st_dev, st_ino)` only.** `st_mtime` is
NOT in the tuple — it changes on every append, which would make the
rotation check fire continuously. On filesystems where inodes can be
recycled within one process lifetime, the practical impact is one
spurious `rotated_live` per recycled inode; acceptable for v1.

## 4. Architecture

### 4.1 Routes

| Route | Method | Description |
|---|---|---|
| `/live-log` | GET | HTML page; vanilla-JS `EventSource` subscription |
| `/live-log/stream` | GET | SSE handler; tails sources and projects events |

No detail route. Operators with FS access read the JSONL files directly
through the existing `/file` route.

### 4.2 SSE handler loop (`operator_console.ConsoleHandler.handle_live_log_stream`)

Single thread per client (the `ThreadingHTTPServer` worker thread):

```python
self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
self.connection.settimeout(30.0)
self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
send_sse_headers(self.wfile)

try:
    resume = parse_composite_last_event_id(
        self.headers.get("Last-Event-ID")
    )
except Exception:
    resume = None
skip_replay = resume is not None and resume.is_valid()
last_cursor = INITIAL_ZERO_CURSOR
stop = threading.Event()

for event in iter_live_events(
    case_dir,
    resume=resume,
    skip_replay=skip_replay,
    replay_window_s=30,
    replay_byte_cap=16 * 1024 * 1024,
    heartbeat_s=15,
    poll_interval=0.5,
    max_line_bytes=1 * 1024 * 1024,
    max_lines_per_burst=5000,
    max_bytes_per_burst=1 * 1024 * 1024,
    stop=stop,
):
    # MUST: last_cursor updated BEFORE we choose the heartbeat cursor.
    if event.kind != "heartbeat":
        last_cursor = event.composite_cursor
    cursor_for_frame = (last_cursor if event.kind == "heartbeat"
                        else event.composite_cursor)

    try:
        projected = project_for_browser(event)
    except Exception as exc:
        log_server_side(
            "project_for_browser failed",
            source=event.source,
            event_type=getattr(event, "event_type", None),
            ts=getattr(event, "ts", None),
            error_type=type(exc).__name__,
            exc_info=False,                  # MUST: no tracebacks
        )
        projected = {
            "kind": "error",
            "source": event.source,
            "msg": "projection_failed",
            "ts": getattr(event, "ts", None),
        }

    try:
        write_sse_frame(self.wfile, id=cursor_for_frame, data=projected)
        self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, socket.timeout):
        stop.set()
        return
    except Exception as exc:
        log_server_side("sse write failed",
                        source=event.source,
                        error_type=type(exc).__name__,
                        exc_info=False)
        stop.set()
        return
```

### 4.3 Helper module

New module
`src/dsar_orchestrator/local_broker/live_log_stream.py`:

- `iter_live_events(case_dir, *, resume, skip_replay, replay_window_s,
   replay_byte_cap, heartbeat_s, poll_interval, max_line_bytes,
   max_lines_per_burst, max_bytes_per_burst, stop)` — single
  generator combining replay and live tail. Owns source file
  descriptors in a `try/finally`. See §6 for invariants.
- `JsonlTail` — source class for `audit_events.jsonl` and L2 jsonls.
  Owns an fd + a per-source partial-line buffer.
- `StructuredLogTail` — source class for L3 (same JSONL shape; separate
  class for readability).
- `open_sources(case_dir, resume, max_line_bytes)` — opens L1 + (if
  verbose) L2 file set + L3. Returns a dict keyed by source name.
- `_phase_a_replay(sources, replay_window_s, replay_byte_cap)` —
  initial 30 s backwards scan, heapq-merged in timestamp order; yields
  events + returns per-source starting offsets.
- `_phase_a_skip_to_cursor(sources, resume, replay_byte_cap)` —
  identity-validate-before-seek; yields gap markers; returns
  per-source starting offsets.
- `project_for_browser(event) -> ProjectedEvent` — table-driven by
  `event_type`; fail-closed default; bounded-enum value scrubber.
- `summary_for(event_type, projected_fields) -> str` — composes from
  projected (scrubbed) fields only.
- `parse_composite_last_event_id(header) -> ResumeCursor | None` —
  exception-safe; bounds the header length at 4 KiB (longer →
  treated as malformed).

### 4.4 Conductor change (`src/dsar_orchestrator/pipeline.py`)

Add `ConductorEventLog` — a thin structured-event writer:

```python
class ConductorEventLog:
    def __init__(self, case_dir: Path):
        self.path = case_dir / "working" / "conductor_events.jsonl"
        # idempotency: lookup an _emitter_registry keyed by case_dir
    def emit(self, template_id: str, **fields) -> None:
        registry_entry = _CONDUCTOR_TEMPLATES[template_id]   # KeyError if unknown
        for field_name, value in fields.items():
            registry_entry.validate_field(field_name, value)  # raises on shape mismatch
        line = json.dumps({"ts": utc_now(), "level": registry_entry.level,
                           "template_id": template_id, "fields": fields},
                          ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
```

- `_CONDUCTOR_TEMPLATES` is a Python dict mapping `template_id` →
  `ConductorTemplate(level, allowed_fields, field_shapes)`. Examples:
  - `STAGE_STARTED` — `{stage: enum, phase: enum, ts_start: iso8601}`
  - `STAGE_COMPLETED` — `{stage: enum, phase: enum, items_processed: int}`
  - `GATE_OPENED` — `{stage: enum, reason: enum}`
  - `MODULE_WORK_CHECK_FAIL` — `{stage: enum, error_type: enum}`
- `field_shapes` accept only:
  - regex literal matches for enum-string fields,
  - integer ranges,
  - ISO-8601 timestamp pattern.
  Document-text strings, candidate-identifier strings, file paths
  under client-bundle prefixes — none of these can be passed as fields.
- Existing `StageBanner` / pipeline-stdout emitters mirror to
  `ConductorEventLog.emit(...)` in addition to their existing print.
  Idempotency: `_emitter_registry` keyed by `case_dir` prevents
  duplicate writers on reruns.

### 4.5 Data contract (server → browser)

Live event frame:
```
id: audit:1024:1:123|stage:redaction_decisions.jsonl:512:1:456|cond:2048:1:789
event: live-log
data: {"kind":"event","ts":"2026-05-29T10:42:11.123Z",
        "source":"audit","stage":"redact",
        "event_type":"REDACT_COMPLETED",
        "severity":"info",
        "summary":"redacted 412 refs"}

```

Gap marker frame (carries the most recent live composite cursor):
```
id: <last live cursor>
event: live-log
data: {"kind":"gap","source":"audit","reason":"rotated_live"}

```

Heartbeat frame:
```
id: <last live cursor>
event: live-log
data: {"kind":"heartbeat","ts":"…"}

```

`gap_reason` enum:
```
rotated | rotated_live | truncated | truncated_live |
source_vanished | source_vanished_live | resume_window_exceeded |
replay_truncated | line_too_long | projection_failed
```

### 4.6 Browser UX

One HTML page, vanilla JS, no framework. Single `EventSource(
"/live-log/stream")` subscription.

- Newest-at-bottom row table; severity color hint (none / warn / error).
- Top controls: **Pause/Resume** (client-side DOM freeze ONLY; server
  keeps streaming so we don't desync); **Source** checkboxes
  (audit / stage / conductor); **Severity** filter; free-text filter
  on `summary` substring.
- Autoscroll when scrolled-to-bottom; manual scroll pauses autoscroll
  (standard tail behaviour).
- Footer "gap N events" badge on any gap marker; click reveals the
  reason and a deep-link to the relevant JSONL file via `/file`.

## 5. Non-goals (out of scope for v1)

- No detail route. No `detail_url` field.
- No replay/rewind controls beyond the 30 s window + Last-Event-ID
  resume.
- No multi-case aggregation. (Console is single-case.)
- No per-event RBAC. (Bound to 127.0.0.1.)
- No persistence of the live view itself (the jsonl files ARE the
  persistence).
- No alerting / notifications.
- No server-side metrics endpoint (Qwen R3 raised; deferred).
- No sub-second polling latency (0.5 s is the contract; documented).

## 6. Implementation invariants (MUST-level)

These are spec-enforced; tests assert each.

1. **JSONL line parsing snaps to `\n`.** Trailing partial line is
   buffered until the next `\n` arrives.
2. **Max line length 1 MiB.** Exceed → emit
   `{kind:"gap", source, reason:"line_too_long"}`. Recovery is
   **streaming discard via 64 KiB chunks** until the next `\n`; the
   discard buffer is single-allocated, per-source-isolated, and
   reused. Naïve `fh.read()` of the tail is forbidden — would
   re-introduce the OOM.
3. **Composite-cursor offsets snapped to `\n` at write time.** Mid-line
   resume is unreachable by construction.
4. **Backwards Phase A scan decodes with `errors="replace"`** and
   re-syncs to the first `\n` before parsing.
5. **Source file handles owned by `iter_live_events`** and closed in a
   `try/finally`. No FD leaks on disconnect.
6. **PII-safe server-side error log.** Projection-failure log calls
   pass `exc_info=False` and serialise only
   `{source, event_type, ts, error_type}`. Tracebacks retain frame
   locals (the raw `event`) — forbidden.
7. **Identity-tuple API split:**
   - `JsonlTail.fstat_identity()` returns `(st_dev, st_ino)` of the
     held fd; used for size / truncation checks (cannot diverge from
     the fd's stored identity by definition).
   - `JsonlTail.stat_path_identity()` returns `(st_dev, st_ino)` of
     the path via a separate `os.stat(path)` call (None if path
     missing); used for rotation detection by comparison against the
     fd's stored identity.
   - Phase B rotation check: `stat_path_identity()` ≠
     `src.identity_tuple` → `rotated_live`, reopen, re-record.
   - Phase B truncation check: `fstat_size()` < `src.last_known_size`
     → `truncated_live`, reset to head.
8. **Phase A identity-init covers every tracked source.** After
   `_phase_a_skip_to_cursor` / `_phase_a_replay`, every source the
   iterator continues to track has both `seek_to(offset)` and
   `record_identity_tuple()` called. Rotated/vanished sources are
   set to `disabled = True` and the Phase B loop skips them. No
   uninitialised identity.
9. **Burst cap lives INSIDE `read_new_lines`.** The method signature
   is `read_new_lines(max_lines, max_bytes)`. It reads up to that
   bound, yields events, and leaves the fd at the byte after the
   last yielded `\n`. The caller does NOT `break` mid-iteration —
   that would discard buffered lines because the fd has already
   advanced. Stateless bounded-batch contract.
10. **`last_cursor` updated BEFORE heartbeat emission.** In the SSE
    handler: if `event.kind != "heartbeat"`, update `last_cursor`
    immediately; choose the SSE frame `id` afterward. Otherwise a
    quiet period followed by a heartbeat could ship a stale or
    initial-zero cursor.
11. **All non-heartbeat events carry `composite_cursor`.** Including
    gap markers (`event.composite_cursor` is set to the most recent
    cursor from the affected source, with the gap reason annotated).
    Tests cover this for every gap reason.
12. **Heartbeat scheduling is independent of yield activity.** Tracked
    by `last_heartbeat_ts`, a monotonic clock initialised at Phase B
    entry. Heartbeat check runs both:
    (a) inside the per-source burst, every 100 yielded lines, AND
    (b) at the end of the per-poll multiplexer loop.
13. **`parse_composite_last_event_id` is exception-safe.** Any failure
    (KeyError, ValueError, AttributeError, …) → fall back to
    `resume=None`. Header length capped at 4 KiB; longer is treated
    as malformed. Server never 500s on a bad client cursor.
14. **`snap_offset_to_newline(target)` floor = 0.** Backwards scan
    for `\n` is bounded by `replay_byte_cap`; on miss, snap to file
    head, never to a negative or mid-line offset.
15. **L3 (`conductor_events.jsonl`) PII-impossible by construction.**
    `ConductorEventLog.emit` raises on unknown `template_id` or on a
    field that fails its declared shape. A test enumerates the
    registry and asserts no template accepts a string field without a
    bounded-shape regex.

## 7. Test plan

### 7.1 Unit — `iter_live_events`

- Replay window only (no live phase).
- Live tail only (no replay; skip_replay=False but file empty).
- Replay + live phase with deterministic timestamps.
- Malformed lines: skipped, never crash the stream.
- Partial line at EOF: buffered until newline arrives, then yielded.
- Missing source file: tolerated (waits, then emits when file appears).
- Stop event: iterator exits within 1 s of `stop.set()`.
- 16 MiB replay cap hit: emits `replay_truncated` once and continues.
- Heartbeat emission timing (no events): every 15 s, ±1 s.
- Rotation (path-level, mid-stream): `rotated_live` fires; new file's
  identity recorded; no infinite-rotation loop.
- Truncation (fd-level, mid-stream): `truncated_live` fires.

### 7.2 Unit — `JsonlTail` and source classes

- `read_new_lines(max_lines, max_bytes)` returns at most the bound,
  fd at byte after last yielded `\n`.
- Line-too-long: `line_too_long` gap; recovery discards via 64 KiB
  chunks; peak RSS bounded.
- `fstat_identity()` is stable across appends.
- `stat_path_identity()` returns None when path is unlinked.

### 7.3 Unit — `project_for_browser`

- Parameterised over EVERY `AuditEventType` enum value AND every
  conductor `template_id`. Asserts:
  (a) defined allowlist OR fail-closed default fires;
  (b) no field outside the allowlist appears in projected dict;
  (c) bounded-enum scrubber replaces malformed values with
  `<typeerror>`.

### 7.4 PII regression tests

- Synthesise audit events with `subject_protected_phrases=["X
  Surname"]`, `example_tokens=["x@y.com"]`,
  `input_artefacts=[{"path":"/Volumes/acme/case/foo.eml"}]`. Assert
  the SSE response body for the corresponding frame NEVER contains
  any of those literal strings. Repeat for L2 and L3 sources with
  analogous payloads.
- Traceback-PII: monkeypatch `project_for_browser` to raise with the
  raw `event` in `__traceback__`; assert the server-side log sink
  does NOT contain the raw payload literal (verifies
  `exc_info=False` is honoured).
- L3 template enforcement: `ConductorEventLog.emit("UNREGISTERED",
  ...)` raises; `emit(template_id, field=<value-violating-shape>)`
  raises.

### 7.5 Resume / rotation / truncation

- Composite-cursor round-trip: open SSE, consume 5 events across all
  three sources, capture the last `id`; reconnect with
  `Last-Event-ID: <captured>`, assert resumes from the exact next
  event on each source with NO duplicates and NO gaps.
- Resume-skips-replay: assert Phase A 30 s replay does NOT fire when
  cursor is valid.
- Resume backlog cap: write 20 MiB to a source, reconnect with cursor
  18 MiB behind; assert `resume_window_exceeded` gap and seek to
  `EOF - 16 MiB` snapped.
- Source-vanished resume: include an L2 source in cursor that no
  longer exists; assert one `source_vanished` gap and handler keeps
  serving other sources.
- Identity validate before seek: capture cursor, delete + recreate
  source file at same path, reconnect; assert one `rotated` gap and
  Phase B tracks the new file's identity without spuriously emitting
  `rotated_live`.
- `snap_offset_to_newline` floor: 17 MiB source with no `\n` in last
  16 MiB; reconnect with 20 MiB-behind cursor; assert resume snaps to
  0 and `resume_window_exceeded` fires.
- Malformed `Last-Event-ID` → 200 + Phase A replay (NOT 500).

### 7.6 Backpressure / hung-client

- Hung-write timeout: client opens but never reads; handler exits
  within `30 s + ε`; worker thread reclaimed.
- Projection-exception robustness: monkeypatch projection to raise on
  one event; assert SSE emits `{kind:"error"}` for that event and
  CONTINUES with subsequent events.
- Heartbeat under sustained burst: 10 k events/s burst for 30 s;
  assert heartbeat frames arrive at ≤ 15 s intervals throughout.
- Burst-limited read: append 50 k lines to one source in one batch;
  assert each iteration of the multiplexer loop yields at most 5 k
  lines for that source.

### 7.7 Conductor change

- `ConductorEventLog` idempotency: invoking the wiring twice on the
  same `case_dir` yields exactly one open file path.
- Template enforcement (above).
- Existing pipeline behaviour: a representative pipeline run still
  writes the same `audit_events.jsonl` content; the new
  `conductor_events.jsonl` is additive.

### 7.8 FD leak

- Open + close 100 SSE clients in series; assert
  `len(psutil.Process().open_files())` is constant.

### 7.9 Integration

- Spin up `ThreadingHTTPServer`, write events to a tmpdir, open
  `httpx` SSE client to `/live-log/stream`, assert events arrive in
  correct projection. Close client, assert handler thread exits
  within 1 s.

## 8. Open questions (deferred)

These were raised across jury rounds but deferred to implementation
discipline and tests rather than additional design iteration:

- **Latency floor / repeated `rotated_live` spam under logrotate
  misconfiguration** (Qwen R7). Current design will emit one gap
  per rotation observed; if a misconfigured rotator churns the file
  many times per minute, the client sees many gap badges. Spec
  accepts this for v1; if it becomes a real problem, a future
  follow-up could add coalescing.
- **Per-source latency tail for a single burst** (Qwen R7).
  Documented worst case: 5000 lines × 100 Hz emit = ≤ 50 s. Not
  considered blocking; documented in the spec.
- **TCP_USER_TIMEOUT / SO_SNDBUF tuning** (Qwen R6, Kimi R5). Default
  kernel KEEPALIVE intervals are accepted. SO_SNDTIMEO 30 s is the
  primary defence.

## 9. Out-of-scope (deliberately deferred to follow-up PRs)

- WebSocket / bidirectional control (operator-driven filter, rewind).
- Multi-case aggregation page.
- Server-side metrics endpoint for handler runtime / gap frequency.
- Coalescing rapid repeat gap markers.

## 10. References

- `src/dsar_orchestrator/operator_console.py` — existing console
  routes; `_RUNNING_STATE_FILE_HINTS` is the canonical map of L2
  sources per phase.
- `dsar_pipeline.audit.FileAuditStore` — writes `audit_events.jsonl`
  (the L1 source). Hash-chained, Article-30 / ROPA-bearing.
- `dsar_pipeline.audit.AuditEventType` — enum the `project_for_browser`
  allowlist parameterises over.
- `dsar_orchestrator.pipeline` — adds `ConductorEventLog` + emit calls
  at existing StageBanner sites.
