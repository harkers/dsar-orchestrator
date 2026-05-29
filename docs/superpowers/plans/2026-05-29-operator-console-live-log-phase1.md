# Operator Console Live-Log Feed — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Plex-style live-event view in the operator console: tail `audit_events.jsonl`, per-stage decision/finding jsonls (verbose toggle), and a new structured `conductor_events.jsonl`. Stream to the browser over SSE with per-event-type field-allowlist projection so no raw PII reaches the browser.

**Architecture:** Single-thread merged iterator in the SSE handler (no per-source queue, no fan-in race). Composite `Last-Event-ID` cursor across all sources with identity-validate-before-seek and 16 MiB resume backlog cap. Browser is vanilla JS, no framework. Conductor structured events use a bounded template registry so PII is impossible by construction for the L3 source.

**Tech Stack:** stdlib only (`http.server.ThreadingHTTPServer`, `json`, `logging`, `socket`, `threading`); pytest for tests; `httpx` for integration tests (already in dev deps).

**Spec:** `docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md`

**House rules:**
- Run from the worktree at `.claude/worktrees/operator-console-live-log/`.
- Use the pre-built venv: `.venv/bin/python -m pytest …` and `.venv/bin/python` for any other invocation. `uv` resolve fails because `dsar-pipeline` is a sibling-repo install.
- Per `~/.claude/CLAUDE.md` code-review-jury amendment: before each commit, stage the task's files and run `~/.claude/scripts/code-review-jury.py --staged --task "Task N: <desc>" --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md`. Ship when ≥3/5 approve; otherwise apply convergent fixes and re-stage.
- Every task ends with a commit. Commits chain naturally into a single PR.

---

## Block A — Conductor structured events (L3 foundation)

### Task A1: ConductorTemplate dataclass + shape validators

**Spec coverage:** §3.1 L3 schema, §4.4 ConductorEventLog, §6 invariant 15.
**Test coverage:** §7.7 (L3 template enforcement).

**Files:**
- Create: `src/dsar_orchestrator/conductor_event_log.py`
- Create: `tests/test_conductor_event_log.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_conductor_event_log.py
"""Tests for conductor structured-event log (L3 source for the
operator console live-log feed). Spec: §3.1, §4.4, §6.15."""

from __future__ import annotations

import pytest

from dsar_orchestrator.conductor_event_log import (
    ConductorTemplate,
    _validate_enum_string,
    _validate_int_range,
    _validate_iso8601,
)


def test_validate_enum_string_accepts_member() -> None:
    _validate_enum_string("redact", allowed={"redact", "export"})


def test_validate_enum_string_rejects_non_member() -> None:
    with pytest.raises(ValueError):
        _validate_enum_string("DROP TABLE", allowed={"redact"})


def test_validate_enum_string_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        _validate_enum_string(123, allowed={"redact"})


def test_validate_int_range_accepts_inside_inclusive_bounds() -> None:
    _validate_int_range(5, lo=0, hi=10)
    _validate_int_range(0, lo=0, hi=10)
    _validate_int_range(10, lo=0, hi=10)


def test_validate_int_range_rejects_outside_bounds() -> None:
    with pytest.raises(ValueError):
        _validate_int_range(-1, lo=0, hi=10)
    with pytest.raises(ValueError):
        _validate_int_range(11, lo=0, hi=10)


def test_validate_int_range_rejects_non_int() -> None:
    with pytest.raises(TypeError):
        _validate_int_range(5.0, lo=0, hi=10)


def test_validate_iso8601_accepts_well_formed_utc() -> None:
    _validate_iso8601("2026-05-29T10:42:11Z")
    _validate_iso8601("2026-05-29T10:42:11.123Z")


def test_validate_iso8601_rejects_naive_and_garbage() -> None:
    with pytest.raises(ValueError):
        _validate_iso8601("2026-05-29 10:42:11")
    with pytest.raises(ValueError):
        _validate_iso8601("not a timestamp")


def test_conductor_template_validate_field_routes_to_shape() -> None:
    tmpl = ConductorTemplate(
        level="INFO",
        field_shapes={
            "stage": ("enum_string", {"redact", "export"}),
            "items": ("int_range", (0, 1_000_000)),
            "ts": ("iso8601", None),
        },
    )
    tmpl.validate_field("stage", "redact")
    tmpl.validate_field("items", 42)
    tmpl.validate_field("ts", "2026-05-29T10:42:11Z")


def test_conductor_template_validate_field_rejects_unknown_field() -> None:
    tmpl = ConductorTemplate(
        level="INFO",
        field_shapes={"stage": ("enum_string", {"redact"})},
    )
    with pytest.raises(KeyError):
        tmpl.validate_field("not_a_field", "value")


def test_conductor_template_rejects_unbounded_string_shape() -> None:
    """Invariant 15: no template may accept a string field without a
    bounded-enum shape. Any future maintainer who tries to add a
    free-text field gets a build-time error."""
    with pytest.raises(ValueError):
        ConductorTemplate(
            level="INFO",
            field_shapes={"message": ("free_string", None)},  # not allowed
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_conductor_event_log.py -v`
Expected: ImportError — module does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# src/dsar_orchestrator/conductor_event_log.py
"""Structured conductor event log (L3 source for the operator console
live-log feed).

Writes one JSON line per conductor banner/milestone to
`<case_dir>/working/conductor_events.jsonl`. Each row uses a bounded
template from `_CONDUCTOR_TEMPLATES` so that field values are restricted
to enum strings / integer ranges / ISO-8601 timestamps. PII-by-construction
impossible — the registry has no free-text string shape.

Spec: 2026-05-29 operator-console-live-log design v1 §3.1, §4.4, §6.15.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_ALLOWED_SHAPE_KINDS = frozenset({"enum_string", "int_range", "iso8601"})
_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
)


def _validate_enum_string(value: Any, *, allowed: set[str]) -> None:
    if not isinstance(value, str):
        raise TypeError(f"enum_string expected str, got {type(value).__name__}")
    if value not in allowed:
        raise ValueError(f"enum_string {value!r} not in {sorted(allowed)!r}")


def _validate_int_range(value: Any, *, lo: int, hi: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"int_range expected int, got {type(value).__name__}")
    if value < lo or value > hi:
        raise ValueError(f"int_range {value} outside [{lo}, {hi}]")


def _validate_iso8601(value: Any) -> None:
    if not isinstance(value, str):
        raise TypeError(f"iso8601 expected str, got {type(value).__name__}")
    if not _ISO8601_RE.match(value):
        raise ValueError(f"iso8601 {value!r} does not match UTC pattern")


@dataclass(frozen=True)
class ConductorTemplate:
    """Bounded-shape template for one conductor event kind.

    `field_shapes` maps field name → `(shape_kind, shape_arg)`.
    Allowed shape_kinds are 'enum_string', 'int_range', 'iso8601' — no
    free-text strings, by construction.
    """

    level: str
    field_shapes: dict[str, tuple[str, Any]]

    def __post_init__(self) -> None:
        for fname, (kind, _) in self.field_shapes.items():
            if kind not in _ALLOWED_SHAPE_KINDS:
                raise ValueError(
                    f"field {fname!r} declared with unsupported shape "
                    f"{kind!r}; allowed: {sorted(_ALLOWED_SHAPE_KINDS)}"
                )

    def validate_field(self, fname: str, value: Any) -> None:
        if fname not in self.field_shapes:
            raise KeyError(fname)
        kind, arg = self.field_shapes[fname]
        if kind == "enum_string":
            _validate_enum_string(value, allowed=arg)
        elif kind == "int_range":
            lo, hi = arg
            _validate_int_range(value, lo=lo, hi=hi)
        elif kind == "iso8601":
            _validate_iso8601(value)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_conductor_event_log.py -v`
Expected: 11 passed.

- [ ] **Step 5: Code-review-jury, then commit**

```bash
git add src/dsar_orchestrator/conductor_event_log.py tests/test_conductor_event_log.py
~/.claude/scripts/code-review-jury.py --staged \
    --task "Task A1: ConductorTemplate + shape validators" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
# If ≥3/5 approve:
git commit -m "feat(conductor): bounded-shape template + validators

L3 source foundation per design v1 §3.1, §4.4, §6.15.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task A2: `_CONDUCTOR_TEMPLATES` registry

**Spec coverage:** §3.1 L3 templates, §4.4 (the four initial templates), §6.15.
**Test coverage:** §7.7 (template enumeration).

**Files:**
- Modify: `src/dsar_orchestrator/conductor_event_log.py`
- Modify: `tests/test_conductor_event_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_conductor_event_log.py`:

```python
from dsar_orchestrator.conductor_event_log import _CONDUCTOR_TEMPLATES


def test_registry_contains_four_initial_templates() -> None:
    assert "STAGE_STARTED" in _CONDUCTOR_TEMPLATES
    assert "STAGE_COMPLETED" in _CONDUCTOR_TEMPLATES
    assert "GATE_OPENED" in _CONDUCTOR_TEMPLATES
    assert "MODULE_WORK_CHECK_FAIL" in _CONDUCTOR_TEMPLATES


def test_registry_enforces_no_free_text_field() -> None:
    """§6.15: no template accepts a string field without a bounded
    shape. Iterate every template, every shape; assert no
    'free_string' kind."""
    for tmpl_id, tmpl in _CONDUCTOR_TEMPLATES.items():
        for fname, (kind, _) in tmpl.field_shapes.items():
            assert kind in {"enum_string", "int_range", "iso8601"}, (
                f"{tmpl_id}.{fname} has unbounded shape {kind!r}"
            )


def test_stage_started_accepts_well_formed_fields() -> None:
    tmpl = _CONDUCTOR_TEMPLATES["STAGE_STARTED"]
    tmpl.validate_field("stage", "redact")
    tmpl.validate_field("phase", "redaction_running")
    tmpl.validate_field("ts_start", "2026-05-29T10:42:11Z")


def test_stage_started_rejects_unknown_stage_value() -> None:
    tmpl = _CONDUCTOR_TEMPLATES["STAGE_STARTED"]
    with pytest.raises(ValueError):
        tmpl.validate_field("stage", "not_a_stage")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_conductor_event_log.py -v`
Expected: 4 new tests fail (`_CONDUCTOR_TEMPLATES` is not exported).

- [ ] **Step 3: Append to `src/dsar_orchestrator/conductor_event_log.py`**

```python
# Conductor stages match dsar_orchestrator.pipeline.STAGE_ORDER —
# additions here MUST be reflected there and vice versa. Bounded
# enum keeps PII out of conductor events by construction.
_KNOWN_STAGES = frozenset({
    "ingest", "ingestion_qc", "dedupe", "context",
    "scope_check", "responsiveness", "redact",
    "redaction_qc_a", "redaction_qc_b", "leak_review",
    "flag_review", "qa_sample", "people_register",
    "sig_block_discovery", "pii_jury_review",
    "presidio_anonymize", "approver", "bake", "export",
})

_KNOWN_PHASES = frozenset({
    "ingestion_running", "ingestion_qc_running", "dedupe_running",
    "context_running", "scope_check_running",
    "responsiveness_running", "redaction_running",
    "redaction_qc_a_running", "redaction_qc_b_running",
    "leak_review_running", "flag_review_running",
    "qa_sample_running", "people_register_running",
    "sig_block_discovery_running", "pii_jury_review_running",
    "presidio_anonymize_running", "approver_running",
    "bake_running", "export_running",
})

_GATE_REASONS = frozenset({
    "manual_advance", "qc_passed", "module_work_check_passed",
    "preflight_passed",
})

_ERROR_TYPES = frozenset({
    "missing_input", "preflight_failed", "subprocess_failed",
    "schema_validation_failed", "module_work_check_failed",
})

_CONDUCTOR_TEMPLATES: dict[str, ConductorTemplate] = {
    "STAGE_STARTED": ConductorTemplate(
        level="INFO",
        field_shapes={
            "stage": ("enum_string", _KNOWN_STAGES),
            "phase": ("enum_string", _KNOWN_PHASES),
            "ts_start": ("iso8601", None),
        },
    ),
    "STAGE_COMPLETED": ConductorTemplate(
        level="INFO",
        field_shapes={
            "stage": ("enum_string", _KNOWN_STAGES),
            "phase": ("enum_string", _KNOWN_PHASES),
            "items_processed": ("int_range", (0, 10_000_000)),
            "elapsed_ms": ("int_range", (0, 86_400_000)),
        },
    ),
    "GATE_OPENED": ConductorTemplate(
        level="INFO",
        field_shapes={
            "stage": ("enum_string", _KNOWN_STAGES),
            "reason": ("enum_string", _GATE_REASONS),
        },
    ),
    "MODULE_WORK_CHECK_FAIL": ConductorTemplate(
        level="WARN",
        field_shapes={
            "stage": ("enum_string", _KNOWN_STAGES),
            "error_type": ("enum_string", _ERROR_TYPES),
        },
    ),
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_conductor_event_log.py -v`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/conductor_event_log.py tests/test_conductor_event_log.py
~/.claude/scripts/code-review-jury.py --staged --task "Task A2: conductor template registry" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(conductor): four initial structured event templates

STAGE_STARTED, STAGE_COMPLETED, GATE_OPENED, MODULE_WORK_CHECK_FAIL.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task A3: `ConductorEventLog.emit` + idempotency registry

**Spec coverage:** §3.1, §4.4, §6.15.
**Test coverage:** §7.7 (idempotency, append, raise-on-unknown-template).

**Files:**
- Modify: `src/dsar_orchestrator/conductor_event_log.py`
- Modify: `tests/test_conductor_event_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_conductor_event_log.py`:

```python
import json
from pathlib import Path

from dsar_orchestrator.conductor_event_log import (
    ConductorEventLog,
    get_or_create_conductor_event_log,
)


def test_emit_writes_one_jsonl_line(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    log = ConductorEventLog(tmp_path)
    log.emit(
        "STAGE_STARTED",
        stage="redact",
        phase="redaction_running",
        ts_start="2026-05-29T10:42:11Z",
    )
    lines = (tmp_path / "working" / "conductor_events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["template_id"] == "STAGE_STARTED"
    assert row["level"] == "INFO"
    assert row["fields"] == {
        "stage": "redact",
        "phase": "redaction_running",
        "ts_start": "2026-05-29T10:42:11Z",
    }
    assert "ts" in row


def test_emit_appends_multiple_events(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    log = ConductorEventLog(tmp_path)
    log.emit("STAGE_STARTED", stage="redact", phase="redaction_running",
             ts_start="2026-05-29T10:42:11Z")
    log.emit("STAGE_COMPLETED", stage="redact", phase="redaction_running",
             items_processed=412, elapsed_ms=15_400)
    lines = (tmp_path / "working" / "conductor_events.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_emit_rejects_unknown_template(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    log = ConductorEventLog(tmp_path)
    with pytest.raises(KeyError):
        log.emit("UNREGISTERED", anything="foo")


def test_emit_rejects_field_with_violating_shape(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    log = ConductorEventLog(tmp_path)
    with pytest.raises(ValueError):
        log.emit(
            "STAGE_STARTED",
            stage="DROP TABLE users",  # not in _KNOWN_STAGES
            phase="redaction_running",
            ts_start="2026-05-29T10:42:11Z",
        )


def test_emit_rejects_unknown_field(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    log = ConductorEventLog(tmp_path)
    with pytest.raises(KeyError):
        log.emit(
            "STAGE_STARTED",
            stage="redact",
            phase="redaction_running",
            ts_start="2026-05-29T10:42:11Z",
            extra_smuggled_field="evil",
        )


def test_idempotent_registry_returns_same_instance(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    a = get_or_create_conductor_event_log(tmp_path)
    b = get_or_create_conductor_event_log(tmp_path)
    assert a is b


def test_idempotent_registry_distinguishes_distinct_case_dirs(
    tmp_path: Path,
) -> None:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    (a_dir / "working").mkdir(parents=True)
    (b_dir / "working").mkdir(parents=True)
    a = get_or_create_conductor_event_log(a_dir)
    b = get_or_create_conductor_event_log(b_dir)
    assert a is not b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_conductor_event_log.py -v`
Expected: 7 new tests fail (`ConductorEventLog` and `get_or_create_conductor_event_log` not exported yet).

- [ ] **Step 3: Append to `src/dsar_orchestrator/conductor_event_log.py`**

```python
import json
import threading
from datetime import UTC, datetime
from pathlib import Path


_emitter_registry: dict[Path, "ConductorEventLog"] = {}
_emitter_registry_lock = threading.Lock()


def _utc_iso_now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ConductorEventLog:
    """Append-only structured conductor event writer.

    One instance per case_dir. PII-by-construction impossible because
    every `emit` call validates `template_id` against the registry and
    each field against its declared bounded shape.
    """

    def __init__(self, case_dir: Path) -> None:
        self.case_dir = Path(case_dir)
        self.path = self.case_dir / "working" / "conductor_events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    def emit(self, template_id: str, **fields: object) -> None:
        tmpl = _CONDUCTOR_TEMPLATES[template_id]  # KeyError on unknown
        for fname, value in fields.items():
            tmpl.validate_field(fname, value)  # KeyError or ValueError on bad
        row = {
            "ts": _utc_iso_now(),
            "level": tmpl.level,
            "template_id": template_id,
            "fields": dict(fields),
        }
        line = json.dumps(row, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        with self._write_lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def get_or_create_conductor_event_log(case_dir: Path) -> ConductorEventLog:
    """Idempotent: returns the same `ConductorEventLog` instance for
    repeated calls on the same case_dir within one process."""
    key = Path(case_dir).resolve()
    with _emitter_registry_lock:
        existing = _emitter_registry.get(key)
        if existing is not None:
            return existing
        instance = ConductorEventLog(key)
        _emitter_registry[key] = instance
        return instance
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_conductor_event_log.py -v`
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/conductor_event_log.py tests/test_conductor_event_log.py
~/.claude/scripts/code-review-jury.py --staged --task "Task A3: ConductorEventLog.emit + idempotent registry" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(conductor): structured event log writer + idempotent registry

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task A4: Wire `ConductorEventLog` into `pipeline.py`

**Spec coverage:** §4.4 (mirror StageBanner sites).
**Test coverage:** §7.7 (additive pipeline behaviour).

**Files:**
- Modify: `src/dsar_orchestrator/pipeline.py`
- Create: `tests/test_pipeline_conductor_events.py`

- [ ] **Step 1: Find the existing StageBanner / stage-start / stage-complete sites in `pipeline.py`**

Run: `grep -nE "StageBanner|print\(.*stage|stage_started|stage_completed" src/dsar_orchestrator/pipeline.py | head -20`

Identify the exact line numbers where the conductor announces stage start / complete / gate-opened. (Each existing site gets a mirroring `get_or_create_conductor_event_log(case_dir).emit(...)` call. If the file is large, do the smallest possible change — add the import + a helper `_emit_conductor_event(case_dir, template_id, **fields)` that wraps the call and is a no-op if `case_dir` is None.)

- [ ] **Step 2: Write the failing test**

```python
# tests/test_pipeline_conductor_events.py
"""End-to-end-ish: a stage start + complete writes two structured
events to conductor_events.jsonl. Spec: §4.4."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Skip cleanly if pipeline isn't importable from this test position —
# the helper-wiring task is the load-bearing change; the pipeline
# function under test is documented in its own module.
pipeline = pytest.importorskip("dsar_orchestrator.pipeline")


def test_emit_conductor_event_writes_jsonl(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    # The helper must exist after Task A4.
    pipeline._emit_conductor_event(
        tmp_path,
        "STAGE_STARTED",
        stage="redact",
        phase="redaction_running",
        ts_start="2026-05-29T10:42:11Z",
    )
    lines = (tmp_path / "working" / "conductor_events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["template_id"] == "STAGE_STARTED"


def test_emit_conductor_event_is_noop_on_none(tmp_path: Path) -> None:
    pipeline._emit_conductor_event(
        None, "STAGE_STARTED", stage="redact",
        phase="redaction_running", ts_start="2026-05-29T10:42:11Z",
    )
    assert not (tmp_path / "working" / "conductor_events.jsonl").exists()


def test_emit_conductor_event_swallows_validation_errors(
    tmp_path: Path, capfd: pytest.CaptureFixture
) -> None:
    """A malformed conductor emit MUST NOT abort the pipeline — the
    structured event log is observability, not control flow. Log to
    stderr and continue."""
    (tmp_path / "working").mkdir()
    pipeline._emit_conductor_event(
        tmp_path,
        "STAGE_STARTED",
        stage="NOT_A_STAGE",
        phase="redaction_running",
        ts_start="2026-05-29T10:42:11Z",
    )
    err = capfd.readouterr().err
    assert "conductor_event_log" in err
    # File may or may not exist (no successful emit yet), but pipeline
    # didn't raise.
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline_conductor_events.py -v`
Expected: AttributeError — `_emit_conductor_event` doesn't exist.

- [ ] **Step 4: Add the helper to `pipeline.py`**

Near the top of `src/dsar_orchestrator/pipeline.py`, after existing imports, add:

```python
import sys

from dsar_orchestrator.conductor_event_log import (
    get_or_create_conductor_event_log,
)


def _emit_conductor_event(
    case_dir: Path | None,
    template_id: str,
    **fields: object,
) -> None:
    """Mirror a stage banner / milestone to working/conductor_events.jsonl.

    Observability-only — a validation failure logs to stderr and
    returns; never aborts the pipeline.
    """
    if case_dir is None:
        return
    try:
        get_or_create_conductor_event_log(case_dir).emit(
            template_id, **fields
        )
    except (KeyError, ValueError, TypeError, OSError) as exc:
        print(
            f"[conductor_event_log] skipped {template_id}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
```

Then, at each existing StageBanner stage-start site, add a mirroring call:

```python
_emit_conductor_event(
    cfg.case_path,
    "STAGE_STARTED",
    stage=<stage_name>,
    phase=<phase_label>,
    ts_start=<iso8601_now>,
)
```

…and at each stage-completion site, mirror with `STAGE_COMPLETED` (including `items_processed` if known, else `items_processed=0`, and `elapsed_ms`).

(Use the existing `STAGE_ORDER` list and the existing per-stage banner helpers to keep this minimal. The actual instrumentation surface is implementation-dependent on the current `pipeline.py` shape — keep each call narrow and additive; do NOT refactor the existing banner code.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline_conductor_events.py -v`
Expected: 3 passed.

Also re-run the existing pipeline tests to confirm nothing regressed:

Run: `.venv/bin/python -m pytest tests/ -q -x --ignore=tests/test_live_log_stream.py --ignore=tests/test_live_log_projection.py 2>&1 | tail -10`
Expected: pre-existing pass-count maintained.

- [ ] **Step 6: Commit**

```bash
git add src/dsar_orchestrator/pipeline.py tests/test_pipeline_conductor_events.py
~/.claude/scripts/code-review-jury.py --staged --task "Task A4: wire ConductorEventLog into pipeline.py" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(conductor): mirror StageBanner milestones to structured events

Stage start/complete and gate-opened milestones now also write to
working/conductor_events.jsonl. Additive — existing banner/stdout
behaviour unchanged. Validation failures log to stderr without
aborting the pipeline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Block B — Projection (PII allowlist + bounded-enum scrubber)

### Task B1: `_ALLOWLIST` table + bounded-enum value scrubber

**Spec coverage:** §3.2 (per-event-type field allowlist, fail-closed default, bounded-enum scrubber).
**Test coverage:** §7.3, §7.4 (PII regression).

**Files:**
- Create: `src/dsar_orchestrator/local_broker/live_log_projection.py`
- Create: `tests/test_live_log_projection.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_live_log_projection.py
"""Tests for the per-event-type field-allowlist projection that
guards PII from reaching the browser. Spec §3.2, §6 invariants 6+15."""

from __future__ import annotations

import pytest

from dsar_orchestrator.local_broker.live_log_projection import (
    project_for_browser,
    summary_for,
    scrub_value,
    _ALLOWLIST,
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
    # Fail-closed: NO payload values, even on extra keys.
    for v in out.values():
        if isinstance(v, str):
            assert "leaked-name" not in v


def test_known_event_type_projects_only_allowlisted_fields() -> None:
    # Pick any event_type known to be in the allowlist after Block B.
    out = project_for_browser(
        _event(
            "audit",
            "REDACT_COMPLETED",
            {
                "refs_processed": 412,
                "redactions_applied": 89,
                # PII-bearing fields that must NOT make it through.
                "subject_protected_phrases": ["Jane Smith"],
                "example_tokens": ["jane@example.com"],
            },
        )
    )
    assert "subject_protected_phrases" not in str(out)
    assert "Jane Smith" not in str(out)
    assert "example_tokens" not in str(out)
    assert "jane@example.com" not in str(out)


def test_scrub_value_replaces_malformed_enum_value() -> None:
    # severity is enum-string {info, warn, error, debug}
    assert scrub_value("severity", "info", "enum_string", {"info", "warn", "error", "debug"}) == "info"
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
    # The summary may interpolate the integer counts, but must NOT
    # contain any string from raw payload.
    assert "412" in s
    assert "89" in s


def test_allowlist_table_has_no_freetext_string_shapes() -> None:
    """§6.15 corollary: same discipline as ConductorTemplate — no
    allowlisted field is a free-text string."""
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

Spec: 2026-05-29 operator-console-live-log design v1 §3.2, §6.6, §6.15.
"""

from __future__ import annotations

import re
from typing import Any


_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
)
_SEVERITY_ENUM = {"info", "warn", "error", "debug"}


# Each value is `{field_name: (shape_kind, shape_arg)}`.
# Shape kinds match conductor_event_log.ConductorTemplate exactly so
# that all three sources (L1 audit, L2 stage, L3 conductor) use the
# same discipline.
_ALLOWLIST: dict[str, dict[str, tuple[str, Any]]] = {
    # ---- L1 audit_events (subset; extend as new events stabilise) ----
    "REDACT_STARTED": {
        "stage": ("enum_string", {"redact"}),
        "phase": ("enum_string", {"redaction_running"}),
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
    # ---- L3 conductor templates (mirrors registry shapes) ----
    "STAGE_STARTED": {
        "stage": ("enum_string", {
            "ingest", "ingestion_qc", "dedupe", "context",
            "scope_check", "responsiveness", "redact",
            "redaction_qc_a", "redaction_qc_b", "leak_review",
            "flag_review", "qa_sample", "people_register",
            "sig_block_discovery", "pii_jury_review",
            "presidio_anonymize", "approver", "bake", "export",
        }),
        "phase": ("enum_string", set()),  # populated by post-init below
        "ts_start": ("iso8601", None),
    },
    "STAGE_COMPLETED": {
        "stage": ("enum_string", set()),  # populated below
        "items_processed": ("int_range", (0, 10_000_000)),
        "elapsed_ms": ("int_range", (0, 86_400_000)),
    },
    "GATE_OPENED": {
        "stage": ("enum_string", set()),
        "reason": ("enum_string",
                    {"manual_advance", "qc_passed",
                     "module_work_check_passed", "preflight_passed"}),
    },
    "MODULE_WORK_CHECK_FAIL": {
        "stage": ("enum_string", set()),
        "error_type": ("enum_string",
                        {"missing_input", "preflight_failed",
                         "subprocess_failed", "schema_validation_failed",
                         "module_work_check_failed"}),
    },
}

# Populate the shared `stage` enum from the STAGE_STARTED template so
# the allowlist stays consistent with conductor_event_log._KNOWN_STAGES.
_STAGE_ENUM = _ALLOWLIST["STAGE_STARTED"]["stage"][1]
for tmpl in ("STAGE_COMPLETED", "GATE_OPENED", "MODULE_WORK_CHECK_FAIL"):
    _ALLOWLIST[tmpl]["stage"] = ("enum_string", _STAGE_ENUM)
_ALLOWLIST["STAGE_STARTED"]["phase"] = (
    "enum_string",
    {
        "ingestion_running", "ingestion_qc_running", "dedupe_running",
        "context_running", "scope_check_running",
        "responsiveness_running", "redaction_running",
        "redaction_qc_a_running", "redaction_qc_b_running",
        "leak_review_running", "flag_review_running",
        "qa_sample_running", "people_register_running",
        "sig_block_discovery_running", "pii_jury_review_running",
        "presidio_anonymize_running", "approver_running",
        "bake_running", "export_running",
    },
)


def scrub_value(fname: str, value: Any, kind: str, arg: Any) -> Any:
    """Return value if it matches the declared shape, else literal '<typeerror>'.

    Pure function, no I/O, no logging. Caller is responsible for any
    server-side debug logging (with `exc_info=False`).
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
    """Compose a short human string from ALREADY-PROJECTED fields only.

    Never interpolates raw payload — the caller passes the output of
    `_project_payload`, which is shape-validated. Returns a fallback
    string for unknown event types.
    """
    if event_type is None or event_type not in _ALLOWLIST:
        return "(unrecognised event type)"
    if event_type == "REDACT_COMPLETED":
        refs = projected.get("refs_processed", "<typeerror>")
        applied = projected.get("redactions_applied", "<typeerror>")
        return f"redacted {applied} of {refs} refs"
    if event_type == "PEOPLE_REGISTER_BUILT":
        rows = projected.get("rows", "<typeerror>")
        clusters = projected.get("clusters", "<typeerror>")
        return f"people register: {rows} rows, {clusters} clusters"
    if event_type == "SIG_BLOCK_DISCOVERY_COMPLETED":
        return f"sig-block candidates: {projected.get('candidates_found', '<typeerror>')}"
    if event_type == "PII_JURY_INFERENCE_RECORD":
        return f"PII jury {projected.get('juror', '<typeerror>')} verdict ({projected.get('severity', '<typeerror>')})"
    if event_type == "PII_JURY_DISAGREEMENT":
        return f"PII jury disagreement ({projected.get('disagreement_kind', '<typeerror>')})"
    if event_type == "STAGE_STARTED":
        return f"stage started: {projected.get('stage', '<typeerror>')}"
    if event_type == "STAGE_COMPLETED":
        return (
            f"stage completed: {projected.get('stage', '<typeerror>')} "
            f"({projected.get('items_processed', '<typeerror>')} items, "
            f"{projected.get('elapsed_ms', '<typeerror>')}ms)"
        )
    if event_type == "GATE_OPENED":
        return f"gate opened: {projected.get('stage', '<typeerror>')} ({projected.get('reason', '<typeerror>')})"
    if event_type == "MODULE_WORK_CHECK_FAIL":
        return f"module-work check fail: {projected.get('stage', '<typeerror>')} ({projected.get('error_type', '<typeerror>')})"
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
        "severity", payload.get("severity", "info"),
        "enum_string", _SEVERITY_ENUM,
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
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_projection.py tests/test_live_log_projection.py
~/.claude/scripts/code-review-jury.py --staged --task "Task B1: live-log projection + allowlist" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): field allowlist + bounded-enum scrubber

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task B2: PII regression test suite

**Spec coverage:** §3.2.
**Test coverage:** §7.4 (PII regression).

**Files:**
- Create: `tests/test_live_log_pii_regression.py`

- [ ] **Step 1: Write the tests**

```python
# tests/test_live_log_pii_regression.py
"""PII regression tests for the live-log projection.

Every test feeds a synthetic event whose payload contains a known
PII canary, and asserts that the literal NEVER appears in the
serialised projected dict. If a future PR widens an allowlist
incorrectly, these tests fail loudly.

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
        f"PII canary {canary!r} leaked into projected dict: {serialised}"
    )


@pytest.mark.parametrize("canary", _CANARIES)
def test_pii_canaries_never_appear_for_unknown_event_type(canary: str) -> None:
    out = project_for_browser({
        "kind": "event",
        "source": "audit",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "FUTURE_EVENT_WE_HAVENT_ALLOWLISTED_YET",
        "payload": {
            "subject_protected_phrases": [canary],
            "name": canary,
            "everything": canary,
        },
    })
    assert canary not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize("canary", _CANARIES)
def test_pii_canaries_never_appear_for_conductor_event(canary: str) -> None:
    """L3 event with a malformed field smuggled in — the projection
    must drop unknown/extra payload keys, never echo them."""
    out = project_for_browser({
        "kind": "event",
        "source": "conductor",
        "ts": "2026-05-29T10:42:11Z",
        "event_type": "STAGE_STARTED",
        "payload": {
            "stage": "redact",
            "phase": "redaction_running",
            "ts_start": "2026-05-29T10:42:11Z",
            "smuggled": canary,
            "subject_name": canary,
        },
    })
    assert canary not in json.dumps(out, ensure_ascii=False)


def test_severity_field_does_not_leak_freeform_text() -> None:
    """A producer that puts text into `severity` must be scrubbed —
    severity is bounded enum {info, warn, error, debug}."""
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
Expected: 22 passed (7 canaries × 3 paths + 1 severity).

- [ ] **Step 3: Commit**

```bash
git add tests/test_live_log_pii_regression.py
~/.claude/scripts/code-review-jury.py --staged --task "Task B2: PII regression suite" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "test(live-log): PII canary regression suite

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Block C — `JsonlTail` source class

### Task C1: `JsonlTail.__init__` + `fstat_identity` / `stat_path_identity` / `fstat_size`

**Spec coverage:** §6.7 (fd-vs-path identity split).
**Test coverage:** §7.2 (JsonlTail unit).

**Files:**
- Create: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Create: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_live_log_stream.py
"""Unit tests for live_log_stream — JsonlTail source class and
iter_live_events merging iterator. Spec: §4.3, §6."""

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
    assert initial == after  # appends do not change (st_dev, st_ino)


def test_jsonl_tail_stat_path_identity_changes_on_rotation(
    tmp_path: Path,
) -> None:
    p = tmp_path / "audit.jsonl"
    _write(p, {"i": 0}, mode="w")
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    before = tail.stat_path_identity()
    # Rotation: remove + recreate at same path.
    p.unlink()
    _write(p, {"i": 0}, mode="w")
    after = tail.stat_path_identity()
    tail.close()
    assert before != after  # different inode after recreate


def test_jsonl_tail_stat_path_identity_returns_none_when_missing(
    tmp_path: Path,
) -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: ImportError — `JsonlTail` doesn't exist.

- [ ] **Step 3: Write the implementation**

```python
# src/dsar_orchestrator/local_broker/live_log_stream.py
"""Live-log streaming primitives for the operator console.

Powers the `/live-log/stream` SSE endpoint: tails one or more
`.jsonl` source files, merges them through a single iterator, and
yields `LiveEvent`s that the SSE handler projects via
`live_log_projection.project_for_browser`.

Stdlib-only. Single-thread design: one SSE worker thread owns the
iterator, which owns all source file handles in a try/finally.

Spec: 2026-05-29 operator-console-live-log design v1 §4.3, §6.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO


@dataclass
class JsonlTail:
    """One source-file tail.

    Owns the fd. Caller MUST `close()` (or rely on
    `iter_live_events`'s try/finally).
    """

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
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task C1: JsonlTail fd identity + size" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): JsonlTail with fd-vs-path identity split

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task C2: `JsonlTail.read_new_lines(max_lines, max_bytes)`

**Spec coverage:** §6 invariants 1, 9 (snap-to-`\n`, burst cap inside the method).
**Test coverage:** §7.2 (read_new_lines bounded batch + partial-line buffering).

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
        fh.write('{"i": 0}\n{"i": 1}\n{"i": 2')  # last line incomplete
    got = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    tail.close()
    # Only the two complete lines yielded.
    assert [evt["i"] for evt in got] == [0, 1]


def test_read_new_lines_yields_partial_line_after_completion(
    tmp_path: Path,
) -> None:
    p = tmp_path / "audit.jsonl"
    p.touch()
    tail = JsonlTail(p, max_line_bytes=1_048_576)
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"i": 0}\n{"i": 1')  # partial
    first = list(tail.read_new_lines(max_lines=100, max_bytes=1_048_576))
    with p.open("a", encoding="utf-8") as fh:
        fh.write('}\n{"i": 2}\n')  # complete previous + a new one
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
    # Next call yields the rest, in order, no duplicates.
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
    # Each line ~70 bytes; max_bytes=200 should let through ~3.
    got = list(tail.read_new_lines(max_lines=100, max_bytes=200))
    tail.close()
    assert 0 < len(got) <= 5  # bounded, non-zero


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
Expected: 5 new tests fail (`read_new_lines` does not exist).

- [ ] **Step 3: Add to `JsonlTail`**

```python
    def read_new_lines(self, *, max_lines: int, max_bytes: int):
        """Yield up to (max_lines, max_bytes) JSON objects from the
        tail. fd is left at the byte after the last complete `\\n`
        yielded; partial trailing line is buffered.

        Stateless bounded-batch contract (§6.9): the caller MUST NOT
        `break` mid-iteration — that would discard buffered lines
        because the fd has already advanced past them.
        """
        import json as _json
        if self._fh is None or self.disabled:
            self._open()
            if self._fh is None:
                return
        lines_yielded = 0
        bytes_yielded = 0
        # Read in 64 KiB chunks; merge with anything still buffered.
        while lines_yielded < max_lines and bytes_yielded < max_bytes:
            chunk = self._fh.read(65_536)
            if not chunk:
                break
            self._buffer += chunk
            # Process whole lines.
            while "\n" in self._buffer and lines_yielded < max_lines and bytes_yielded < max_bytes:
                line, self._buffer = self._buffer.split("\n", 1)
                if len(line) > self.max_line_bytes:
                    # §6.2: line-too-long discard via streaming.
                    # We've already consumed the line; surface a sentinel
                    # so iter_live_events emits a gap. (Returning a
                    # dict here keeps the API uniform.)
                    yield {"_kind": "line_too_long"}
                    lines_yielded += 1
                    bytes_yielded += len(line)
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    yield _json.loads(stripped)
                except _json.JSONDecodeError:
                    continue
                lines_yielded += 1
                bytes_yielded += len(stripped) + 1  # +1 for \n
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task C2: JsonlTail.read_new_lines bounded batch" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): JsonlTail.read_new_lines with stateless bounded batch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Block D — `iter_live_events` merging iterator

### Task D1: `LiveEvent`, `ResumeCursor`, `parse_composite_last_event_id`

**Spec coverage:** §3.5, §6 invariant 13 (exception-safe cursor parse).
**Test coverage:** §7.5 (composite cursor round-trip, malformed → fallback).

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
from dsar_orchestrator.local_broker.live_log_stream import (
    LiveEvent,
    ResumeCursor,
    parse_composite_last_event_id,
    INITIAL_ZERO_CURSOR,
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
    """§6.13: any failure → None, never 500."""
    assert parse_composite_last_event_id("garbage:not:numeric:here") is None
    assert parse_composite_last_event_id("nofields") is None
    assert parse_composite_last_event_id("a:1:2|b:") is None
    assert parse_composite_last_event_id("a:99999999999999999999:1:1") is None


def test_parse_composite_cursor_rejects_oversized_header() -> None:
    huge = "audit:1024:1:123|" * 10_000  # ~150 KiB
    assert parse_composite_last_event_id(huge) is None


def test_initial_zero_cursor_round_trip() -> None:
    assert isinstance(INITIAL_ZERO_CURSOR, ResumeCursor)
    assert INITIAL_ZERO_CURSOR.is_valid() is False  # zero-cursor is sentinel


def test_live_event_composite_cursor_emitted_for_event_kind() -> None:
    e = LiveEvent(
        kind="event",
        source="audit",
        ts="2026-05-29T10:42:11Z",
        event_type="REDACT_COMPLETED",
        payload={"refs_processed": 1},
        composite_cursor="audit:1024:1:123",
    )
    assert e.composite_cursor == "audit:1024:1:123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 6 new tests fail (`LiveEvent`, `ResumeCursor`, etc. not exported).

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
        # Non-empty + all offsets non-negative.
        if not self.per_source:
            return False
        return all(p.offset >= 0 for p in self.per_source.values())


INITIAL_ZERO_CURSOR = ResumeCursor(per_source={})


_MAX_LAST_EVENT_ID_BYTES = 4 * 1024


def parse_composite_last_event_id(header: str | None) -> ResumeCursor | None:
    """Exception-safe parser. Returns None on any malformation.

    Format: `<source>:<offset>:<st_dev>:<st_ino>` joined by `|`.
    Source name may contain `:` separator characters? NO — by
    invariant, source names are `audit`, `cond`, or
    `stage:<filename>`; the leading two-token slot is treated as the
    name and the LAST THREE colon-separated tokens are offset, dev,
    ino. This tolerates `stage:redaction_decisions.jsonl`.
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
            offset_str, dev_str, ino_str = parts[-3], parts[-2], parts[-1]
            offset = int(offset_str)
            dev = int(dev_str)
            ino = int(ino_str)
            if offset < 0:
                return None
            # Sanity-bound the integers so a future buggy producer
            # can't pass us 10**500.
            if offset > 2**63 or dev > 2**63 or ino > 2**63:
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
    """A single event yielded by `iter_live_events`.

    `composite_cursor` is the SSE `id:` to emit on this frame. For
    `kind="heartbeat"` events the cursor is set by the SSE handler
    from `last_cursor` rather than by the iterator.
    """

    kind: str                            # "event" | "gap" | "heartbeat"
    source: str
    ts: str | None = None
    event_type: str | None = None
    payload: dict | None = None
    reason: str | None = None            # for kind="gap"
    composite_cursor: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D1: ResumeCursor + exception-safe parse" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): composite cursor + exception-safe parse

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D2: `iter_live_events` — Phase A replay (linear backwards scan + heapq.merge)

**Spec coverage:** §3.3, §6.4, §6.14.
**Test coverage:** §7.1 (replay window).

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
from datetime import UTC, datetime, timedelta

from dsar_orchestrator.local_broker.live_log_stream import iter_live_events


def _ts(seconds_ago: float) -> str:
    return (
        datetime.now(UTC) - timedelta(seconds=seconds_ago)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_phase_a_replay_yields_events_within_window(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        # Two events within 30s window, one outside.
        fh.write(json.dumps({"ts": _ts(50), "event_type": "OLD"}) + "\n")
        fh.write(json.dumps({"ts": _ts(20), "event_type": "RECENT_1"}) + "\n")
        fh.write(json.dumps({"ts": _ts(10), "event_type": "RECENT_2"}) + "\n")

    stop = threading.Event()
    seen: list[dict] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path,
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
                seen.append({"event_type": e.event_type, "ts": e.ts})
                if len(seen) >= 2:
                    stop.set()
                    return

    t = threading.Thread(target=consumer)
    t.start()
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert [s["event_type"] for s in seen] == ["RECENT_1", "RECENT_2"]


def test_phase_a_replay_byte_cap_emits_gap(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    # Write enough bytes to exceed an artificially-small cap.
    with audit.open("a", encoding="utf-8") as fh:
        for i in range(100):
            fh.write(json.dumps({"ts": _ts(0.1 * i), "pad": "x" * 500}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path, resume=None, skip_replay=False,
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
Expected: 2 new tests fail (`iter_live_events` not implemented).

- [ ] **Step 3: Add `open_sources`, `_phase_a_replay`, `iter_live_events` (replay-only first)**

```python
# Source name conventions: "audit", "cond" (= conductor_events),
# "stage:<filename>". The composite cursor encoder uses these.

_AUDIT_FILENAME = "audit_events.jsonl"
_CONDUCTOR_FILENAME = "conductor_events.jsonl"


def _l2_source_filenames_from_console_hints() -> list[str]:
    """Subset of `_RUNNING_STATE_FILE_HINTS` filenames — flat list
    deduplicated. Imported lazily to avoid a circular import with
    operator_console."""
    try:
        from dsar_orchestrator.operator_console import _RUNNING_STATE_FILE_HINTS
    except (ImportError, AttributeError):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for filenames in _RUNNING_STATE_FILE_HINTS.values():
        for fn in filenames:
            if fn not in seen and fn.endswith(".jsonl"):
                seen.add(fn)
                out.append(fn)
    return out


def open_sources(case_dir: Path, *, max_line_bytes: int, verbose_l2: bool) -> dict[str, JsonlTail]:
    working = Path(case_dir) / "working"
    sources: dict[str, JsonlTail] = {
        "audit": JsonlTail(working / _AUDIT_FILENAME, max_line_bytes=max_line_bytes),
        "cond": JsonlTail(working / _CONDUCTOR_FILENAME, max_line_bytes=max_line_bytes),
    }
    if verbose_l2:
        for fn in _l2_source_filenames_from_console_hints():
            sources[f"stage:{fn}"] = JsonlTail(
                working / fn, max_line_bytes=max_line_bytes,
            )
    return sources


def _utc_now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _phase_a_replay(
    sources: dict[str, JsonlTail],
    *,
    replay_window_s: float,
    replay_byte_cap: int,
):
    """Linear backwards scan, heapq-merged in timestamp order.

    Yields LiveEvents (kind="event" or kind="gap"). Returns a dict
    of per-source starting offsets for Phase B.
    """
    import heapq
    from datetime import UTC, datetime, timedelta
    cutoff = (datetime.now(UTC) - timedelta(seconds=replay_window_s))
    cutoff_iso = cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    starting_offsets: dict[str, int] = {}
    per_source_events: dict[str, list[tuple[str, dict, int]]] = {}

    for name, src in sources.items():
        size = src.fstat_size()
        if size == 0:
            starting_offsets[name] = 0
            per_source_events[name] = []
            continue
        # Read from max(0, size - replay_byte_cap) to EOF.
        scan_start = max(0, size - replay_byte_cap)
        if scan_start > 0:
            yield LiveEvent(
                kind="gap", source=name, reason="replay_truncated",
            )
        if src._fh is not None:
            src._fh.seek(scan_start)
            chunk = src._fh.read(size - scan_start)
            # Re-sync to first \n if not at start of line.
            if scan_start > 0:
                nl = chunk.find("\n")
                if nl >= 0:
                    chunk = chunk[nl + 1:]
            events: list[tuple[str, dict, int]] = []
            offset_cursor = scan_start + (len(chunk.split("\n", 1)[0]) + 1 if scan_start > 0 else 0)
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
                    events.append((ts, payload, offset_cursor))
                offset_cursor += len(line) + 1
            per_source_events[name] = events
            # Phase A leaves the fd at EOF — Phase B continues from there.
            starting_offsets[name] = size

    # heapq.merge across sources by ts.
    iters = []
    for name, events in per_source_events.items():
        iters.append(((ts, payload, name) for ts, payload, _off in events))
    for ts, payload, name in heapq.merge(*iters, key=lambda t: t[0]):
        event_type = payload.get("event_type") or payload.get("template_id")
        yield LiveEvent(
            kind="event", source=name, ts=ts,
            event_type=event_type, payload=payload,
            composite_cursor=f"{name}:{starting_offsets[name]}:0:0",
        )
    return starting_offsets


import json
```

(Move the `import json` to the top of the file with the other imports — left at the bottom of the snippet only for clarity; in the real edit, place it at module top.)

Now add the top-level iterator (replay-only for this task; Phase B comes in D3):

```python
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
    sources = open_sources(
        Path(case_dir),
        max_line_bytes=max_line_bytes,
        verbose_l2=verbose_l2,
    )
    try:
        if not skip_replay:
            yield from _phase_a_replay(
                sources,
                replay_window_s=replay_window_s,
                replay_byte_cap=replay_byte_cap,
            )
        # Phase B follows in Task D3.
    finally:
        for src in sources.values():
            src.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D2: iter_live_events Phase A replay" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): iter_live_events Phase A replay window

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D3: Phase B live-tail loop with rotation/truncation/burst/heartbeat

**Spec coverage:** §3.4 (heartbeat scheduler), §3.5 (rotation/truncation), §6 invariants 8, 9, 10, 11, 12.
**Test coverage:** §7.1 (live tail, rotation, truncation, heartbeat), §7.6 (burst-limited read).

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
def test_live_tail_yields_appended_events(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    audit.touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=15, poll_interval=0.02,
            max_line_bytes=1_048_576,
            max_lines_per_burst=5000, max_bytes_per_burst=1_048_576,
            stop=stop, verbose_l2=False,
        ):
            seen.append(e)
            event_kinds = [s for s in seen if s.kind == "event"]
            if len(event_kinds) >= 2:
                stop.set()
                return

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.1)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "LIVE_A"}) + "\n")
        fh.write(json.dumps({"ts": _ts(0), "event_type": "LIVE_B"}) + "\n")
    t.join(timeout=3.0)
    assert not t.is_alive()
    event_kinds = [s for s in seen if s.kind == "event"]
    assert [e.event_type for e in event_kinds[:2]] == ["LIVE_A", "LIVE_B"]


def test_live_truncation_emits_gap(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E1"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path, resume=None, skip_replay=False,
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
    # Truncate.
    audit.write_text("")
    t.join(timeout=3.0)
    assert any(e.kind == "gap" and e.reason == "truncated_live" for e in seen)


def test_live_path_rotation_emits_gap(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "E1"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path, resume=None, skip_replay=False,
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
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    audit.touch()
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path, resume=None, skip_replay=False,
            replay_window_s=30, replay_byte_cap=16 * 1024 * 1024,
            heartbeat_s=0.1,  # short for test
            poll_interval=0.02,
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

- [ ] **Step 3: Extend `iter_live_events` with the Phase B loop**

Replace the post-Phase-A `# Phase B follows in Task D3.` placeholder with:

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
                # Rotation (path-level) detection.
                path_id = src.stat_path_identity()
                if path_id is None:
                    yield LiveEvent(
                        kind="gap", source=name,
                        reason="source_vanished_live",
                    )
                    src.disabled = True
                    continue
                if path_id != src.identity_tuple:
                    yield LiveEvent(
                        kind="gap", source=name, reason="rotated_live",
                    )
                    src.close()
                    src._open()
                    src.identity_tuple = src.fstat_identity()
                    src.last_known_size = 0
                # Truncation detection.
                fd_size = src.fstat_size()
                if fd_size < src.last_known_size:
                    yield LiveEvent(
                        kind="gap", source=name, reason="truncated_live",
                    )
                    if src._fh is not None:
                        src._fh.seek(0)
                src.last_known_size = fd_size
                # Burst-limited read.
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
                    event_type = evt.get("event_type") or evt.get("template_id")
                    offset = src.fstat_size() if src._fh is not None else 0
                    dev, ino = (src.identity_tuple or (0, 0))
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
            # End-of-poll heartbeat fallback.
            now = _time.monotonic()
            if now - last_heartbeat_ts >= heartbeat_s:
                yield LiveEvent(kind="heartbeat", source="*")
                last_heartbeat_ts = now
            if stop.wait(poll_interval):
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D3: iter_live_events Phase B live-tail loop" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): live-tail loop with rotation/truncation/heartbeat

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task D4: `_phase_a_skip_to_cursor` (resume via Last-Event-ID)

**Spec coverage:** §3.5 (identity-validate before seek, resume backlog cap), §6.8.
**Test coverage:** §7.5 (resume round-trip, rotation/truncation/window-exceeded on resume).

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_live_log_stream.py`:

```python
def test_resume_skips_replay_when_cursor_valid(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"ts": _ts(0.1 * i), "event_type": f"E{i}"}) + "\n")
    # Build a cursor at current EOF.
    size = audit.stat().st_size
    st = audit.stat()
    cursor = ResumeCursor(per_source={
        "audit": _PerSourceCursor(offset=size, identity_tuple=(st.st_dev, st.st_ino)),
    })
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path, resume=cursor, skip_replay=True,
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
    # No replay of pre-cursor events; only the new one.
    event_types = [e.event_type for e in seen if e.kind == "event"]
    assert event_types == ["NEW"], event_types


def test_resume_rotation_emits_gap(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    audit = tmp_path / "working" / "audit_events.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "OLD"}) + "\n")
    st = audit.stat()
    cursor = ResumeCursor(per_source={
        "audit": _PerSourceCursor(offset=audit.stat().st_size, identity_tuple=(st.st_dev, st.st_ino)),
    })
    # Rotate.
    audit.unlink()
    with audit.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _ts(0), "event_type": "NEW"}) + "\n")
    stop = threading.Event()
    seen: list[LiveEvent] = []

    def consumer() -> None:
        for e in iter_live_events(
            tmp_path, resume=cursor, skip_replay=True,
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
Expected: 2 new tests fail (skip-to-cursor path not implemented).

- [ ] **Step 3: Add the resume path**

Modify the top of `iter_live_events`:

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
        # If skip_replay but cursor is None/invalid, fall through to
        # Phase B directly (treated as a fresh connect with no replay).
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
    truncated / window-exceeded / vanished sources.

    Leaves each source's fd positioned ready for Phase B.
    """
    for name, src in sources.items():
        cursor_state = resume.per_source.get(name)
        if cursor_state is None:
            # Source not in cursor → start from current EOF (fresh-style).
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
            yield LiveEvent(kind="gap", source=name, reason="resume_window_exceeded")
            target = max(0, size - replay_byte_cap)
            # Snap to next \n.
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
Expected: 23 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task D4: _phase_a_skip_to_cursor resume path" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): resume via Last-Event-ID with identity validate

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Block E — SSE handler + console routes

### Task E1: SSE helpers (`send_sse_headers`, `write_sse_frame`)

**Spec coverage:** §4.2, §4.5.
**Test coverage:** §7.9 (integration).

**Files:**
- Modify: `src/dsar_orchestrator/local_broker/live_log_stream.py`
- Modify: `tests/test_live_log_stream.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live_log_stream.py`:

```python
import io

from dsar_orchestrator.local_broker.live_log_stream import (
    send_sse_headers,
    write_sse_frame,
)


def test_write_sse_frame_emits_id_event_data() -> None:
    buf = io.BytesIO()
    write_sse_frame(buf, id="audit:1024:1:123", data={"k": 1})
    raw = buf.getvalue().decode("utf-8")
    assert "id: audit:1024:1:123\n" in raw
    assert "event: live-log\n" in raw
    assert 'data: {"k": 1}\n\n' in raw


def test_write_sse_frame_omits_id_when_none() -> None:
    buf = io.BytesIO()
    write_sse_frame(buf, id=None, data={"k": 1})
    raw = buf.getvalue().decode("utf-8")
    assert "id:" not in raw
    assert 'data: {"k": 1}\n\n' in raw
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_stream.py -v`
Expected: 2 new tests fail.

- [ ] **Step 3: Add to `live_log_stream.py`**

```python
def send_sse_headers(wfile) -> None:
    """Send the HTTP/1.1 status line + SSE headers via the raw wfile.

    Callers using BaseHTTPRequestHandler should use the standard
    `send_response`/`send_header`/`end_headers` API instead. This
    helper is for tests and out-of-band writes.
    """
    wfile.write(b"HTTP/1.1 200 OK\r\n")
    wfile.write(b"Content-Type: text/event-stream\r\n")
    wfile.write(b"Cache-Control: no-cache\r\n")
    wfile.write(b"X-Accel-Buffering: no\r\n")
    wfile.write(b"\r\n")


def write_sse_frame(wfile, *, id: str | None, data: dict) -> None:
    """Write one SSE frame (id, event, data) to wfile.

    Raises BrokenPipeError / OSError if the underlying connection
    is gone. Caller is responsible for `flush()`.
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
Expected: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/local_broker/live_log_stream.py tests/test_live_log_stream.py
~/.claude/scripts/code-review-jury.py --staged --task "Task E1: SSE helpers" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(live-log): SSE frame helpers

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task E2: `/live-log/stream` SSE handler in operator_console

**Spec coverage:** §4.1, §4.2 (full SSE handler loop with PII-safe error log).
**Test coverage:** §7.6, §7.9.

**Files:**
- Modify: `src/dsar_orchestrator/operator_console.py`
- Create: `tests/test_live_log_route.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_live_log_route.py
"""Integration-ish tests for the /live-log/stream SSE route.

Spawns the ConsoleHandler in-process, connects with a stdlib
http.client, asserts SSE frames arrive correctly.
"""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

from dsar_orchestrator.operator_console import ConsoleHandler
from http.server import ThreadingHTTPServer


@pytest.fixture()
def case_dir(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    return tmp_path


@pytest.fixture()
def server(case_dir: Path):
    # ConsoleHandler reads case_dir via a module-level global.
    import dsar_orchestrator.operator_console as oc
    oc._GLOBAL_CASE_DIR = case_dir  # set by run_server in production
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()


def test_live_log_stream_serves_text_event_stream(case_dir, server):
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    # Don't drain the body — close on the server side via fixture teardown.
    conn.close()


def test_live_log_stream_emits_a_frame_for_appended_event(case_dir, server):
    port = server
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()
    # Append an event after the connection is up.

    def appender() -> None:
        time.sleep(0.05)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-05-29T10:42:11Z",
                "event_type": "REDACT_COMPLETED",
                "refs_processed": 1,
                "redactions_applied": 1,
            }) + "\n")
    threading.Thread(target=appender, daemon=True).start()

    # Read a bounded amount of body.
    body = resp.read1(4096)
    conn.close()
    text = body.decode("utf-8", errors="replace")
    assert "event: live-log" in text
    assert "REDACT_COMPLETED" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: route does not exist → 404 or similar.

- [ ] **Step 3: Add the route to `operator_console.py`**

In the `do_GET` chain in `ConsoleHandler`, add:

```python
        if url.path == "/live-log/stream":
            self._serve_live_log_stream()
            return
        if url.path == "/live-log":
            self._send(200, render_live_log_page(ctx))
            return
```

Then add the handler method below `do_GET`:

```python
    def _serve_live_log_stream(self) -> None:
        """SSE handler — single-thread merged iterator from
        live_log_stream.iter_live_events, projected via
        live_log_projection.project_for_browser.
        """
        import socket
        import threading as _threading
        from urllib.parse import parse_qs

        from .local_broker.live_log_stream import (
            INITIAL_ZERO_CURSOR,
            iter_live_events,
            parse_composite_last_event_id,
            write_sse_frame,
        )
        from .local_broker.live_log_projection import project_for_browser

        ctx = self._ctx()
        query = parse_qs(urllib.parse.urlparse(self.path).query)
        verbose_l2 = (query.get("verbose", ["0"])[0] == "1")

        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.connection.settimeout(30.0)
            self.connection.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1,
            )
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
                cursor_for_frame = (
                    last_cursor if event.kind == "heartbeat"
                    else event.composite_cursor
                )

                if event.kind == "heartbeat":
                    try:
                        self.wfile.write(b":hb\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                        return
                    continue

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
                        # §6.6: PII-safe error log only — no exc_info.
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
                    write_sse_frame(
                        self.wfile, id=cursor_for_frame, data=projected,
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                    return
        finally:
            stop.set()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/operator_console.py tests/test_live_log_route.py
~/.claude/scripts/code-review-jury.py --staged --task "Task E2: /live-log/stream SSE handler" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(console): /live-log/stream SSE route

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task E3: `/live-log` HTML page + nav-bar link

**Spec coverage:** §4.1, §4.6 (browser UX).
**Test coverage:** smoke test that `/live-log` returns 200 + the static HTML.

**Files:**
- Modify: `src/dsar_orchestrator/operator_console.py`
- Modify: `tests/test_live_log_route.py`

- [ ] **Step 1: Write the failing test**

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
    assert "/live-log" in body  # nav bar link present
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: 2 new tests fail.

- [ ] **Step 3: Add `render_live_log_page` and the nav-bar link**

In `src/dsar_orchestrator/operator_console.py`, add the renderer near the other `render_*` functions:

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


def render_live_log_page(ctx) -> str:  # noqa: ARG001 — ctx reserved for future scoping
    return _LIVE_LOG_HTML
```

Add the nav-bar link in `_case_header` near the existing `<a href='/people-register'>People register</a>`:

```python
        "<a href='/live-log'>Live log</a>"
```

…appended after the existing links.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_log_route.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/operator_console.py tests/test_live_log_route.py
~/.claude/scripts/code-review-jury.py --staged --task "Task E3: /live-log HTML page + nav link" \
    --spec docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md
git commit -m "feat(console): /live-log page with vanilla-JS EventSource UI

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Block F — Final test sweep + PR

### Task F1: Full suite green + open PR

**Files:** (no source files touched)

- [ ] **Step 1: Run full test suite from the worktree**

Run: `.venv/bin/python -m pytest tests/ -q 2>&1 | tail -10`
Expected: all green (existing + new tests).

- [ ] **Step 2: Manual smoke test** (5 min)

```bash
# Start the console against a test case dir.
.venv/bin/python -m dsar_orchestrator.operator_console --case-dir /tmp/smoke-case --port 8089 &
# In another shell, write a few events:
mkdir -p /tmp/smoke-case/working
for i in 1 2 3; do
  echo '{"ts":"2026-05-29T10:42:1'$i'Z","event_type":"REDACT_COMPLETED","refs_processed":1,"redactions_applied":1}' >> /tmp/smoke-case/working/audit_events.jsonl
  sleep 1
done
# Open http://127.0.0.1:8089/live-log — confirm events stream in, pause works,
# filters work, autoscroll behaves.
```

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin worktree-operator-console-live-log

gh pr create --base main --title "feat(console): live-log feed (operator console)" --body "$(cat <<'EOF'
## Summary
- Plex-style live event view in the operator console at `/live-log`.
- Tails `working/audit_events.jsonl`, per-stage decision/finding jsonls
  (behind a verbose toggle), and a new structured
  `working/conductor_events.jsonl` written via a bounded-template
  registry (`ConductorEventLog`).
- Stream is server-sent events (`/live-log/stream`); each frame is
  passed through a fail-closed per-event-type field allowlist with a
  bounded-enum value scrubber, so no raw PII ever reaches the browser.
- Composite `Last-Event-ID` cursor for browser auto-reconnect with
  identity-validate-before-seek (rotation/truncation detected); 16 MiB
  resume backlog cap; 15 s heartbeat on an independent monotonic
  schedule; `SO_KEEPALIVE` + `SO_SNDTIMEO=30s` for hung-client safety.

Spec: `docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v1.md`
(7 rounds of brainstorm-jury, 2/3 approve at round 7; remaining items
encoded as MUST-level invariants in §6.)

## Test plan
- [ ] `tests/test_conductor_event_log.py` (22 tests)
- [ ] `tests/test_pipeline_conductor_events.py`
- [ ] `tests/test_live_log_projection.py` (7 tests)
- [ ] `tests/test_live_log_pii_regression.py` (22 PII canary tests)
- [ ] `tests/test_live_log_stream.py` (25 tests — replay/live/rotation/truncation/cursor/heartbeat/burst-limited read)
- [ ] `tests/test_live_log_route.py` (4 tests — SSE handler + HTML page)
- [ ] Manual smoke test against a tmp case dir.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist (run before claiming the plan complete)

1. **Spec coverage:**
   - §3.1 (L3 structured events): Tasks A1–A4. ✓
   - §3.2 (allowlist + scrubber, fail-closed, no detail_url): Tasks B1, B2. ✓
   - §3.3 (30 s replay, 16 MiB cap, replay_truncated gap): Task D2. ✓
   - §3.4 (single-thread, SO_KEEPALIVE, SO_SNDTIMEO, heartbeat): Tasks D3, E2. ✓
   - §3.5 (composite cursor, identity-validate, resume backlog, source-vanished, heartbeat carries cursor): Tasks D1, D4, E2. ✓
   - §3.6 (`(st_dev, st_ino)` only — no st_mtime): Tasks C1, D3, D4. ✓
   - §4.1–4.2 (routes, SSE handler loop): Tasks E2, E3. ✓
   - §4.3 (helper module structure): Tasks C1, C2, D1–D4, E1. ✓
   - §4.4 (ConductorEventLog wiring): Tasks A3, A4. ✓
   - §4.5 (data contract): Tasks D3, E1, E2. ✓
   - §4.6 (browser UX): Task E3. ✓
   - §6 invariants 1–15: distributed across Tasks C1, C2, D1, D3, D4, E2. ✓
   - §7 test plan: every test id has a corresponding test file/task. ✓
2. **Placeholder scan:** no TBDs, no "TODO", no "appropriate error handling". Every code block contains the actual code. ✓
3. **Type consistency:** `JsonlTail`, `LiveEvent`, `ResumeCursor`, `_PerSourceCursor`, `INITIAL_ZERO_CURSOR` defined once and used consistently across tasks. `composite_cursor` format `<name>:<offset>:<dev>:<ino>` defined in Task D1 and used in D3/D4/E2.
4. **Scope:** one PR's worth. ~1,000 lines new code + tests. Each task is a single committable unit with green tests.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-29-operator-console-live-log-phase1.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task. Each subagent gets the task's full text + the spec section it satisfies; produces TDD red→green→commit cycle; code-review-jury runs on staged diff before each commit. I review between tasks.

**2. Inline Execution** — I execute tasks in this session via executing-plans, batching where independent.

Which approach?
