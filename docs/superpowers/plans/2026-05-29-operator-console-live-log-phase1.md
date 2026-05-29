# Operator Console Live-Log Feed — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Plex-style live-event view in the operator console: tail `working/audit_events.jsonl`, per-stage decision/finding jsonls (verbose toggle), and the existing `~/.dsar-audit/<case_no>/pipeline.jsonl` written by `PipelineAuditor` + `StageBanner`. Stream to the browser over SSE with per-event-type field-allowlist projection so no raw PII reaches the browser.

**Architecture:** Single-thread merged iterator in the SSE handler (no per-source queue, no fan-in race). Composite `Last-Event-ID` cursor across all sources with identity-validate-before-seek and 16 MiB resume backlog cap. Browser is vanilla JS, no framework. **L3 source is the file `PipelineAuditor` already writes — no new producer code, no template registry.** Free-text PII risk in `note().message` is closed by the projection allowlist dropping `message` at the projection boundary.

**Tech Stack:** stdlib only (`http.server.ThreadingHTTPServer`, `json`, `logging`, `socket`, `threading`); pytest for tests.

**Spec:** `docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md`

**House rules:**
- Run from the worktree at `.claude/worktrees/operator-console-live-log/`.
- Use the pre-built venv: `.venv/bin/python -m pytest …`. `uv` resolve fails because `dsar-pipeline` is a sibling-repo install.
- Per `~/.claude/CLAUDE.md` code-review-jury amendment: before each commit, stage the task's files and run `~/.claude/scripts/code-review-jury.py --staged --task "Task N: <desc>" --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md`. Ship when ≥3/5 approve; otherwise apply convergent fixes and re-stage.
- Every task ends with a commit. Commits chain naturally into a single PR.

---

## Block B — Projection (PII allowlist + bounded-enum scrubber)

L3 is the existing `pipeline.jsonl`; the allowlist must dispatch on the `event`
field for L3 rows (`stage_start` / `stage_end` / `stage_skipped` / `note`).
`note().message` is free text and MUST be dropped at the projection boundary.

### Task B1: `_ALLOWLIST` table + bounded-enum value scrubber

**Spec coverage:** §3.2 (per-event-type field allowlist, fail-closed default, bounded-enum scrubber, L3 `event`-value dispatch with `note().message` drop), §6 invariants 6+15.
**Test coverage:** §7.3, §7.4 (PII regression including L3 `note().message` drop).

**Files:**
- Create: `src/dsar_orchestrator/local_broker/live_log_projection.py`
- Create: `tests/test_live_log_projection.py`

- [ ] **Step 1: Write the failing tests**

```python
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
    out = project_for_browser(
        _event("audit", "TOTALLY_UNKNOWN_EVENT", {"secret": "leaked-name"})
    )
    assert out["kind"] == "event"
    assert out["summary"] == "(unrecognised event type)"
    for v in out.values():
        if isinstance(v, str):
            assert "leaked-name" not in v


def test_known_l1_event_projects_only_allowlisted_fields() -> None:
    out = project_for_browser(_event(
        "audit", "REDACT_COMPLETED",
        {
            "refs_processed": 412,
            "redactions_applied": 89,
            # PII-bearing fields that must NOT survive projection.
            "subject_protected_phrases": ["Jane Smith"],
            "example_tokens": ["jane@example.com"],
        },
    ))
    serialised = str(out)
    assert "Jane Smith" not in serialised
    assert "subject_protected_phrases" not in serialised
    assert "example_tokens" not in serialised
    assert "jane@example.com" not in serialised


def test_l3_stage_start_projects_stage_and_ts() -> None:
    """L3 row dispatches on `event` field, not `event_type`."""
    out = project_for_browser({
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
    })
    assert "stage started" in out["summary"]
    assert "redact" in out["summary"]


def test_l3_note_drops_message_field() -> None:
    """§3.2: `note().message` is free text → DROPPED by projection.
    Critical PII control — operator-readable rationale strings must
    not surface in the live feed."""
    out = project_for_browser({
        "kind": "event",
        "source": "cond",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "note",
        "payload": {
            "event": "note",
            "kind": "info",
            "message": "Jane Smith is the data subject, DoB 1985-03-14",
        },
    })
    serialised = str(out)
    assert "Jane Smith" not in serialised
    assert "1985-03-14" not in serialised
    assert "message" not in out
    assert out["summary"] == "note (info)"


def test_l3_unknown_event_value_fails_closed() -> None:
    """A future `RunReport` row or any unrecognised event value flows
    through the fail-closed default."""
    out = project_for_browser({
        "kind": "event",
        "source": "cond",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "RunReport",
        "payload": {
            "event": "RunReport",
            "halt_reason": "Jane Smith vulnerable subject — paused",
        },
    })
    assert "Jane Smith" not in str(out)
    assert out["summary"] == "(unrecognised event type)"


def test_scrub_value_replaces_malformed_enum_value() -> None:
    assert scrub_value("severity", "info", "enum_string",
                       {"info", "warn", "error", "debug"}) == "info"
    assert scrub_value("severity", "<script>", "enum_string",
                       {"info", "warn"}) == "<typeerror>"


def test_scrub_value_replaces_out_of_range_int() -> None:
    assert scrub_value("refs_processed", 412, "int_range",
                       (0, 10_000_000)) == 412
    assert scrub_value("refs_processed", -1, "int_range",
                       (0, 10_000_000)) == "<typeerror>"


def test_scrub_value_passes_iso8601() -> None:
    assert scrub_value("ts", "2026-05-29T10:42:11Z", "iso8601", None) == "2026-05-29T10:42:11Z"
    assert scrub_value("ts", "not a ts", "iso8601", None) == "<typeerror>"


def test_summary_for_uses_only_projected_fields() -> None:
    projected = {"refs_processed": 412, "redactions_applied": 89}
    s = summary_for("REDACT_COMPLETED", projected)
    assert "412" in s
    assert "89" in s


def test_allowlist_table_has_no_freetext_string_shapes() -> None:
    """No allowlisted field is a free-text string. Every shape is
    enum_string, int_range, or iso8601."""
    for event_type, fields in _ALLOWLIST.items():
        for fname, (kind, _arg) in fields.items():
            assert kind in {"enum_string", "int_range", "iso8601"}, (
                f"{event_type}.{fname} declared as {kind!r}; PII risk"
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_projection.py -v`
Expected: ImportError — module does not exist yet.

- [ ] **Step 3: Write the implementation**

```python
# src/dsar_orchestrator/local_broker/live_log_projection.py
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


_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
)
_SEVERITY_ENUM = {"info", "warn", "error", "debug"}

_KNOWN_STAGES = {
    "fitness_preflight", "subject_protection_preflight",
    "people_register_preflight", "extraction_quality_gate",
    "threat_model_verify",
    "ingest", "stage_2_parallel", "stage_3_parallel",
    "sig_block_discovery", "scope_classify", "pii_classify",
    "redact", "presidio_anonymize", "pii_jury_review",
    "verify_spec", "bake", "verify_pdf", "export",
}

_NOTE_KINDS = {"info", "warn", "error", "debug"}

_STAGE_SKIPPED_REASONS = {
    "module_work_check", "halted_upstream",
    "manual_skip", "preflight_failed", "unknown",
}


# Each value is `{field_name: (shape_kind, shape_arg)}`.
_ALLOWLIST: dict[str, dict[str, tuple[str, Any]]] = {
    # ---- L1 audit_events (subset; extend as new events stabilise) ----
    "REDACT_STARTED": {
        "stage": ("enum_string", {"redact"}),
    },
    "REDACT_COMPLETED": {
        "refs_processed": ("int_range", (0, 10_000_000)),
        "redactions_applied": ("int_range", (0, 10_000_000)),
    },
    "PEOPLE_REGISTER_BUILT": {
        "rows": ("int_range", (0, 10_000_000)),
        "clusters": ("int_range", (0, 1_000_000)),
    },
    "SIG_BLOCK_DISCOVERY_COMPLETED": {
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
        "duration_s": ("int_range", (0, 86_400)),
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
    ts = event.get("ts")
    payload = event.get("payload") or {}

    projected_fields = _project_payload(event_type, payload) if event_type else {}
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
        "event_type": event_type if event_type in _ALLOWLIST else None,
        "stage": projected_fields.get("stage"),
        "severity": severity,
        "summary": summary_for(event_type, projected_fields),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_projection.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_projection.py tests/test_live_log_projection.py
~/.claude/scripts/code-review-jury.py --staged --task "Task B1: live-log projection + L3 event dispatch" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): allowlist + bounded-enum scrubber + L3 event dispatch"
```

---

### Task B2: PII regression test suite

**Spec coverage:** §3.2 (covers the L3 free-text `message` drop).
**Test coverage:** §7.4.

**Files:**
- Create: `tests/test_live_log_pii_regression.py`

- [ ] **Step 1: Write the tests**

```python
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
    out = project_for_browser({
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
    })
    serialised = json.dumps(out, ensure_ascii=False)
    assert canary not in serialised, (
        f"PII canary {canary!r} leaked: {serialised}"
    )


@pytest.mark.parametrize("canary", _CANARIES)
def test_pii_canaries_never_appear_for_unknown_event_type(canary: str) -> None:
    out = project_for_browser({
        "kind": "event",
        "source": "audit",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "FUTURE_EVENT_NOT_ALLOWLISTED",
        "payload": {
            "subject_protected_phrases": [canary],
            "name": canary,
            "everything": canary,
        },
    })
    assert canary not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("canary", _CANARIES)
def test_l3_note_message_field_is_dropped(canary: str) -> None:
    """The critical L3 PII regression: `note().message` is free text
    and MUST be dropped by the projection. Operator-readable rationale
    strings written by PipelineAuditor.note() never reach the browser."""
    out = project_for_browser({
        "kind": "event",
        "source": "cond",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "note",
        "payload": {
            "event": "note",
            "kind": "info",
            "message": f"context says: {canary}",
        },
    })
    serialised = json.dumps(out, ensure_ascii=False)
    assert canary not in serialised, (
        f"L3 note().message leaked canary {canary!r}: {serialised}"
    )
    assert "message" not in out


@pytest.mark.parametrize("canary", _CANARIES)
def test_l3_unknown_event_value_fails_closed(canary: str) -> None:
    out = project_for_browser({
        "kind": "event",
        "source": "cond",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "RunReport",
        "payload": {
            "event": "RunReport",
            "halt_reason": f"halted on subject {canary}",
        },
    })
    assert canary not in json.dumps(out, ensure_ascii=False)


def test_severity_field_does_not_leak_freeform_text() -> None:
    out = project_for_browser({
        "kind": "event", "source": "audit",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "REDACT_COMPLETED",
        "payload": {"severity": "leak-canary-9876"},
    })
    assert "leak-canary-9876" not in json.dumps(out)
    assert out["severity"] == "<typeerror>"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_pii_regression.py -v`
Expected: 29 passed (7 canaries × 4 paths + 1 severity).

- [ ] **Step 3: Commit**

```bash
git add tests/test_live_log_pii_regression.py
~/.claude/scripts/code-review-jury.py --staged --task "Task B2: PII regression suite (L1 + L3 note drop)" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "test(live-log): PII canary regression suite incl. L3 note().message drop"
```

---

## Block C — `JsonlTail` source class

### Task C1: `JsonlTail.__init__` + identity-tuple methods

**Spec coverage:** §6 invariant 7 (fd-vs-path identity split).
**Test coverage:** §7.2.

**Files:**
- Create: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Create: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_live_log_stream.py
"""Unit tests for live_log_stream. Spec §4.3, §6."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from dsar_orchestrator.local_broker.live_log_stream import JsonlTail


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: ImportError.

- [ ] **Step 3: Write the implementation**

```python
# src/dsar_orchestrator/local_broker/live_log_stream.py
"""Live-log streaming primitives for the operator console.

Powers `/live-log/stream` SSE: tails one or more JSONL source files,
merges them through a single iterator, yields `LiveEvent`s for the
SSE handler to project.

Single-thread design: one SSE worker thread owns the iterator,
which owns all source file handles in a try/finally.

Spec: 2026-05-29 operator-console-live-log design v2 §4.3, §6.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO


@dataclass
class JsonlTail:
    path: Path
    max_line_bytes: int
    _fh: IO[str] | None = field(default=None, init=False, repr=False)
    _buffer: str = field(default="", init=False, repr=False)
    identity_tuple: tuple[int, int] | None = field(default=None, init=False)
    last_known_size: int = field(default=0, init=False)
    disabled: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._open()

    def _open(self) -> None:
        if self._fh is not None:
            return
        try:
            self._fh = self.path.open("r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            self._fh = None

    @property
    def name(self) -> str:
        return self.path.name

    def fstat_identity(self) -> tuple[int, int] | None:
        if self._fh is None:
            return None
        st = os.fstat(self._fh.fileno())
        return (st.st_dev, st.st_ino)

    def stat_path_identity(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (st.st_dev, st.st_ino)

    def fstat_size(self) -> int:
        if self._fh is None:
            return 0
        return os.fstat(self._fh.fileno()).st_size

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task C1: JsonlTail with fd-vs-path identity" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): JsonlTail with fd-vs-path identity split"
```

---

### Task C2: `JsonlTail.read_new_lines(max_lines, max_bytes)`

**Spec coverage:** §6 invariants 1, 9 (snap-to-`\n`, bounded-batch contract).
**Test coverage:** §7.2.

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 5 new tests fail.

- [ ] **Step 3: Add to `JsonlTail`**

```python
    def read_new_lines(self, *, max_lines: int, max_bytes: int):
        """Yield up to (max_lines, max_bytes) JSON objects from the
        tail. fd left at byte after last complete `\\n` yielded;
        partial trailing line buffered.

        Stateless bounded-batch contract (§6.9): caller MUST NOT
        `break` mid-iteration — that would discard buffered lines.
        """
        if self._fh is None or self.disabled:
            self._open()
            if self._fh is None:
                return
        lines_yielded = 0
        bytes_yielded = 0
        while lines_yielded < max_lines and bytes_yielded < max_bytes:
            chunk = self._fh.read(65_536)
            if not chunk:
                break
            self._buffer += chunk
            while "\n" in self._buffer and lines_yielded < max_lines and bytes_yielded < max_bytes:
                line, self._buffer = self._buffer.split("\n", 1)
                if len(line) > self.max_line_bytes:
                    yield {"_kind": "line_too_long"}
                    lines_yielded += 1
                    bytes_yielded += len(line)
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    yield json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                lines_yielded += 1
                bytes_yielded += len(stripped) + 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task C2: read_new_lines bounded batch" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): JsonlTail.read_new_lines bounded batch"
```

---

## Block D — `iter_live_events` merging iterator

### Task D1: `LiveEvent`, `ResumeCursor`, `parse_composite_last_event_id`

**Spec coverage:** §3.5, §6 invariant 13.
**Test coverage:** §7.5.

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
from dsar_orchestrator.local_broker.live_log_stream import (
    INITIAL_ZERO_CURSOR,
    LiveEvent,
    ResumeCursor,
    _PerSourceCursor,
    parse_composite_last_event_id,
)


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
        kind="event", source="audit", ts="2026-05-29T10:42:11Z",
        event_type="REDACT_COMPLETED",
        payload={"refs_processed": 1},
        composite_cursor="audit:1024:1:123",
    )
    assert e.composite_cursor == "audit:1024:1:123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 6 new tests fail.

- [ ] **Step 3: Add to `src/dsar_orchestrator/local_broker/live_log_stream.py`**

```python
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
            offset, dev, ino = int(parts[-3]), int(parts[-2]), int(parts[-1])
            if offset < 0 or offset > 2**63 or dev > 2**63 or ino > 2**63:
                return None
            per_source[name] = _PerSourceCursor(
                offset=offset, identity_tuple=(dev, ino),
            )
        cursor = ResumeCursor(per_source=per_source)
        if not cursor.is_valid():
            return None
        return cursor
    except (ValueError, OverflowError, AttributeError):
        return None


@dataclass
class LiveEvent:
    kind: str                            # "event" | "gap" | "heartbeat"
    source: str
    ts: str | None = None
    event_type: str | None = None
    payload: dict | None = None
    reason: str | None = None
    composite_cursor: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D1: ResumeCursor + exception-safe parse" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): composite cursor + exception-safe parse"
```

---

### Task D2: `open_sources` + Phase A replay (with correct L3 path)

**Spec coverage:** §3.1 (L3 at `~/.dsar-audit/<case_no>/pipeline.jsonl`), §3.3 (30 s replay, 16 MiB cap), §4.3 (`open_sources` with `STAGE_ARTEFACTS`).
**Test coverage:** §7.1 (replay window, cap), §7.7 (L3 source path resolution).

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
from datetime import UTC, datetime, timedelta

from dsar_orchestrator.local_broker.live_log_stream import (
    iter_live_events,
    open_sources,
    _l3_pipeline_jsonl_path,
)


def _ts(seconds_ago: float) -> str:
    return (
        datetime.now(UTC) - timedelta(seconds=seconds_ago)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.05,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
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
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=2_000,
            heartbeat_s=15, poll_interval=0.05,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "gap" and s.reason == "replay_truncated" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=3.0)
    assert any(e.kind == "gap" and e.reason == "replay_truncated" for e in seen)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 5 new tests fail (`open_sources`, `iter_live_events`, `_l3_pipeline_jsonl_path` not defined).

- [ ] **Step 3: Add source-opener + replay phase to `live_log_stream.py`**

```python
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
            if fn not in seen and fn.endswith(".jsonl"):
                seen.add(fn)
                out.append(fn)
    return out


def open_sources(
    case_dir: Path, *, max_line_bytes: int, verbose_l2: bool,
) -> dict[str, JsonlTail]:
    """Open L1 (always) + L3 (always) + L2 (if verbose). Returns a
    dict keyed by source name: 'audit', 'cond', or 'stage:<filename>'.
    """
    working = Path(case_dir) / "working"
    sources: dict[str, JsonlTail] = {
        "audit": JsonlTail(
            working / _AUDIT_FILENAME, max_line_bytes=max_line_bytes,
        ),
        "cond": JsonlTail(
            _l3_pipeline_jsonl_path(case_dir),
            max_line_bytes=max_line_bytes,
        ),
    }
    if verbose_l2:
        for fn in _l2_stage_artefact_filenames():
            sources[f"stage:{fn}"] = JsonlTail(
                working / fn, max_line_bytes=max_line_bytes,
            )
    return sources


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

    cutoff_iso = (
        (datetime.now(UTC) - timedelta(seconds=replay_window_s))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    per_source_events: dict[str, list[tuple[str, dict, str]]] = {}

    for name, src in sources.items():
        size = src.fstat_size()
        if size == 0 or src._fh is None:
            per_source_events[name] = []
            continue
        scan_start = max(0, size - replay_byte_cap)
        if scan_start > 0:
            yield LiveEvent(kind="gap", source=name, reason="replay_truncated")
        src._fh.seek(scan_start)
        chunk = src._fh.read(size - scan_start)
        if scan_start > 0:
            # Re-sync to first \n: discard any partial line at start.
            nl = chunk.find("\n")
            if nl >= 0:
                chunk = chunk[nl + 1:]
        events: list[tuple[str, dict, str]] = []
        for line in chunk.split("\n"):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = payload.get("ts") or payload.get("timestamp")
            if not isinstance(ts, str):
                continue
            if ts >= cutoff_iso:
                events.append((ts, payload, name))
        per_source_events[name] = events
        # Leave fd at EOF for Phase B.
        src._fh.seek(0, 2)

    for ts, payload, name in heapq.merge(
        *[iter(events) for events in per_source_events.values()],
        key=lambda t: t[0],
    ):
        # L3 dispatch: L3 rows carry `event`, L1/L2 carry `event_type`.
        event_type = payload.get("event_type") or payload.get("event") or payload.get("template_id")
        size = sources[name].fstat_size()
        dev, ino = sources[name].fstat_identity() or (0, 0)
        yield LiveEvent(
            kind="event", source=name, ts=ts,
            event_type=event_type, payload=payload,
            composite_cursor=f"{name}:{size}:{dev}:{ino}",
        )


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
    """See module docstring + spec §4.3. Phase B is added in Task D3."""
    sources = open_sources(
        Path(case_dir), max_line_bytes=max_line_bytes, verbose_l2=verbose_l2,
    )
    try:
        if not skip_replay:
            yield from _phase_a_replay(
                sources,
                replay_window_s=replay_window_s,
                replay_byte_cap=replay_byte_cap,
            )
        # Phase B added in D3.
    finally:
        for src in sources.values():
            src.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D2: open_sources + Phase A replay" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): open_sources (L1+L3 always, L2 verbose) + Phase A replay"
```

---

### Task D3: Phase B live-tail loop with rotation/truncation/burst/heartbeat

**Spec coverage:** §3.4 (heartbeat scheduler), §3.5 (rotation/truncation), §6 invariants 8, 9, 10, 11, 12.
**Test coverage:** §7.1.

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
def test_live_tail_yields_appended_events(tmp_path: Path) -> None:
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
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
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
            case_dir, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=0.1, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 4 new tests fail.

- [ ] **Step 3: Extend `iter_live_events` with Phase B**

Replace the `# Phase B added in D3.` placeholder with:

```python
        # Initialise identity baselines for the live-tail loop.
        for src in sources.values():
            src.identity_tuple = src.fstat_identity()
            src.last_known_size = src.fstat_size()

        import time as _time
        last_heartbeat_ts = _time.monotonic()
        while not stop.is_set():
            for name, src in sources.items():
                if src.disabled:
                    continue
                path_id = src.stat_path_identity()
                if path_id is None:
                    yield LiveEvent(
                        kind="gap", source=name,
                        reason="source_vanished_live",
                    )
                    src.disabled = True
                    continue
                if src.identity_tuple is not None and path_id != src.identity_tuple:
                    yield LiveEvent(
                        kind="gap", source=name, reason="rotated_live",
                    )
                    src.close()
                    src._open()
                    src.identity_tuple = src.fstat_identity()
                    src.last_known_size = 0
                elif src.identity_tuple is None:
                    # First time the file actually exists.
                    src.identity_tuple = path_id
                fd_size = src.fstat_size()
                if fd_size < src.last_known_size:
                    yield LiveEvent(
                        kind="gap", source=name, reason="truncated_live",
                    )
                    if src._fh is not None:
                        src._fh.seek(0)
                src.last_known_size = fd_size

                line_count = 0
                for evt in src.read_new_lines(
                    max_lines=max_lines_per_burst,
                    max_bytes=max_bytes_per_burst,
                ):
                    if evt.get("_kind") == "line_too_long":
                        yield LiveEvent(
                            kind="gap", source=name, reason="line_too_long",
                        )
                        continue
                    ts = evt.get("ts") or evt.get("timestamp")
                    event_type = evt.get("event_type") or evt.get("event") or evt.get("template_id")
                    offset = src.fstat_size() if src._fh is not None else 0
                    dev, ino = src.identity_tuple or (0, 0)
                    yield LiveEvent(
                        kind="event", source=name, ts=ts,
                        event_type=event_type, payload=evt,
                        composite_cursor=f"{name}:{offset}:{dev}:{ino}",
                    )
                    line_count += 1
                    if line_count % 100 == 0:
                        now = _time.monotonic()
                        if now - last_heartbeat_ts >= heartbeat_s:
                            yield LiveEvent(kind="heartbeat", source=name)
                            last_heartbeat_ts = now
            now = _time.monotonic()
            if now - last_heartbeat_ts >= heartbeat_s:
                yield LiveEvent(kind="heartbeat", source="*")
                last_heartbeat_ts = now
            if stop.wait(poll_interval):
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D3: Phase B live-tail loop" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): Phase B live-tail with rotation/truncation/heartbeat"
```

---

### Task D4: `_phase_a_skip_to_cursor` (resume via Last-Event-ID)

**Spec coverage:** §3.5, §6.8.
**Test coverage:** §7.5.

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
def test_resume_skips_replay_when_cursor_valid(tmp_path: Path) -> None:
    case_dir = tmp_path / "CASE-1"
    (case_dir / "working").mkdir(parents=True)
    audit = case_dir / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"ts": _ts(0.1 * i), "event_type": f"E{i}"}) + "\n")
    size = audit.stat().st_size
    st = audit.stat()
    cursor = ResumeCursor(per_source={
        "audit": _PerSourceCursor(offset=size, identity_tuple=(st.st_dev, st.st_ino)),
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
            stop=stop, verbose_l2=False,
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
    cursor = ResumeCursor(per_source={
        "audit": _PerSourceCursor(offset=audit.stat().st_size,
                                  identity_tuple=(st.st_dev, st.st_ino)),
    })
    audit.unlink()
    with audit.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "NEW"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            case_dir, resume=cursor, skip_replay=True,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            seen.append(e)
            if any(s.kind == "gap" and s.reason == "rotated" for s in seen):
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=2.0)
    assert any(e.kind == "gap" and e.reason == "rotated" for e in seen)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 2 new tests fail.

- [ ] **Step 3: Add `_phase_a_skip_to_cursor` and wire into `iter_live_events`**

Modify the Phase A dispatch at the top of `iter_live_events`:

```python
        if skip_replay and resume is not None and resume.is_valid():
            yield from _phase_a_skip_to_cursor(
                sources, resume=resume, replay_byte_cap=replay_byte_cap,
            )
        elif not skip_replay:
            yield from _phase_a_replay(
                sources,
                replay_window_s=replay_window_s,
                replay_byte_cap=replay_byte_cap,
            )
        # Otherwise (skip_replay but no valid cursor): fall through
        # to Phase B from current EOF.
```

Add `_phase_a_skip_to_cursor`:

```python
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
            yield LiveEvent(kind="gap", source=name, reason="rotated")
            if src._fh is not None:
                src._fh.seek(0)
            continue
        size = src.fstat_size()
        if cursor_state.offset > size:
            yield LiveEvent(kind="gap", source=name, reason="truncated")
            if src._fh is not None:
                src._fh.seek(0)
            continue
        if size - cursor_state.offset > replay_byte_cap:
            yield LiveEvent(
                kind="gap", source=name, reason="resume_window_exceeded",
            )
            target = max(0, size - replay_byte_cap)
            if src._fh is not None:
                src._fh.seek(target)
                tail = src._fh.read(min(replay_byte_cap, size - target))
                nl = tail.find("\n")
                if nl >= 0:
                    src._fh.seek(target + nl + 1)
                else:
                    src._fh.seek(0)
            continue
        if src._fh is not None:
            src._fh.seek(cursor_state.offset)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 27 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D4: _phase_a_skip_to_cursor resume" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): resume via Last-Event-ID with identity validate"
```

---

## Block E — SSE handler + console routes

### Task E1: SSE helpers (`send_sse_headers`, `write_sse_frame`)

**Spec coverage:** §4.2, §4.5.
**Test coverage:** §7.9.

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
import io

from dsar_orchestrator.local_broker.live_log_stream import (
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 2 new tests fail.

- [ ] **Step 3: Add to `live_log_stream.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 29 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task E1: SSE write_sse_frame helper" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(live-log): SSE write_sse_frame helper"
```

---

### Task E2: `/live-log/stream` SSE handler in operator_console

**Spec coverage:** §4.1, §4.2.
**Test coverage:** §7.6, §7.9.

**Files:**
- Modify: `src/dsar_orchestrator/operator_console.py`
- Create: `tests/test_live_log_route.py`

**Integration notes from the codebase audit:**
- `case_dir` reaches the handler via module-level `_CFG: ServerConfig`. Tests MUST monkeypatch `_CFG`, not a phantom global.
- `ConsoleHandler._send(code, body, ctype)` is the one-shot response helper; SSE uses raw `self.send_response(...)`/`self.send_header(...)`/`self.end_headers()` because it's long-lived.
- Route dispatch is a top-down `if url.path == "X":` chain in `do_GET`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_live_log_route.py
"""Integration-ish tests for the /live-log/stream SSE route.
Spawns ConsoleHandler in-process, connects with stdlib http.client,
asserts SSE frames.
"""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture()
def case_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "CASE-TEST"
    (cd / "working").mkdir(parents=True)
    return cd


@pytest.fixture()
def server(case_dir: Path):
    # case_dir reaches the handler via module-level _CFG.
    import dsar_orchestrator.operator_console as oc

    saved = getattr(oc, "_CFG", None)
    oc._CFG = oc.ServerConfig(
        case_dir=case_dir,
        orchestrator_cli="dsar-conductor",
        approver_bin=None,
        approver_input=Path("/tmp/approver_input.json"),
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), oc.ConsoleHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()
    oc._CFG = saved


def test_live_log_stream_serves_text_event_stream(case_dir, server):
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    conn.close()


def test_live_log_stream_emits_frame_for_appended_event(case_dir, server):
    port = server
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()

    def appender() -> None:
        time.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-05-29T10:42:11Z",
                "event_type": "REDACT_COMPLETED",
                "refs_processed": 1,
                "redactions_applied": 1,
            }) + "\n")
    threading.Thread(target=appender, daemon=True).start()

    body = resp.read1(4096)
    conn.close()
    text = body.decode("utf-8", errors="replace")
    assert "event: live-log" in text
    assert "REDACT_COMPLETED" in text


def test_live_log_stream_malformed_last_event_id_falls_back_to_replay(case_dir, server):
    """Spec §6.13: a malformed Last-Event-ID MUST NOT 500. Server
    falls back to Phase A replay (returns 200)."""
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "GET", "/live-log/stream",
        headers={"Last-Event-ID": "garbage:not:numeric:here"},
    )
    resp = conn.getresponse()
    assert resp.status == 200
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: route does not exist.

- [ ] **Step 3: Add route + handler method to `operator_console.py`**

In the `do_GET` chain (search for existing `/people-register` route, add this nearby):

```python
        if url.path == "/live-log/stream":
            self._serve_live_log_stream()
            return
        if url.path == "/live-log":
            self._send(200, render_live_log_page(ctx))
            return
```

Add the `_serve_live_log_stream` method on `ConsoleHandler`:

```python
    def _serve_live_log_stream(self) -> None:
        """SSE handler — single-thread merged iterator. Spec §4.2."""
        import socket
        import threading as _threading
        from urllib.parse import parse_qs

        from .local_broker.live_log_projection import project_for_browser
        from .local_broker.live_log_stream import (
            iter_live_events,
            parse_composite_last_event_id,
            write_sse_frame,
        )

        ctx = self._ctx()
        query = parse_qs(urllib.parse.urlparse(self.path).query)
        verbose_l2 = (query.get("verbose", ["0"])[0] == "1")

        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.connection.settimeout(30.0)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return

        try:
            resume = parse_composite_last_event_id(
                self.headers.get("Last-Event-ID"),
            )
        except Exception:
            resume = None
        skip_replay = resume is not None and resume.is_valid()
        last_cursor: str | None = None

        stop = _threading.Event()
        try:
            for event in iter_live_events(
                ctx.case_dir,
                resume=resume,
                skip_replay=skip_replay,
                replay_window_s=30,
                replay_byte_cap=16 * 1024 * 1024,
                heartbeat_s=15,
                poll_interval=0.5,
                max_line_bytes=1_048_576,
                max_lines_per_burst=5000,
                max_bytes_per_burst=1_048_576,
                stop=stop,
                verbose_l2=verbose_l2,
            ):
                if event.kind != "heartbeat":
                    last_cursor = event.composite_cursor

                if event.kind == "heartbeat":
                    try:
                        self.wfile.write(b":hb\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    continue

                cursor_for_frame = (
                    last_cursor if event.kind == "heartbeat"
                    else event.composite_cursor
                )

                if event.kind == "gap":
                    projected = {
                        "kind": "gap",
                        "source": event.source,
                        "reason": event.reason,
                    }
                else:
                    try:
                        projected = project_for_browser({
                            "kind": "event",
                            "source": event.source,
                            "ts": event.ts,
                            "event_type": event.event_type,
                            "payload": event.payload or {},
                        })
                    except Exception as exc:
                        # Spec §6.6: PII-safe error log only, no exc_info.
                        sys.stderr.write(
                            f"[live-log] project_for_browser failed: "
                            f"source={event.source} "
                            f"event_type={event.event_type} "
                            f"error_type={type(exc).__name__}\n"
                        )
                        projected = {
                            "kind": "error",
                            "source": event.source,
                            "msg": "projection_failed",
                            "ts": event.ts,
                        }
                try:
                    write_sse_frame(self.wfile, id=cursor_for_frame, data=projected)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        finally:
            stop.set()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/operator_console.py tests/test_live_log_route.py
~/.claude/scripts/code-review-jury.py --staged --task "Task E2: /live-log/stream SSE handler" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(console): /live-log/stream SSE route"
```

---

### Task E3: `/live-log` HTML page + nav-bar link

**Spec coverage:** §4.1, §4.6.
**Test coverage:** smoke.

**Files:**
- Modify: `src/dsar_orchestrator/operator_console.py`
- Modify: `tests/test_live_log_route.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_route.py`:

```python
def test_live_log_page_serves_html(case_dir, server):
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    assert resp.status == 200
    assert "<title>Live log" in body
    assert "EventSource" in body
    assert "/live-log/stream" in body


def test_case_header_nav_links_to_live_log(case_dir, server):
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    # Either the nav strip on the landing page, or the page itself, links to /live-log.
    assert "/live-log" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: 2 new tests fail.

- [ ] **Step 3: Add `render_live_log_page` and nav-bar link**

In `src/dsar_orchestrator/operator_console.py`, add near other `render_*` functions:

```python
_LIVE_LOG_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Live log — operator console</title>
  <style>
    body { font: 13px/1.4 -apple-system, system-ui, sans-serif; margin: 0; padding: 0; background: #111; color: #ddd; }
    header { padding: 8px 12px; background: #222; border-bottom: 1px solid #333; display: flex; gap: 12px; align-items: center; }
    header h1 { font-size: 14px; margin: 0; }
    header label { color: #aaa; }
    main { padding: 8px 12px; }
    table { width: 100%; border-collapse: collapse; font-family: ui-monospace, monospace; font-size: 12px; }
    td { padding: 2px 6px; border-bottom: 1px solid #1c1c1c; vertical-align: top; }
    tr.gap td { color: #c8a25c; font-style: italic; }
    tr.error td { color: #d96a6a; }
    tr.warn td { color: #c8a25c; }
    .footer { padding: 6px 12px; color: #888; font-size: 12px; }
    button, input[type=checkbox]+label, input[type=text], select { font-size: 12px; background: #222; color: #ddd; border: 1px solid #333; padding: 2px 6px; }
    .gap-badge { background: #5c3b1c; color: #fff; padding: 2px 6px; border-radius: 3px; margin-left: 8px; }
  </style>
</head>
<body>
<header>
  <h1>Live log</h1>
  <button id="pause">Pause</button>
  <label><input type="checkbox" id="src-audit" checked> audit</label>
  <label><input type="checkbox" id="src-stage"> stage (verbose)</label>
  <label><input type="checkbox" id="src-cond" checked> conductor</label>
  <label>severity <select id="sev"><option value="">all</option><option>info</option><option>warn</option><option>error</option><option>debug</option></select></label>
  <label>filter <input id="filter" type="text" placeholder="substring"></label>
  <span id="gap-badge"></span>
  <span style="margin-left:auto"><a href="/" style="color:#8bb">&larr; back</a></span>
</header>
<main>
  <table id="log"><tbody></tbody></table>
</main>
<div class="footer" id="status">connecting&hellip;</div>
<script>
(() => {
  const tbody = document.querySelector("#log tbody");
  const status = document.getElementById("status");
  const btnPause = document.getElementById("pause");
  const fltSrcAudit = document.getElementById("src-audit");
  const fltSrcStage = document.getElementById("src-stage");
  const fltSrcCond = document.getElementById("src-cond");
  const fltSev = document.getElementById("sev");
  const fltText = document.getElementById("filter");
  const gapBadge = document.getElementById("gap-badge");
  let paused = false;
  let gapCount = 0;
  let autoscroll = true;
  let verbose = false;

  window.addEventListener("scroll", () => {
    autoscroll = (window.innerHeight + window.scrollY) >= document.body.offsetHeight - 50;
  });

  function rebuildEventSource() {
    if (window.__es) { window.__es.close(); }
    const url = "/live-log/stream" + (verbose ? "?verbose=1" : "");
    const es = new EventSource(url);
    window.__es = es;
    es.addEventListener("live-log", onFrame);
    es.onopen = () => { status.textContent = "live"; };
    es.onerror = () => { status.textContent = "reconnecting..."; };
  }

  function visible(frame) {
    if (frame.source === "audit" && !fltSrcAudit.checked) return false;
    if (frame.source && frame.source.startsWith("stage:") && !fltSrcStage.checked) return false;
    if (frame.source === "cond" && !fltSrcCond.checked) return false;
    if (fltSev.value && frame.severity && frame.severity !== fltSev.value) return false;
    const text = (frame.summary || frame.reason || "");
    if (fltText.value && !text.includes(fltText.value)) return false;
    return true;
  }

  function append(frame) {
    if (paused || !visible(frame)) return;
    const tr = document.createElement("tr");
    tr.classList.add(frame.kind === "gap" ? "gap"
                     : frame.severity === "error" ? "error"
                     : frame.severity === "warn" ? "warn" : "");
    const cells = [
      frame.ts || "",
      frame.source || "",
      frame.event_type || frame.kind || "",
      frame.summary || frame.reason || frame.msg || "",
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
    if (frame.kind === "gap") {
      gapCount++;
      gapBadge.textContent = "gaps: " + gapCount;
      gapBadge.className = "gap-badge";
    }
    if (autoscroll) {
      window.scrollTo({top: document.body.scrollHeight});
    }
  }

  function onFrame(ev) {
    let frame;
    try { frame = JSON.parse(ev.data); } catch (e) { return; }
    append(frame);
  }

  btnPause.addEventListener("click", () => {
    paused = !paused;
    btnPause.textContent = paused ? "Resume" : "Pause";
  });
  fltSrcStage.addEventListener("change", () => {
    verbose = fltSrcStage.checked;
    rebuildEventSource();
  });
  rebuildEventSource();
})();
</script>
</body>
</html>
"""


def render_live_log_page(ctx) -> str:  # noqa: ARG001
    return _LIVE_LOG_HTML
```

Add the nav-bar link in `_case_header`. The existing nav block (around line 790) is a single string with a list of `<a>` links — append `"<a href='/live-log'>Live log</a>"` to that string.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/operator_console.py tests/test_live_log_route.py
~/.claude/scripts/code-review-jury.py --staged --task "Task E3: /live-log HTML page + nav link" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md
git commit -m "feat(console): /live-log page with vanilla-JS EventSource UI"
```

---

## Block F — Final sweep + PR

### Task F1: Full suite green + open PR

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/python -m pytest tests/ -q 2>&1 | tail -10`
Expected: all green (existing + new tests).

- [ ] **Step 2: Manual smoke test** (~5 min)

```bash
mkdir -p /tmp/smoke-case/working
.venv/bin/python -m dsar_orchestrator.operator_console --case-dir /tmp/smoke-case --port 8089 &
for i in 1 2 3; do
  echo '{"ts":"2026-05-29T10:42:1'$i'Z","event_type":"REDACT_COMPLETED","refs_processed":1,"redactions_applied":1}' >> /tmp/smoke-case/working/audit_events.jsonl
  sleep 1
done
# Open http://127.0.0.1:8089/live-log — confirm events stream in, pause works, filters work.
```

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin worktree-operator-console-live-log

gh pr create --base main --title "feat(console): live-log feed (operator console)" --body "$(cat <<'EOF'
## Summary
- Plex-style live event view at `/live-log` in the operator console.
- Tails `working/audit_events.jsonl`, per-stage decision/finding jsonls
  (behind a verbose toggle), and `~/.dsar-audit/<case_no>/pipeline.jsonl`
  written by the existing `PipelineAuditor` + `StageBanner`. No new
  conductor producer code — the audit infrastructure that's already
  there IS the L3 source.
- Server-sent events via `/live-log/stream`; each frame passes through a
  fail-closed per-event-type field allowlist with a bounded-enum value
  scrubber so no raw PII reaches the browser. `note().message` is the
  one free-text PII surface; the projection drops it.
- Composite `Last-Event-ID` cursor for browser auto-reconnect with
  identity-validate-before-seek (rotation/truncation detected), 16 MiB
  resume backlog cap, 15 s heartbeat on an independent monotonic
  schedule, `SO_KEEPALIVE` + `SO_SNDTIMEO=30s` for hung-client safety.

Spec: `docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md`
(v2 revised after codebase deep-read; v1 frozen for reference.)
Plan: `docs/superpowers/plans/2026-05-29-operator-console-live-log-phase1.md`

## Test plan
- [ ] `tests/test_live_log_projection.py` (11 tests — allowlist, fail-closed, L3 dispatch)
- [ ] `tests/test_live_log_pii_regression.py` (29 tests — canaries through all 4 projection paths incl. L3 `note().message` drop)
- [ ] `tests/test_live_log_stream.py` (29 tests — JsonlTail, replay, live tail, rotation, truncation, cursor, heartbeat, burst-limited reads)
- [ ] `tests/test_live_log_route.py` (5 tests — SSE handler, malformed Last-Event-ID fallback, HTML page, nav link)
- [ ] Manual smoke test on a tmp case dir.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist

1. **Spec coverage:**
   - §3.1 L1 source: Tasks D1, D2 (open audit_events.jsonl). ✓
   - §3.1 L2 sources via `STAGE_ARTEFACTS`: Task D2 (`_l2_stage_artefact_filenames`). ✓
   - §3.1 L3 at `~/.dsar-audit/<case_no>/pipeline.jsonl`: Task D2 (`_l3_pipeline_jsonl_path`). ✓
   - §3.2 PII allowlist + L3 `event` dispatch + `note().message` drop: Tasks B1, B2. ✓
   - §3.3 30 s replay + 16 MiB cap + `replay_truncated`: Task D2. ✓
   - §3.4 single-thread, SO_KEEPALIVE, SO_SNDTIMEO, heartbeat: Tasks D3, E2. ✓
   - §3.5 composite cursor + identity-validate + resume backlog + source-vanished + heartbeat carries cursor: Tasks D1, D4, E2. ✓
   - §3.6 `(st_dev, st_ino)` only: Tasks C1, D3, D4. ✓
   - §4.1–4.2 routes + SSE handler: Tasks E2, E3. ✓
   - §4.3 helper module: Tasks C1, C2, D1–D4, E1. ✓
   - §4.4 conductor change: **none — L3 reuses existing file.** ✓
   - §4.5 data contract: Tasks D3, E2. ✓
   - §4.6 browser UX: Task E3. ✓
   - §6 invariants 1–15: distributed across Tasks C1, C2, D1, D3, D4, E2. ✓
   - §7 test plan: every test id maps to a test file. ✓
2. **Placeholder scan:** no TBDs. Every code block contains actual code.
3. **Type consistency:** `JsonlTail`, `LiveEvent`, `ResumeCursor`, `_PerSourceCursor`, `INITIAL_ZERO_CURSOR`, composite cursor format `<name>:<offset>:<dev>:<ino>` defined in D1 and used consistently in D2/D3/D4/E2.
4. **Integration sanity (from deep-read):**
   - `_CFG: ServerConfig` is the case_dir mechanism (used in E2 test fixture). ✓
   - `STAGE_ARTEFACTS` is the L2 source map (used in D2 helper). ✓
   - L3 lives at `~/.dsar-audit/<case_no>/pipeline.jsonl`, not under `case_dir/working`. ✓
   - No new producer-side code in `pipeline.py`. ✓
5. **Scope:** one PR (~600 LOC + tests). 10 tasks across 4 blocks.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-29-operator-console-live-log-phase1.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task; spec then code-quality review between tasks; jury before each commit.

**2. Inline Execution** — drive tasks in this session via `executing-plans`, batched checkpoints.

Which approach?
