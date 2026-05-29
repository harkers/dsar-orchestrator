# Operator console live-log feed — design v2

| Version | Date | Notes |
|---|---|---|
| v2 | 2026-05-29 | **Integration-revised after codebase deep-read.** L3 source is no longer a new `working/conductor_events.jsonl` written by a new `ConductorEventLog` + template registry; instead the live-log feed tails the existing `~/.dsar-audit/<case_no>/pipeline.jsonl` that `dsar_orchestrator.audit.PipelineAuditor` + `StageBanner` already write. Eliminates a parallel writer (drops 4 implementation tasks). Projection allowlist gains explicit `event`-value rules for `stage_start` / `stage_end` / `stage_skipped` / `note` and fail-closes on anything else. `note(kind, message)` rows project to `{kind}` only — `message` is free text and is DROPPED at projection time. Constant rename: `_RUNNING_STATE_FILE_HINTS` was an in-spec typo; the actual operator_console constant is `STAGE_ARTEFACTS`. Test-fixture references updated: case_dir reaches the handler via module-level `_CFG: ServerConfig`, not a phantom global. |
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
  `operator_console.STAGE_ARTEFACTS` (a phase-name → list-of-filenames
  map) and live in the case's `working/` directory.
- The conductor emits structured rows via `PipelineAuditor` +
  `StageBanner` to `~/.dsar-audit/<case_no>/pipeline.jsonl` (NOT to the
  case's `working/` directory). It also prints a stderr banner per
  stage start/end; the file is the canonical source for L3.

## 3. Decisions (locked)

### 3.1 Sources (three, layered)

- **L1** `working/audit_events.jsonl` — primary. The Article-30
  canonical event log written by toolkit `FileAuditStore`. Hash-chained,
  rich payloads, projection-mandatory.
- **L2** per-stage jsonls — secondary, behind a verbose toggle (off by
  default). Filenames sourced from
  `operator_console.STAGE_ARTEFACTS` (a phase-name → list-of-filenames
  map). Examples: `redaction_decisions.jsonl`,
  `context_classifications.jsonl`, `qc_findings_07a.jsonl`.
- **L3** `~/.dsar-audit/<case_no>/pipeline.jsonl` — **existing file**.
  Written by `dsar_orchestrator.audit.PipelineAuditor` (one row per
  `StageBanner.__enter__/__exit__`, one row per
  `PipelineAuditor.note()`, one row per `mark_skipped`, one row per
  `finalise()` as a `RunReport`). Row shape examples:
  ```json
  {"event":"stage_start","stage":"redact","ts":"…",
   "schema_version":"…","producer_version":"…","case":"…"}
  {"event":"stage_end","stage":"redact","ts":"…","duration_s":15.4,
   "schema_version":"…","producer_version":"…","case":"…"}
  {"event":"stage_skipped","stage":"presidio_anonymize",
   "reason":"…","schema_version":"…","producer_version":"…","case":"…"}
  {"event":"note","kind":"…","message":"…","schema_version":"…",
   "producer_version":"…","case":"…"}
  ```
  Critically: `note().message` is **free text** — operator-readable
  rationale strings, NOT a bounded enum. This is the one L3 PII risk
  surface; see §3.2 for how the projection allowlist closes it.

  No new code is added on the L3 producer side. The live-log feed
  consumes the file that `PipelineAuditor` writes today.

L3 uses the **same `project_for_browser` allowlist machinery** as L1/L2.

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
- **L3 event-key dispatch.** L3 rows don't have an `event_type` field;
  they have an `event` field (`stage_start` / `stage_end` /
  `stage_skipped` / `note` / `RunReport` shape). The projection treats
  L3's `event` as the event_type for allowlist lookup. The four
  recognised values:
  - `stage_start` → project `{stage: enum_string, ts: iso8601}`.
  - `stage_end` → project `{stage: enum_string, ts: iso8601, duration_s: int_range[0, 86400]}`.
  - `stage_skipped` → project `{stage: enum_string, reason: enum_string{module_work_check, halted_upstream, manual_skip, unknown}}`.
  - `note` → project `{kind: enum_string}` — **`message` is DROPPED**
    because it's free text. Operator who needs the full note reads the
    pipeline.jsonl file directly via `/file`.

  Any L3 row with an unrecognised `event` value (e.g. a future
  `RunReport` row, or a yet-to-be-added event kind) flows through the
  fail-closed default and produces the
  `(unrecognised event type)` summary.

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
- `JsonlTail` — source class for ALL THREE source kinds (L1, L2, L3).
  Same JSONL append-only contract; same partial-line buffering; same
  identity/size tracking. No special-case `StructuredLogTail`.
- `open_sources(case_dir, resume, max_line_bytes, verbose_l2)` —
  opens L1 at `<case_dir>/working/audit_events.jsonl`; (if verbose)
  L2 file set at `<case_dir>/working/*.jsonl` per `STAGE_ARTEFACTS`;
  L3 at `Path.home() / ".dsar-audit" / <case_dir.name> /
  "pipeline.jsonl"`. Returns a dict keyed by source name; tolerates
  missing files (waits for them to appear).
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

### 4.4 Conductor change (none — L3 reuses existing file)

**v1 proposed a new `ConductorEventLog` writing
`working/conductor_events.jsonl`. v2 drops this entirely.** The L3
source is the file `dsar_orchestrator.audit.PipelineAuditor` already
writes to `~/.dsar-audit/<case_no>/pipeline.jsonl` on every stage
banner / note / skip / finalise. No new producer code, no template
registry, no wire-in to pipeline.py.

The live-log helper module reads `case_no` from the
operator_console's `_CFG.case_dir` (the case_dir is shaped like
`<root>/<case_no>/`; `case_no = case_dir.name`) and tails
`Path.home() / ".dsar-audit" / case_no / "pipeline.jsonl"` as L3.

PII discipline for L3 is enforced ENTIRELY by the projection
allowlist (§3.2). The `message` field on `note` rows is the one free-
text surface; the allowlist explicitly drops it.

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
15. **L3 (`pipeline.jsonl`) PII-safe via projection allowlist.**
    Unlike v1's "PII-impossible by construction" stance,
    `PipelineAuditor.note(kind, message)` accepts a free-text
    `message`. The projection MUST drop `message` at the projection
    boundary — the L3 allowlist for `event="note"` lists only `kind`.
    Tests assert that a synthetic `note` row containing a PII canary
    in `message` does not appear in the SSE response.

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
  recognised L3 `event` value (`stage_start`, `stage_end`,
  `stage_skipped`, `note`). Asserts:
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
- **L3 `note().message` drop**: synthesise a pipeline.jsonl `note`
  row with `message="leak-canary-<PII>"`. Assert the SSE response
  body NEVER contains the canary string — the projection MUST drop
  `message` and surface only the `kind` enum.
- Traceback-PII: monkeypatch `project_for_browser` to raise with the
  raw `event` in `__traceback__`; assert the server-side log sink
  does NOT contain the raw payload literal (verifies
  `exc_info=False` is honoured).

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

### 7.7 L3 source path resolution

- Given a `case_dir` of `/tmp/cases/CASE-123`, the helper opens L3 at
  `Path.home() / ".dsar-audit" / "CASE-123" / "pipeline.jsonl"` (NOT
  under `case_dir`). Test asserts the correct path is opened.
- Missing L3 file is tolerated (PipelineAuditor hasn't yet been
  invoked): handler does NOT crash; emits only L1 + L2 frames until
  L3 appears.

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
  routes; `STAGE_ARTEFACTS` (`dict[phase, list[filename]]`) is the
  canonical map of L2 sources per phase. `_CFG: ServerConfig` holds
  the case_dir; handlers access it via `self._ctx()` →
  `CaseContext(case_dir=_CFG.case_dir)`.
- `dsar_pipeline.audit.FileAuditStore` — writes
  `<case_dir>/working/audit_events.jsonl` (the L1 source). Hash-chained,
  Article-30 / ROPA-bearing.
- `dsar_pipeline.audit.AuditEventType` — enum the `project_for_browser`
  allowlist parameterises over.
- `dsar_orchestrator.audit.PipelineAuditor` + `StageBanner`
  (`@contextmanager`) — already write
  `~/.dsar-audit/<case_no>/pipeline.jsonl` with `{event, stage, ts,
  schema_version, producer_version, case}` rows. This file IS L3 — no
  new producer code needed in `pipeline.py`.
