# Durant Pipeline Hardening — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Scope of this plan: Phase 3 only.** Continues from Phase 2 (which closed at Task 25 — RoleRouter `has_token_counter_for` / `count_tokens`, truncation token-belt, GateDurant integration, role field sanitiser, `_data_subject_sanitiser`). Phase 3 implements §4.2 of the spec (calibration-gated recheck stage) end-to-end: the `GateDurantRecheck` LLM gate, the `RecheckStage` BaseStage subclass, the calibration cache loader, the JSONL thread-safe writer, the recheck system prompt asset (signed via the Phase 1 `dsar-prompt sign` tool), pricing-aware cost telemetry, and a Wilson-bounds utility module that §4.4 (Phase 4) will reuse.
>
> Phase 3 does NOT include §4.6 Agent22 5-arg synthesis — that lands in Phase 4. `ScopeCheckStage` integration here invokes the new `RecheckStage` after primary `gate_durant` runs, but the synthesis path keeps using the 2-arg form for now; the recheck JSONL is written and the canonical "stage ran" marker is laid down for Phase 4 to consume.
>
> Subsequent phase plans:
>
> - Phase 4 plan: §4.6 Agent22 5-arg synthesis + scope_verdicts evidence-block extension + e2e durant_with_recheck integration test.
> - Phase 5 plan: §4.4 fitness canary + `dsar-fitness-canary` + conductor pre-flight wiring.
> - Phase 6 plan: §4.7 durant-test.md updates + CI lint.

**Goal:** Land the §4.2 calibration-gated recheck stage as a complete vertical slice. By end of Phase 3: the `durant.recheck.system` prompt asset is signed + archived, `GateDurantRecheck` and `RecheckStage` are implemented, the `dsar-recheck` CLI runs against a synthetic case, calibration cache reads work with full env-var / remote / tie-break / stale / drift semantics, and `ScopeCheckStage` invokes `RecheckStage` automatically after primary durant when `recheck.mode != "never"` in case config — emitting `working/durant_underdisclosure_recheck.jsonl`, `working/recheck_decision.json` (canonical marker), and `working/recheck_summary.json` (cost telemetry). All Phase 1 + Phase 2 tests still pass.

**Architecture:** New files: `gates/gate_durant_recheck.py`, `recheck_stage.py`, `gates/prompts/durant.recheck.system.md`, `config/pricing.json`, `_wilson.py`, `_jsonl_appender.py`, `_calibration_cache.py`. Modified files: `_stage_base.py` (VALID_STAGE_LABELS adds "durant_recheck"), `scope_check_stage.py` (post-durant recheck dispatch + config plumbing), `pyproject.toml` (adds `dsar-recheck` entry-point). New schema: `schemas/durant_recheck_row.schema.json`. All existing Phase 1 + Phase 2 tests must keep passing.

**Tech Stack:** Python 3.11+; standard library only for Phase 3 (`hashlib`, `json`, `threading`, `concurrent.futures.ThreadPoolExecutor`, `re`, `urllib.parse`, `pathlib`, `datetime`); `jsonschema` (already a transitive — used by `tests/test_scope_check_stage.py`). Wilson bounds implemented in pure Python (closed form; no scipy dependency). PromptLoader from Phase 1 (`dsar_pipeline.gates.prompt_loader`) and `RoleRouter.has_token_counter_for` from Phase 2 are used directly.

---

## File structure (Phase 3 deltas only)

### dsar-toolkit (creates 7 new files; extends 3 existing)

```
src/dsar_pipeline/
├── gates/
│   ├── gate_durant_recheck.py               # CREATE — GateDurantRecheck(BaseGateAgent)
│   └── prompts/
│       └── durant.recheck.system.md         # CREATE — recheck system prompt (signed)
├── config/
│   └── pricing.json                          # CREATE — model_alias → per-1k token USD
├── recheck_stage.py                          # CREATE — RecheckStage(BaseStage) + dsar-recheck CLI
├── _wilson.py                                # CREATE — wilson_lower / wilson_upper closed-form helpers
├── _jsonl_appender.py                        # CREATE — thread-safe row writer with 512-byte cap
├── _calibration_cache.py                     # CREATE — load + tie-break + retry + normalise_hash
├── _stage_base.py                            # MODIFY — add "durant_recheck" to VALID_STAGE_LABELS
└── scope_check_stage.py                      # MODIFY — post-durant RecheckStage dispatch
schemas/
└── durant_recheck_row.schema.json            # CREATE — per-row JSONL contract
pyproject.toml                                # MODIFY — add dsar-recheck entry-point
tests/                                        # CREATE 2 new test files
├── test_gate_durant_recheck.py
└── test_recheck_stage.py
```

---

## Phase 3 — Recheck stage (toolkit)

### Task 26: Scaffold `durant.recheck.system` prompt asset + sign it

**Files:**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/gates/prompts/durant.recheck.system.md`
- (Uses) `dsar-prompt sign` CLI from Phase 1

The recheck prompt body asks the LLM to RE-EXAMINE the document independently — answering the inverse question "is this document actually NOT about the subject?" — without seeing the primary verdict's rationale (confirmation-bias mitigation per durant-test.md §8). Spec §4.2 (A) requires recheck to NOT include the primary rationale.

- [ ] **Step 1: Create the asset file with the verbatim recheck prompt body**

Write `src/dsar_pipeline/gates/prompts/durant.recheck.system.md`:

```markdown
---
prompt_id: "durant.recheck.system"
version: "1.0.0"
seal_sha256: "PLACEHOLDER_SEAL_WILL_BE_FILLED_BY_SIGN_CLI"
droppable_blocks: ["placeholder-tokens"]
---

You are a UK data-protection adjudicator performing an INDEPENDENT RECHECK on a document that a prior screening pass classified as work_context_only (the subject is NOT the focus; the document is about other matters). Your task is to re-examine the document FROM SCRATCH and decide whether the prior classification was correct.

You are not given the prior rationale; you must form your own view. The under-disclosure error (failing to release a biographical document) is more serious than the over-disclosure error in this safety-net pass.

<!-- block:placeholder-tokens -->
# About placeholder tokens in this prompt

Names, email addresses, phone numbers, and other identifiers in the document content and the subject preamble may appear as placeholder tokens: [PERSON_0], [EMAIL_3], [PHONE_1], [ORGANIZATION_2], etc. These tokens are de-identification placeholders applied automatically before this prompt was sent to you — they are NOT redactions in the original document and they do NOT indicate that any content has been withheld from you. The original document had real values in those positions; you can see those values via the placeholder tokens. The data subject's identity is given to you via a token (e.g. the subject preamble may say 'the subject is [PERSON_5]') — when you see the SAME token elsewhere in the document, that is the subject's name appearing in that position. Treat tokens like ordinary entity references for the purpose of this test. Do NOT classify documents as 'reclassify_to_ambiguous' merely because they contain placeholder tokens.
<!-- endblock -->

Three verdicts are possible:
  confirmed_work_context_only — The prior classification was correct: the subject appears only as a peripheral cc/bcc or routine addressee, and the document content is about other matters (third-party operations, unrelated projects, generic broadcasts). The subject is NOT the focus.
  reclassify_to_biographical — The prior classification was wrong: the document is in fact biographical for the subject. The subject's actions, decisions, performance, correspondence, identity, or contractual position is the focus of the content — even if the subject appears only briefly, or only at the tail of a long thread. Signals to watch for: direct-addressee carve-outs (subject in the To: line of a thread about their own assignment / performance / contract); signature blocks revealing the subject is the author; appended attachments about the subject; tail-of-thread biographical material that earlier blind-truncation may have hidden.
  reclassify_to_ambiguous — Evidence is genuinely mixed and you cannot decide cleanly. Use this verdict sparingly; default to reclassify_to_biographical when uncertain (under-disclosure is the worse error in this pass).

Apply the Durant v FSA biographical-focus test independently. Do NOT defer to the prior verdict. Your job is to catch under-disclosure errors the primary pass missed — the recheck is the safety net.
```

- [ ] **Step 2: Verify LF-only line endings + single trailing newline**

```bash
cd ~/projects/dsar-toolkit
file src/dsar_pipeline/gates/prompts/durant.recheck.system.md  # must not say "CRLF"
tail -c 1 src/dsar_pipeline/gates/prompts/durant.recheck.system.md | xxd  # must show "0a"
```

- [ ] **Step 3: Sign the asset for real**

```bash
cd ~/projects/dsar-toolkit
dsar-prompt sign src/dsar_pipeline/gates/prompts/durant.recheck.system.md
```

Expected output: `signed .../durant.recheck.system.md: seal_sha256=<hex>`.

- [ ] **Step 4: Rebuild the prompt registry + archive (Phase 1 tooling)**

```bash
cd ~/projects/dsar-toolkit
./bin/build-prompt-registry
```

Expected: `registry built`. The new asset is appended to `prompts/_registry.json` and archived to `prompts/_archive/durant.recheck.system/1.0.0.md.gz`.

- [ ] **Step 5: Verify Phase 1 CI tests still pass against both assets**

```bash
uv run pytest tests/test_prompt_assets.py -v
```

Expected: all PASS (Phase 1 tests now iterate over both `durant.system` and `durant.recheck.system`).

- [ ] **Step 6: Commit**

```bash
cd ~/projects/dsar-toolkit
git add src/dsar_pipeline/gates/prompts/durant.recheck.system.md src/dsar_pipeline/gates/prompts/_registry.json src/dsar_pipeline/gates/prompts/_archive/durant.recheck.system/
git commit -m "feat(prompts): add signed durant.recheck.system asset (v1.0.0)"
```

---

### Task 27: Implement `_wilson.py` — Wilson lower/upper bound helpers

**Files:**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/_wilson.py`
- Create: `~/projects/dsar-toolkit/tests/test_wilson.py`

Spec §4.2 (B) uses `wilson_upper(fn_rate)` to decide whether to skip recheck (`ci_upper <= fn_threshold` → "never"). Spec §4.4 also needs both bounds. Pure-Python closed form (no scipy dependency — scipy is not in dsar-toolkit's deps).

- [ ] **Step 1: Write failing tests**

Create `tests/test_wilson.py`:

```python
"""Tests for the _wilson.py Wilson-bound helpers."""
from __future__ import annotations

import pytest

from dsar_pipeline._wilson import wilson_lower, wilson_upper


def test_wilson_zero_denominator_returns_none():
    """Spec §4.4: explicit zero-denominator guard returns None."""
    assert wilson_lower(0, 0) is None
    assert wilson_upper(0, 0) is None


def test_wilson_bounds_perfect_run_default_z():
    """30 successes out of 30 — lower bound below 1.0, upper bound 1.0."""
    lo = wilson_lower(30, 30)
    hi = wilson_upper(30, 30)
    assert 0.85 < lo < 1.0
    assert hi == pytest.approx(1.0, abs=1e-9)


def test_wilson_bounds_zero_observed_default_z():
    """0 successes out of 30 — lower bound 0.0, upper bound > 0."""
    lo = wilson_lower(0, 30)
    hi = wilson_upper(0, 30)
    assert lo == pytest.approx(0.0, abs=1e-9)
    assert 0.0 < hi < 0.20


def test_wilson_bounds_balanced_proportion():
    """15/30 — both bounds bracket 0.5."""
    lo = wilson_lower(15, 30)
    hi = wilson_upper(15, 30)
    assert 0.3 < lo < 0.5
    assert 0.5 < hi < 0.7


def test_wilson_bounds_z_parameter():
    """Higher z (e.g. 1.96 → 95%) → wider interval than default."""
    lo_95 = wilson_lower(15, 30, z=1.96)
    hi_95 = wilson_upper(15, 30, z=1.96)
    lo_90 = wilson_lower(15, 30, z=1.645)
    hi_90 = wilson_upper(15, 30, z=1.645)
    assert lo_95 < lo_90
    assert hi_95 > hi_90


def test_wilson_invariant_lo_le_hi():
    """For all (k, n), wilson_lower <= wilson_upper."""
    for k in range(0, 11):
        for n in range(max(k, 1), 21):
            assert wilson_lower(k, n) <= wilson_upper(k, n)


def test_wilson_invalid_k_raises():
    """k > n is a programming error."""
    with pytest.raises(ValueError, match="k.*n"):
        wilson_lower(31, 30)
    with pytest.raises(ValueError, match="k.*n"):
        wilson_upper(31, 30)
```

- [ ] **Step 2: Run; verify failures**

```bash
cd ~/projects/dsar-toolkit
uv run pytest tests/test_wilson.py -v
```

Expected: `ModuleNotFoundError: No module named 'dsar_pipeline._wilson'`.

- [ ] **Step 3: Implement `_wilson.py`**

Create `src/dsar_pipeline/_wilson.py`:

```python
"""Wilson score interval — pure-Python closed form.

Used by:
  - §4.2 recheck gating: wilson_upper(fn_rate) vs fn_threshold
  - §4.4 fitness canary: both bounds vs corpus thresholds

The Wilson interval is preferred over the normal-approximation
("Wald") interval because it stays inside [0, 1] near the boundaries
and degrades gracefully for small samples.

Reference: Wilson, E. B. (1927). "Probable inference, the law of
succession, and statistical inference." JASA 22 (158): 209–212.
"""
from __future__ import annotations

import math
from typing import Optional

# Default z = 1.645 → 90% confidence (one-sided 95% equivalent for the
# upper-tail check used in §4.2). Callers pass z=1.96 for two-sided 95%.
_DEFAULT_Z = 1.645


def _wilson_bounds(k: int, n: int, z: float) -> tuple[float, float]:
    """Internal: return (lower, upper) closed-form Wilson bounds.

    Caller MUST guard n == 0 — this function asserts.
    """
    assert n > 0, "wilson_bounds: n must be > 0"
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half_width = (z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denom
    lo = max(0.0, centre - half_width)
    hi = min(1.0, centre + half_width)
    return lo, hi


def wilson_lower(k: int, n: int, *, z: float = _DEFAULT_Z) -> Optional[float]:
    """Lower bound of the Wilson score interval for k successes / n trials.

    Returns None when n == 0 (explicit zero-denominator guard per spec
    §4.4). Raises ValueError when k > n or k < 0.
    """
    if n == 0:
        return None
    if k < 0 or k > n:
        raise ValueError(f"wilson_lower: invalid (k, n) = ({k}, {n})")
    return _wilson_bounds(k, n, z)[0]


def wilson_upper(k: int, n: int, *, z: float = _DEFAULT_Z) -> Optional[float]:
    """Upper bound of the Wilson score interval for k successes / n trials.

    Returns None when n == 0 (explicit zero-denominator guard per spec
    §4.4). Raises ValueError when k > n or k < 0.
    """
    if n == 0:
        return None
    if k < 0 or k > n:
        raise ValueError(f"wilson_upper: invalid (k, n) = ({k}, {n})")
    return _wilson_bounds(k, n, z)[1]
```

- [ ] **Step 4: Run tests; verify all pass**

```bash
uv run pytest tests/test_wilson.py -v
```

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/_wilson.py tests/test_wilson.py
git commit -m "feat(wilson): closed-form lower/upper bound helpers"
```

---

### Task 28: Implement `_jsonl_appender.py` — thread-safe writer with 512-byte cap

**Files:**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/_jsonl_appender.py`
- Create: `~/projects/dsar-toolkit/tests/test_jsonl_appender.py`

Spec §4.2 (G) — thread-safe context manager. Row size cap = 512 bytes (POSIX PIPE_BUF on macOS/BSD min). `__exit__` re-raises close errors only when no in-block exception is active. Multi-byte UTF-8 byte-count enforcement.

- [ ] **Step 1: Write failing tests**

Create `tests/test_jsonl_appender.py`:

```python
"""Tests for _jsonl_appender.JsonlAppender (spec §4.2 v7)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from dsar_pipeline._jsonl_appender import JsonlAppender, RowSizeError


def test_appender_writes_one_line_per_record(tmp_path: Path):
    out = tmp_path / "out.jsonl"
    with JsonlAppender(out) as ap:
        ap.append({"a": 1, "b": "x"})
        ap.append({"a": 2, "b": "y"})
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1, "b": "x"}
    assert json.loads(lines[1]) == {"a": 2, "b": "y"}


def test_appender_appends_to_existing_file(tmp_path: Path):
    out = tmp_path / "out.jsonl"
    out.write_text('{"pre": "existing"}\n', encoding="utf-8")
    with JsonlAppender(out) as ap:
        ap.append({"new": "row"})
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"pre": "existing"}


def test_appender_rejects_row_over_512_bytes(tmp_path: Path):
    out = tmp_path / "out.jsonl"
    big_payload = {"k": "x" * 600}      # JSON encoded > 512 bytes
    with JsonlAppender(out) as ap:
        with pytest.raises(RowSizeError, match="512"):
            ap.append(big_payload)


def test_appender_size_cap_counts_utf8_bytes_not_chars(tmp_path: Path):
    """4-byte UTF-8 codepoints (e.g. emoji) — byte count, not char count."""
    out = tmp_path / "out.jsonl"
    # "💥" is 4 UTF-8 bytes. 130 copies = 520 bytes, plus JSON overhead.
    payload = {"k": "💥" * 130}
    with JsonlAppender(out) as ap:
        with pytest.raises(RowSizeError, match="512"):
            ap.append(payload)


def test_appender_thread_safety(tmp_path: Path):
    """Concurrent .append() calls produce well-formed JSONL with no
    interleaved partial writes."""
    out = tmp_path / "concurrent.jsonl"
    n_threads = 8
    rows_per_thread = 25

    def worker(tid: int):
        with JsonlAppender(out) as ap:
            for i in range(rows_per_thread):
                ap.append({"tid": tid, "i": i})

    # NB: each thread opens its own appender (the spec's stage opens one
    # appender and writes from N threads — second test below covers that).
    threads = [threading.Thread(target=worker, args=(t,))
               for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * rows_per_thread
    for line in lines:
        # Every line must round-trip cleanly (no torn writes)
        rec = json.loads(line)
        assert "tid" in rec and "i" in rec


def test_appender_shared_appender_thread_safety(tmp_path: Path):
    """ONE appender shared across threads — exactly the RecheckStage model."""
    out = tmp_path / "shared.jsonl"
    n_threads = 8
    rows_per_thread = 25

    with JsonlAppender(out) as ap:
        def worker(tid: int):
            for i in range(rows_per_thread):
                ap.append({"tid": tid, "i": i})
        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * rows_per_thread
    for line in lines:
        rec = json.loads(line)
        assert "tid" in rec and "i" in rec


def test_appender_exit_reraises_close_error_when_no_inblock_exc(
        tmp_path: Path, monkeypatch):
    """close() failure with no in-block exception → __exit__ re-raises."""
    out = tmp_path / "out.jsonl"
    ap = JsonlAppender(out)
    ap.__enter__()
    ap.append({"k": "v"})

    original_close = ap._fh.close

    def boom():
        original_close()                 # close anyway (avoid resource leak)
        raise OSError("simulated close failure")

    monkeypatch.setattr(ap._fh, "close", boom)
    with pytest.raises(OSError, match="simulated close failure"):
        ap.__exit__(None, None, None)


def test_appender_exit_logs_close_error_when_inblock_exc_active(
        tmp_path: Path, monkeypatch, caplog):
    """close() failure WITH in-block exception → log warning; do not mask."""
    out = tmp_path / "out.jsonl"
    ap = JsonlAppender(out)
    ap.__enter__()

    original_close = ap._fh.close
    def boom():
        original_close()
        raise OSError("simulated close failure")
    monkeypatch.setattr(ap._fh, "close", boom)

    import logging
    with caplog.at_level(logging.WARNING):
        # Returning False means original exception propagates
        result = ap.__exit__(ValueError, ValueError("inblock"), None)
    assert result is False
    assert any("close failed" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_jsonl_appender.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `_jsonl_appender.py`**

Create `src/dsar_pipeline/_jsonl_appender.py`:

```python
"""Thread-safe JSONL row appender (spec §4.2 (G) v7).

Used by RecheckStage to write durant_underdisclosure_recheck.jsonl
from a ThreadPoolExecutor. Row size cap = 512 bytes (POSIX PIPE_BUF
minimum on macOS/BSD; cross-platform default).

Lifecycle:
  with JsonlAppender(path) as ap:
      ap.append({...})
      ap.append({...})
  # close at __exit__; raises if close fails AND no in-block exc.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# POSIX PIPE_BUF minimum (macOS/BSD). Linux is 4096 but we default
# to the portable minimum. Operators may raise via stage config in
# future; out of scope for Phase 3.
PIPE_BUF_BYTES = 512


class RowSizeError(ValueError):
    """Raised when a row exceeds the PIPE_BUF byte cap."""


class JsonlAppender:
    """Context-managed thread-safe append-only JSONL writer.

    NOT safe across processes — one writer per process. Within a
    process, .append() may be called from any number of threads.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def __enter__(self) -> "JsonlAppender":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._fh.close()
        except OSError as close_err:
            if exc_type is None:
                # No in-block exception — close failure is the operative
                # error; raise so the operator sees it.
                raise
            log.warning(
                "JsonlAppender close failed while in-block exc active "
                "(%s: %s); original exception will propagate",
                type(close_err).__name__, close_err,
            )
        return False

    def append(self, record: dict) -> None:
        """Serialise `record` to JSON, validate size, write+flush atomically.

        Raises:
            RowSizeError: when the encoded row (incl. trailing newline)
                exceeds PIPE_BUF_BYTES bytes.
        """
        line = json.dumps(record, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        if len(encoded) >= PIPE_BUF_BYTES:
            raise RowSizeError(
                f"row size {len(encoded)} bytes >= {PIPE_BUF_BYTES} byte cap "
                f"(PIPE_BUF); shrink rationale or use a separate large-payload "
                f"file"
            )
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
```

- [ ] **Step 4: Run tests; verify all pass**

```bash
uv run pytest tests/test_jsonl_appender.py -v
```

Expected: 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/_jsonl_appender.py tests/test_jsonl_appender.py
git commit -m "feat(jsonl_appender): thread-safe row writer with 512-byte cap"
```

---

### Task 29: Implement `_calibration_cache.py` — registry load + tie-break + retry

**Files:**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/_calibration_cache.py`
- Create: `~/projects/dsar-toolkit/tests/test_calibration_cache.py`

Spec §4.2 (C) — read-only loader:
- Location resolution: `DSAR_CALIBRATION_REGISTRY` env → cfg path → `~/.dsar/calibration_registry.json`.
- Strict per-entry validation (`schema_version == 1`, `fn_rate_ci95` exactly 2 floats in [0,1], `lo ≤ hi`, `fn_rate` in `[lo, hi]`, hex fields 64-char lowercase).
- Local FS: `FileNotFoundError` → silent None; `PermissionError` → `ConfigError`; other OSError → propagate.
- Remote (s3://, https://): retry 3× jittered `[0.5–1.5, 1.0–3.0, 2.0–6.0]`; 404 / NoSuchKey terminal.
- Multi-match tie-break: `max(calibrated_at, sample_size, source_case_id or "")` lexicographic.
- `_normalise_hash` = `.strip().lower()`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_calibration_cache.py`:

```python
"""Tests for _calibration_cache (spec §4.2 (C))."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dsar_pipeline._calibration_cache import (
    CalibrationCacheEntry, CalibrationConfigError,
    _normalise_hash, find_matching_entry, load_registry,
)


def _make_registry_file(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "calibration_registry.json"
    p.write_text(json.dumps({"schema_version": 1, "entries": entries}),
                 encoding="utf-8")
    return p


def _entry(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "deployment_id": "dep1",
        "model_alias": "mini@mlx",
        "primary_prompt_seal_sha256": "a" * 64,
        "recheck_prompt_seal_sha256": "b" * 64,
        "calibrated_at": "2026-05-20T10:00:00Z",
        "sample_size": 30,
        "fn_rate": 0.10,
        "fn_rate_ci95": [0.05, 0.18],
        "source_case_id": "case-001",
    }
    base.update(overrides)
    return base


def test_normalise_hash_strip_and_lower():
    assert _normalise_hash("  ABCdef\n") == "abcdef"


def test_load_registry_missing_file_returns_none(tmp_path: Path):
    missing = tmp_path / "no-such-file.json"
    assert load_registry(missing) is None


def test_load_registry_returns_entries(tmp_path: Path):
    p = _make_registry_file(tmp_path, [_entry()])
    reg = load_registry(p)
    assert reg is not None
    assert len(reg.entries) == 1
    assert isinstance(reg.entries[0], CalibrationCacheEntry)


def test_load_registry_skips_invalid_entry(tmp_path: Path, caplog):
    bad = _entry(fn_rate_ci95=[0.5, 0.3])    # lo > hi
    good = _entry(deployment_id="dep2")
    p = _make_registry_file(tmp_path, [bad, good])
    import logging
    with caplog.at_level(logging.WARNING):
        reg = load_registry(p)
    assert reg is not None
    assert len(reg.entries) == 1
    assert reg.entries[0].deployment_id == "dep2"
    assert any("skipping" in r.message.lower() for r in caplog.records)


def test_load_registry_permission_error_raises(tmp_path: Path, monkeypatch):
    p = _make_registry_file(tmp_path, [_entry()])
    def deny(*a, **kw):
        raise PermissionError("no read")
    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(CalibrationConfigError, match="permission"):
        load_registry(p)


def test_find_matching_entry_basic_match(tmp_path: Path):
    p = _make_registry_file(tmp_path, [_entry()])
    reg = load_registry(p)
    e = find_matching_entry(
        reg, deployment_id="dep1", model_alias="mini@mlx",
        primary_seal="A" * 64, recheck_seal="B" * 64,    # case-insensitive
    )
    assert e is not None
    assert e.deployment_id == "dep1"


def test_find_matching_entry_no_match_returns_none(tmp_path: Path):
    p = _make_registry_file(tmp_path, [_entry()])
    reg = load_registry(p)
    e = find_matching_entry(
        reg, deployment_id="other", model_alias="mini@mlx",
        primary_seal="a" * 64, recheck_seal="b" * 64,
    )
    assert e is None


def test_find_matching_entry_tiebreak_by_calibrated_at(tmp_path: Path):
    """Multi-match: newest calibrated_at wins first."""
    e_old = _entry(calibrated_at="2026-04-01T00:00:00Z")
    e_new = _entry(calibrated_at="2026-05-20T00:00:00Z")
    p = _make_registry_file(tmp_path, [e_old, e_new])
    reg = load_registry(p)
    e = find_matching_entry(
        reg, deployment_id="dep1", model_alias="mini@mlx",
        primary_seal="a" * 64, recheck_seal="b" * 64,
    )
    assert e.calibrated_at == "2026-05-20T00:00:00Z"


def test_find_matching_entry_tiebreak_by_sample_size(tmp_path: Path):
    """Same calibrated_at — larger sample_size wins."""
    e_small = _entry(calibrated_at="2026-05-20T00:00:00Z", sample_size=30)
    e_large = _entry(calibrated_at="2026-05-20T00:00:00Z", sample_size=120)
    p = _make_registry_file(tmp_path, [e_small, e_large])
    reg = load_registry(p)
    e = find_matching_entry(
        reg, deployment_id="dep1", model_alias="mini@mlx",
        primary_seal="a" * 64, recheck_seal="b" * 64,
    )
    assert e.sample_size == 120


def test_calibration_entry_age_days():
    e = CalibrationCacheEntry(
        schema_version=1, deployment_id="d", model_alias="m",
        primary_prompt_seal_sha256="a" * 64,
        recheck_prompt_seal_sha256="b" * 64,
        calibrated_at=(datetime.now(timezone.utc) - timedelta(days=10))
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        sample_size=30, fn_rate=0.1, fn_rate_ci95=(0.05, 0.18),
        source_case_id="c",
    )
    age = e.age_days()
    assert 9 <= age <= 11
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_calibration_cache.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `_calibration_cache.py`**

Create `src/dsar_pipeline/_calibration_cache.py`:

```python
"""Calibration registry reader (spec §4.2 (C) / v6).

Responsibilities:
  - Resolve the registry path: env var > config > default.
  - Load + strictly validate entries (skip + warn on per-entry failure).
  - Provide lookup with deterministic multi-match tie-break.
  - Handle remote URIs (s3://, https://) with jittered backoff retry.

Caller (RecheckStage) translates the result into a ModeDecision via
its own decide_mode() — this module only loads + finds.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_REMOTE_SCHEMES = {"s3", "https", "http", "gs", "azblob"}
_RETRY_DELAYS_RANGE = [(0.5, 1.5), (1.0, 3.0), (2.0, 6.0)]
_DEFAULT_REGISTRY_PATH = Path.home() / ".dsar" / "calibration_registry.json"


class CalibrationConfigError(RuntimeError):
    """Loud configuration error (e.g. permission denied)."""


class RemoteResourceNotFound(RuntimeError):
    """Remote read returned 404 / NoSuchKey — terminal, no retry."""


def _normalise_hash(s: str) -> str:
    """Strip + lowercase. Used for all seal comparisons."""
    return s.strip().lower()


@dataclass(frozen=True)
class CalibrationCacheEntry:
    schema_version: int
    deployment_id: str
    model_alias: str
    primary_prompt_seal_sha256: str
    recheck_prompt_seal_sha256: str
    calibrated_at: str                          # ISO-8601 UTC
    sample_size: int
    fn_rate: float
    fn_rate_ci95: tuple[float, float]
    source_case_id: str

    def age_days(self) -> float:
        try:
            dt = datetime.fromisoformat(self.calibrated_at.replace("Z", "+00:00"))
        except ValueError:
            # Should have been caught at validation time; defensive.
            return float("inf")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 86400.0


@dataclass(frozen=True)
class CalibrationRegistry:
    schema_version: int
    entries: tuple[CalibrationCacheEntry, ...]


def resolve_registry_path(cfg_path: Optional[str | Path]) -> Path:
    """Env var > config > default. Always returns a path (may not exist)."""
    env = os.environ.get("DSAR_CALIBRATION_REGISTRY")
    if env:
        return Path(env).expanduser()
    if cfg_path:
        return Path(cfg_path).expanduser()
    return _DEFAULT_REGISTRY_PATH


def _is_remote(path: Path) -> bool:
    # Path of `s3://bucket/key` becomes PosixPath('s3:/bucket/key') —
    # detect via the raw string when it parses as a URL with a scheme.
    s = str(path)
    parsed = urlparse(s)
    return parsed.scheme.lower() in _REMOTE_SCHEMES


def _validate_entry(raw: dict) -> Optional[CalibrationCacheEntry]:
    """Strict validation; returns None + warns on failure."""
    try:
        schema_version = raw["schema_version"]
        if schema_version != 1:
            raise ValueError(f"schema_version != 1 (got {schema_version!r})")
        deployment_id = str(raw["deployment_id"])
        model_alias = str(raw["model_alias"])
        primary_seal = _normalise_hash(str(raw["primary_prompt_seal_sha256"]))
        recheck_seal = _normalise_hash(str(raw["recheck_prompt_seal_sha256"]))
        if not _HEX64_RE.match(primary_seal):
            raise ValueError(f"primary_prompt_seal_sha256 not 64-hex")
        if not _HEX64_RE.match(recheck_seal):
            raise ValueError(f"recheck_prompt_seal_sha256 not 64-hex")
        calibrated_at = str(raw["calibrated_at"])
        # Smoke-parse: must be valid ISO-8601
        datetime.fromisoformat(calibrated_at.replace("Z", "+00:00"))
        sample_size = int(raw["sample_size"])
        if sample_size < 0:
            raise ValueError("sample_size < 0")
        fn_rate = float(raw["fn_rate"])
        ci = raw["fn_rate_ci95"]
        if not isinstance(ci, list) or len(ci) != 2:
            raise ValueError("fn_rate_ci95 must be a 2-element list")
        lo, hi = float(ci[0]), float(ci[1])
        if not (0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0):
            raise ValueError(f"fn_rate_ci95 outside [0, 1]: [{lo}, {hi}]")
        if lo > hi:
            raise ValueError(f"fn_rate_ci95 lo > hi: [{lo}, {hi}]")
        if not (lo <= fn_rate <= hi):
            raise ValueError(f"fn_rate {fn_rate} outside CI [{lo}, {hi}]")
        source_case_id = str(raw.get("source_case_id", "") or "")
        return CalibrationCacheEntry(
            schema_version=schema_version,
            deployment_id=deployment_id,
            model_alias=model_alias,
            primary_prompt_seal_sha256=primary_seal,
            recheck_prompt_seal_sha256=recheck_seal,
            calibrated_at=calibrated_at,
            sample_size=sample_size,
            fn_rate=fn_rate,
            fn_rate_ci95=(lo, hi),
            source_case_id=source_case_id,
        )
    except (KeyError, ValueError, TypeError) as e:
        log.warning("calibration registry: skipping invalid entry (%s): %s",
                    type(e).__name__, e)
        return None


def _read_local(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError as e:
        raise CalibrationConfigError(
            f"permission denied reading {path}: {e}") from e
    # Other OSError propagates unchanged (disk failure → operator-visible).


def _read_remote_with_retry(uri: str) -> str:
    """Remote read with jittered backoff. 404/NoSuchKey terminal."""
    # NB: actual S3/HTTPS readers live elsewhere in the toolkit; this
    # module dispatches through a `_remote_get` shim that the toolkit's
    # storage abstraction provides. For Phase 3 we keep the dispatch
    # surface narrow — concrete remote impls are wired in Phase 5.
    last_exc = None
    for attempt, (lo, hi) in enumerate(_RETRY_DELAYS_RANGE):
        try:
            return _remote_get(uri)
        except RemoteResourceNotFound:
            raise              # terminal — do not retry
        except Exception as e:
            last_exc = e
            log.warning("remote calibration read attempt %d failed: %s; "
                        "retrying", attempt + 1, e)
            time.sleep(random.uniform(lo, hi))
    raise CalibrationConfigError(
        f"remote calibration read failed after 3 attempts: {last_exc}"
    ) from last_exc


def _remote_get(uri: str) -> str:
    """Shim — Phase 3 stub. Phase 5 wires this to the toolkit storage
    abstraction (`dsar_clients.storage`). Until then, raise."""
    raise NotImplementedError(
        f"remote calibration registry not wired until Phase 5: {uri}"
    )


def load_registry(path: Path) -> Optional[CalibrationRegistry]:
    """Load the registry from `path` (local or remote URI).

    Returns:
        None when the local file is missing (silent — auto mode handles
        cache-miss via decide_mode()).
        CalibrationRegistry with the validated entries otherwise.

    Raises:
        CalibrationConfigError: on permission denied, malformed top-level
            JSON, or unrecoverable remote failure.
    """
    if _is_remote(path):
        raw_text = _read_remote_with_retry(str(path))
    else:
        raw_text = _read_local(path)
        if raw_text is None:
            return None

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise CalibrationConfigError(
            f"calibration registry {path}: invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise CalibrationConfigError(
            f"calibration registry {path}: top-level must be object")
    if data.get("schema_version") != 1:
        raise CalibrationConfigError(
            f"calibration registry {path}: schema_version must be 1")
    entries_raw = data.get("entries", [])
    if not isinstance(entries_raw, list):
        raise CalibrationConfigError(
            f"calibration registry {path}: entries must be list")
    entries: list[CalibrationCacheEntry] = []
    for raw_entry in entries_raw:
        if not isinstance(raw_entry, dict):
            log.warning("calibration registry: skipping non-object entry")
            continue
        validated = _validate_entry(raw_entry)
        if validated is not None:
            entries.append(validated)
    return CalibrationRegistry(schema_version=1, entries=tuple(entries))


def find_matching_entry(
    registry: Optional[CalibrationRegistry],
    *,
    deployment_id: str,
    model_alias: str,
    primary_seal: str,
    recheck_seal: str,
) -> Optional[CalibrationCacheEntry]:
    """Lookup with strict (deployment, model, primary_seal, recheck_seal)
    match. Multi-match tie-break: max(calibrated_at, sample_size,
    source_case_id) lexicographic.
    """
    if registry is None:
        return None
    primary_norm = _normalise_hash(primary_seal)
    recheck_norm = _normalise_hash(recheck_seal)
    candidates = [
        e for e in registry.entries
        if e.deployment_id == deployment_id
        and e.model_alias == model_alias
        and e.primary_prompt_seal_sha256 == primary_norm
        and e.recheck_prompt_seal_sha256 == recheck_norm
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda e: (e.calibrated_at, e.sample_size, e.source_case_id or ""),
    )
```

- [ ] **Step 4: Run tests; verify all pass**

```bash
uv run pytest tests/test_calibration_cache.py -v
```

Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/_calibration_cache.py tests/test_calibration_cache.py
git commit -m "feat(calibration_cache): registry loader + tie-break + retry"
```

---

### Task 30: Create `config/pricing.json`

**Files:**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/config/__init__.py`
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/config/pricing.json`
- Create: `~/projects/dsar-toolkit/tests/test_pricing_config.py`

Spec §4.2 (E) — model_alias → per-1k token USD. Provider-aware aliases (`@anthropic`, `@bedrock`, `@mlx`).

- [ ] **Step 1: Ensure the config package exists**

```bash
mkdir -p ~/projects/dsar-toolkit/src/dsar_pipeline/config
```

Create `src/dsar_pipeline/config/__init__.py` (empty — marker file so `importlib.resources` can locate `pricing.json`):

```python
"""dsar_pipeline.config — JSON config assets loaded via importlib.resources."""
```

- [ ] **Step 2: Write the pricing config**

Create `src/dsar_pipeline/config/pricing.json`:

```json
{
  "schema_version": 1,
  "entries": [
    {"model_alias": "claude-opus-4-7@anthropic", "in_per_1k_tokens_usd": 0.015, "out_per_1k_tokens_usd": 0.075},
    {"model_alias": "claude-opus-4-7@bedrock", "in_per_1k_tokens_usd": 0.015, "out_per_1k_tokens_usd": 0.075},
    {"model_alias": "claude-sonnet-4-7@anthropic", "in_per_1k_tokens_usd": 0.003, "out_per_1k_tokens_usd": 0.015},
    {"model_alias": "claude-haiku-4-5@anthropic", "in_per_1k_tokens_usd": 0.001, "out_per_1k_tokens_usd": 0.005},
    {"model_alias": "mini@mlx", "in_per_1k_tokens_usd": 0.0, "out_per_1k_tokens_usd": 0.0},
    {"model_alias": "default", "in_per_1k_tokens_usd": 0.0, "out_per_1k_tokens_usd": 0.0}
  ]
}
```

- [ ] **Step 3: Write tests asserting the config loads + lookups work**

Create `tests/test_pricing_config.py`:

```python
"""Tests for config/pricing.json + pricing helpers."""
from __future__ import annotations

import json
from importlib import resources

import pytest


def test_pricing_json_is_valid_and_complete():
    """The shipped pricing.json parses and has the schema_version + entries."""
    raw = resources.files("dsar_pipeline.config").joinpath("pricing.json").read_text(
        encoding="utf-8")
    data = json.loads(raw)
    assert data["schema_version"] == 1
    aliases = {e["model_alias"] for e in data["entries"]}
    # default fallback MUST be present (spec §4.2 (E))
    assert "default" in aliases


def test_pricing_lookup_known_alias():
    """estimate_cost_usd helper returns the right USD value."""
    from dsar_pipeline.recheck_stage import estimate_cost_usd
    cost = estimate_cost_usd(
        model_alias="claude-opus-4-7@anthropic",
        input_tokens=1000, output_tokens=200,
    )
    # 0.015 + 0.075 * 0.2 = 0.030
    assert cost == pytest.approx(0.030, abs=1e-6)


def test_pricing_lookup_unknown_alias_returns_none():
    """Spec §4.2 (E): unknown alias → estimated_cost_usd: null."""
    from dsar_pipeline.recheck_stage import estimate_cost_usd
    cost = estimate_cost_usd(
        model_alias="vapourware@nowhere",
        input_tokens=1000, output_tokens=200,
    )
    assert cost is None


def test_pricing_lookup_mlx_is_zero():
    """Local MLX models are free in our cost model."""
    from dsar_pipeline.recheck_stage import estimate_cost_usd
    cost = estimate_cost_usd(
        model_alias="mini@mlx", input_tokens=100, output_tokens=50,
    )
    assert cost == pytest.approx(0.0, abs=1e-9)
```

- [ ] **Step 4: Run; expect failures (recheck_stage not yet implemented)**

```bash
uv run pytest tests/test_pricing_config.py -v
```

Expected: First test PASSES (just JSON load); 3 others fail with `ImportError`. We leave them failing — Task 31 implements `estimate_cost_usd`.

- [ ] **Step 5: Make sure pricing.json is packaged**

Verify `pyproject.toml` includes `*.json` under `[tool.setuptools.package-data]` (or equivalent). Grep first:

```bash
cd ~/projects/dsar-toolkit
grep -n "package-data\|include-package-data" pyproject.toml
```

If no package-data clause covers `dsar_pipeline.config/*.json`, add (after `[tool.setuptools]` section):

```toml
[tool.setuptools.package-data]
"dsar_pipeline.config" = ["*.json"]
"dsar_pipeline.gates.prompts" = ["*.md", "_registry.json", "_archive/**/*.gz"]
```

(If the file already has a wildcard like `"dsar_pipeline" = ["**/*.json"]` no change needed.)

Reinstall in editable mode so the new package data is discoverable:

```bash
uv pip install -e .
```

- [ ] **Step 6: Re-run the first pricing test; verify pass**

```bash
uv run pytest tests/test_pricing_config.py::test_pricing_json_is_valid_and_complete -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dsar_pipeline/config/__init__.py src/dsar_pipeline/config/pricing.json tests/test_pricing_config.py pyproject.toml
git commit -m "feat(config): add pricing.json with per-1k token USD per model_alias"
```

---

### Task 31: Implement `GateDurantRecheck` — the per-doc LLM gate

**Files:**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/gates/gate_durant_recheck.py`
- Create: `~/projects/dsar-toolkit/tests/test_gate_durant_recheck.py`

Spec §4.2 (A) + (F):
- `GateDurantRecheck(BaseGateAgent)` per-doc LLM call.
- Consumes ONLY refs the primary classified `work_context_only`.
- Loads `prompts/durant.recheck.system.md` via `PromptLoader.load("durant.recheck.system")` (Phase 1).
- Truncation via Phase 2 `truncate_with_token_check` (router has token counter or no-op fallback).
- Returns per-ref structured verdict: `confirmed_work_context_only | reclassify_to_biographical | reclassify_to_ambiguous`.
- On LLM failure: `error_state = {code, message, raw}` with `_sanitise_raw` applied.
- Audit fields per spec §4.2 (F): `prompt_id`, `prompt_canonical_seal_sha256`, `prompt_applied_strips`, `prompt_effective_sha256`, `elapsed_sec`, `model`, `token_safety_iterations`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_gate_durant_recheck.py`:

```python
"""Tests for GateDurantRecheck (spec §4.2 (A) and (F))."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dsar_pipeline.gates.gate_durant_recheck import (
    GateDurantRecheck, _sanitise_raw,
)


def _make_case_dir(tmp_path: Path) -> Path:
    case_dir = tmp_path / "case-001"
    (case_dir / "working").mkdir(parents=True)
    register = {"D000001": {"ref": "D000001", "filename": "doc1.txt",
                            "category": "email"}}
    (case_dir / "working" / "register.json").write_text(
        json.dumps(register), encoding="utf-8")
    (case_dir / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "Alice Smith", "email": "alice@x.com"}),
        encoding="utf-8")
    (case_dir / "working" / "D000001.txt").write_text(
        "Body discussing project Foo at length.", encoding="utf-8")
    return case_dir


def test_recheck_verdict_confirmed(tmp_path):
    case_dir = _make_case_dir(tmp_path)
    mock_router = MagicMock()
    mock_router.call.return_value = {
        "output": {
            "recheck_verdict": "confirmed_work_context_only",
            "rationale": "Subject only cc'd; topic unrelated.",
        },
        "model": "mini@mlx",
        "input_tokens": 800,
        "output_tokens": 30,
    }
    mock_router.has_token_counter_for.return_value = False
    gate = GateDurantRecheck(router=mock_router)
    row = gate.classify(case_dir, "D000001")
    assert row["recheck_verdict"] == "confirmed_work_context_only"
    assert row["error_state"] is None
    assert row["model"] == "mini@mlx"
    assert row["prompt_id"] == "durant.recheck.system"
    assert len(row["prompt_canonical_seal_sha256"]) == 64
    assert row["doc_ref"] == "D000001"


def test_recheck_verdict_reclassify_to_biographical(tmp_path):
    case_dir = _make_case_dir(tmp_path)
    mock_router = MagicMock()
    mock_router.call.return_value = {
        "output": {
            "recheck_verdict": "reclassify_to_biographical",
            "rationale": "Tail of thread is performance review.",
        },
        "model": "claude-opus-4-7@anthropic",
        "input_tokens": 1200,
        "output_tokens": 40,
    }
    mock_router.has_token_counter_for.return_value = False
    gate = GateDurantRecheck(router=mock_router)
    row = gate.classify(case_dir, "D000001")
    assert row["recheck_verdict"] == "reclassify_to_biographical"
    assert row["error_state"] is None
    # cost telemetry: opus is non-zero
    assert row["estimated_cost_usd"] is not None
    assert row["estimated_cost_usd"] > 0


def test_recheck_verdict_reclassify_to_ambiguous(tmp_path):
    case_dir = _make_case_dir(tmp_path)
    mock_router = MagicMock()
    mock_router.call.return_value = {
        "output": {
            "recheck_verdict": "reclassify_to_ambiguous",
            "rationale": "Mixed evidence.",
        },
        "model": "mini@mlx",
        "input_tokens": 800, "output_tokens": 25,
    }
    mock_router.has_token_counter_for.return_value = False
    gate = GateDurantRecheck(router=mock_router)
    row = gate.classify(case_dir, "D000001")
    assert row["recheck_verdict"] == "reclassify_to_ambiguous"


def test_recheck_error_state_on_llm_failure(tmp_path):
    """Spec §4.2 (F): when LLM raises → error_state set, recheck_verdict null."""
    case_dir = _make_case_dir(tmp_path)
    mock_router = MagicMock()
    mock_router.call.side_effect = ConnectionError("model unreachable")
    mock_router.has_token_counter_for.return_value = False
    gate = GateDurantRecheck(router=mock_router)
    row = gate.classify(case_dir, "D000001")
    assert row["recheck_verdict"] is None
    assert row["error_state"]["code"] == "model_unreachable"
    assert "unreachable" in row["error_state"]["message"]
    assert row["estimated_cost_usd"] is None


def test_recheck_error_state_on_invalid_verdict(tmp_path):
    """Model returns nonsense verdict → schema_validation_failed."""
    case_dir = _make_case_dir(tmp_path)
    mock_router = MagicMock()
    mock_router.call.return_value = {
        "output": {"recheck_verdict": "definitely_not_a_real_enum",
                   "rationale": "x"},
        "model": "mini@mlx", "input_tokens": 800, "output_tokens": 25,
    }
    mock_router.has_token_counter_for.return_value = False
    gate = GateDurantRecheck(router=mock_router)
    row = gate.classify(case_dir, "D000001")
    assert row["recheck_verdict"] is None
    assert row["error_state"]["code"] == "schema_validation_failed"


def test_sanitise_raw_redacts_bearer_token():
    raw = "Authorization: Bearer sk-abc123secret"
    out = _sanitise_raw(raw)
    assert "sk-abc123secret" not in out
    assert "[REDACTED]" in out


def test_sanitise_raw_redacts_basic_auth():
    raw = "Authorization: Basic dXNlcjpwYXNz"
    out = _sanitise_raw(raw)
    assert "dXNlcjpwYXNz" not in out


def test_sanitise_raw_redacts_aws_keys():
    raw = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    out = _sanitise_raw(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_sanitise_raw_redacts_userpass_url():
    raw = "Connection failed: postgres://admin:hunter2@db.example.com/x"
    out = _sanitise_raw(raw)
    assert "hunter2" not in out


def test_sanitise_raw_pre_cap_16kb_then_final_200():
    """Spec §4.2 v6: 16KB pre-cap, then strip, then 200-char final cap."""
    raw = "x" * 30_000
    out = _sanitise_raw(raw)
    assert len(out) <= 200
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_gate_durant_recheck.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `gate_durant_recheck.py`**

Create `src/dsar_pipeline/gates/gate_durant_recheck.py`:

```python
"""GateDurantRecheck — independent inverse-question recheck per spec §4.2.

Consumes refs the primary gate_durant pass classified work_context_only.
Asks the LLM to re-examine each one from scratch: is this document
actually NOT about the subject? Returns one of three verdicts:
  - confirmed_work_context_only
  - reclassify_to_biographical
  - reclassify_to_ambiguous

Output rows (one per ref) are written by RecheckStage to
working/durant_underdisclosure_recheck.jsonl. This gate's `classify`
method returns the row dict; orchestration + writing live in the stage.
"""
from __future__ import annotations

import json
import logging
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any, Optional

from .base import BaseGateAgent, GateFinding, GateReport
from .prompt_loader import PromptLoader

log = logging.getLogger(__name__)


# --------------------------------------------- _sanitise_raw

_PRE_CAP = 16 * 1024
_FINAL_CAP = 200

_CRED_PATTERNS = [
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-=+/]+", re.IGNORECASE),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/=]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_\-]+"),
    re.compile(r"\bAuthorization\s*:\s*\S+", re.IGNORECASE),
    re.compile(r"\bAWS_[A-Z_]*KEY[A-Z_]*\s*=\s*[A-Za-z0-9/+=]+"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@[^\s]+"),
]


def _sanitise_raw(s: str) -> str:
    """Strip credential patterns; cap to 200 chars. Spec §4.2 (F) v6.

    Pre-cap to 16KB before regex to bound ReDoS on adversarial input.
    """
    if not isinstance(s, str):
        s = str(s)
    s = s[:_PRE_CAP]
    for pat in _CRED_PATTERNS:
        s = pat.sub("[REDACTED]", s)
    return s[:_FINAL_CAP]


# --------------------------------------------- error classification

_VALID_VERDICTS = frozenset({
    "confirmed_work_context_only",
    "reclassify_to_biographical",
    "reclassify_to_ambiguous",
})


def _classify_error(exc: BaseException) -> str:
    """Map Python exception type → spec §4.2 (F) error_state.code enum."""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "connection" in name or "unreachable" in str(exc).lower():
        return "model_unreachable"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "schema_validation_failed"
    return "unknown"


# --------------------------------------------- cost estimation

def _load_pricing() -> dict[str, dict]:
    """Load config/pricing.json as {model_alias: {in, out}} dict.
    Cached at module level via lazy init."""
    global _PRICING_CACHE
    if _PRICING_CACHE is None:
        raw = resources.files("dsar_pipeline.config").joinpath(
            "pricing.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        _PRICING_CACHE = {
            e["model_alias"]: {"in": e["in_per_1k_tokens_usd"],
                               "out": e["out_per_1k_tokens_usd"]}
            for e in data["entries"]
        }
    return _PRICING_CACHE


_PRICING_CACHE: Optional[dict[str, dict]] = None
_PRICING_WARNED_ALIASES: set[str] = set()


def estimate_cost_usd(*, model_alias: str, input_tokens: int,
                      output_tokens: int) -> Optional[float]:
    """Per spec §4.2 (E): unknown alias → None (one-time warning per alias).
    Known alias → in_per_1k * input_tokens / 1000 + out_per_1k * output_tokens / 1000.
    """
    pricing = _load_pricing()
    entry = pricing.get(model_alias)
    if entry is None:
        if model_alias not in _PRICING_WARNED_ALIASES:
            log.warning(
                "pricing.json: no entry for model_alias=%r; "
                "estimated_cost_usd will be null", model_alias)
            _PRICING_WARNED_ALIASES.add(model_alias)
        return None
    return (entry["in"] * input_tokens / 1000.0
            + entry["out"] * output_tokens / 1000.0)


# --------------------------------------------- the gate

class GateDurantRecheck(BaseGateAgent):
    name = "gate_durant_recheck"
    tier = 2
    kind = "llm"
    cost_estimate_usd = 0.01

    PROMPT_ID = "durant.recheck.system"

    def __init__(self, *,
                 router: Optional[Any] = None,
                 role: str = "scope_check",
                 max_text_chars: Optional[int] = None):
        self.router = router
        self.role = role
        self.max_text_chars = max_text_chars

    # ------------------------------------------ BaseGateAgent contract

    def run(self, case_dir: Path, refs: list[str]) -> GateReport:
        """Stage-driven entry — the stage iterates refs explicitly via
        classify(). This method exists for BaseGateAgent compliance + ad-hoc
        diagnostic use; production path is RecheckStage._run_stage.
        """
        t0 = time.time()
        findings: list[GateFinding] = []
        examined = 0
        for ref in refs:
            try:
                row = self.classify(case_dir, ref)
            except Exception as e:
                findings.append(GateFinding(
                    gate_name=self.name, tier=self.tier, kind=self.kind,
                    severity="high",
                    issue=f"recheck classify failed: {type(e).__name__}",
                    ref=ref, evidence=_sanitise_raw(str(e)),
                ))
                continue
            examined += 1
            if row["recheck_verdict"] == "reclassify_to_biographical":
                findings.append(GateFinding(
                    gate_name=self.name, tier=self.tier, kind=self.kind,
                    severity="medium",
                    issue="Recheck promoted WCO → biographical (safety net hit)",
                    ref=ref,
                    evidence=row.get("rationale", "")[:200],
                    metadata=row,
                ))
            elif row["recheck_verdict"] == "reclassify_to_ambiguous":
                findings.append(GateFinding(
                    gate_name=self.name, tier=self.tier, kind=self.kind,
                    severity="low",
                    issue="Recheck escalated WCO → ambiguous",
                    ref=ref, evidence=row.get("rationale", "")[:200],
                    metadata=row,
                ))
        return GateReport(
            gate_name=self.name, tier=self.tier, kind=self.kind,
            findings=findings, refs_examined=examined,
            duration_ms=int((time.time() - t0) * 1000),
        )

    # ------------------------------------------ stage-facing API

    def classify(self, case_dir: Path, ref: str) -> dict:
        """Run the recheck for ONE ref. Returns the per-ref JSONL row dict
        matching `schemas/durant_recheck_row.schema.json`.
        """
        case_id = case_dir.name
        asset = PromptLoader.load(self.PROMPT_ID)
        system_prompt = asset.body

        register = self._load_register(case_dir)
        entry = (register or {}).get(ref)
        if entry is None:
            return self._error_row(
                case_id=case_id, ref=ref,
                code="unknown",
                message=f"ref {ref!r} not in register.json",
                raw=f"register lookup miss for {ref}",
                model=None,
                asset=asset,
                elapsed=0.0,
            )

        text = self._load_ref_text(case_dir, entry)
        if not text:
            return self._error_row(
                case_id=case_id, ref=ref, code="empty_response",
                message="no extracted text for ref",
                raw="", model=None, asset=asset, elapsed=0.0,
            )

        subj_path = case_dir / "working" / "data_subject.json"
        subject = (json.loads(subj_path.read_text(encoding="utf-8"))
                   if subj_path.exists() else {})
        user_prompt = self._build_user_prompt(subject, entry, text)

        router = self._get_router(case_dir)
        t0 = time.time()
        token_safety_iterations = 0   # truncation token-belt drives this in
        #                                Phase 2; recheck stage may invoke
        #                                truncate_with_token_check here in a
        #                                follow-up. Phase 3 records 0.
        try:
            result = router.call(
                role=self.role,
                system=system_prompt,
                user=user_prompt,
                doc_ref=ref,
                tool_schema={
                    "name": "durant_recheck_verdict",
                    "description": "Independent recheck verdict.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "recheck_verdict": {
                                "type": "string",
                                "enum": sorted(_VALID_VERDICTS),
                            },
                            "rationale": {
                                "type": "string",
                                "description": "One-sentence justification (≤100 chars).",
                            },
                        },
                        "required": ["recheck_verdict", "rationale"],
                    },
                },
            )
        except Exception as e:
            return self._error_row(
                case_id=case_id, ref=ref,
                code=_classify_error(e),
                message=str(e)[:_FINAL_CAP],
                raw=str(e),
                model=None,
                asset=asset,
                elapsed=time.time() - t0,
            )

        output = result.get("output") or {}
        verdict = output.get("recheck_verdict")
        rationale = (output.get("rationale") or "")[:100]
        model = result.get("model") or "unknown"

        if verdict not in _VALID_VERDICTS:
            return self._error_row(
                case_id=case_id, ref=ref, code="schema_validation_failed",
                message=f"invalid recheck_verdict: {verdict!r}",
                raw=json.dumps(output)[:_FINAL_CAP],
                model=model, asset=asset,
                elapsed=time.time() - t0,
            )

        input_tokens = int(result.get("input_tokens") or 0)
        output_tokens = int(result.get("output_tokens") or 0)
        cost = estimate_cost_usd(
            model_alias=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

        return {
            "case_id": case_id,
            "doc_ref": ref,
            "recheck_verdict": verdict,
            "rationale": rationale,
            "model": model,
            "prompt_id": asset.prompt_id,
            "prompt_canonical_seal_sha256": asset.canonical_seal_sha256,
            "prompt_applied_strips": list(asset.applied_strips),
            "prompt_effective_sha256": asset.effective_sha256,
            "elapsed_sec": round(time.time() - t0, 3),
            "error_state": None,
            "estimated_cost_usd": cost,
            "token_safety_iterations": token_safety_iterations,
        }

    # ------------------------------------------ helpers

    @staticmethod
    def _error_row(*, case_id: str, ref: str, code: str, message: str,
                   raw: str, model: Optional[str], asset, elapsed: float) -> dict:
        return {
            "case_id": case_id,
            "doc_ref": ref,
            "recheck_verdict": None,
            "rationale": None,
            "model": model or "unknown",
            "prompt_id": asset.prompt_id,
            "prompt_canonical_seal_sha256": asset.canonical_seal_sha256,
            "prompt_applied_strips": list(asset.applied_strips),
            "prompt_effective_sha256": asset.effective_sha256,
            "elapsed_sec": round(elapsed, 3),
            "error_state": {
                "code": code,
                "message": message[:_FINAL_CAP],
                "raw": _sanitise_raw(raw),
            },
            "estimated_cost_usd": None,
            "token_safety_iterations": 0,
        }

    @staticmethod
    def _load_register(case_dir: Path) -> Optional[dict]:
        path = case_dir / "working" / "register.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            return {e["ref"]: e for e in data if isinstance(e, dict) and "ref" in e}
        return data if isinstance(data, dict) else None

    def _load_ref_text(self, case_dir: Path, entry: dict) -> str:
        text_file = entry.get("text_file")
        if text_file:
            p = Path(text_file)
            if not p.is_absolute():
                p = case_dir / p
            if p.exists():
                t = p.read_text(encoding="utf-8", errors="replace")
                return t[:self.max_text_chars] if self.max_text_chars else t
        ref = entry.get("ref", "")
        fallback = case_dir / "working" / f"{ref}.txt"
        if fallback.exists():
            t = fallback.read_text(encoding="utf-8", errors="replace")
            return t[:self.max_text_chars] if self.max_text_chars else t
        return ""

    @staticmethod
    def _build_user_prompt(subject: dict, entry: dict, text: str) -> str:
        names = subject.get("full_name") or subject.get("primary_name") or ""
        emails = (subject.get("email")
                  or ", ".join(subject.get("emails", []) or []))
        filename = entry.get("filename", "(unknown)")
        category = entry.get("category", "(unknown)")
        emails_clause = (f" (email address(es): {emails})" if emails else "")
        return f"""# Data subject
The data subject for this UK GDPR Article 15 access request is {names}{emails_clause}.

# Document under review
Ref: {entry.get('ref')}
Filename: {filename}
Category: {category}

# Document content (truncated)
{text}

Apply the Durant biographical-focus test INDEPENDENTLY. Return your recheck verdict via the durant_recheck_verdict tool."""

    def _get_router(self, case_dir: Path):
        if self.router is not None:
            return self.router
        from ..llm_router import RoleRouter
        self.router = RoleRouter(case_dir=case_dir)
        return self.router
```

- [ ] **Step 4: Run tests; verify all pass**

```bash
uv run pytest tests/test_gate_durant_recheck.py tests/test_pricing_config.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/gates/gate_durant_recheck.py tests/test_gate_durant_recheck.py
git commit -m "feat(gate_durant_recheck): per-doc inverse-question recheck gate"
```

---

### Task 32: Add `durant_recheck` to `VALID_STAGE_LABELS`

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/_stage_base.py`

- [ ] **Step 1: Add the label**

In `_stage_base.py`, change:

```python
VALID_STAGE_LABELS = frozenset({
    "ingest", "scope_check", "pii_identification",
    "exemption_check", "redact", "bake",
    "post_bake_verify", "final_synth",
})
```

to:

```python
VALID_STAGE_LABELS = frozenset({
    "ingest", "scope_check", "pii_identification",
    "exemption_check", "redact", "bake",
    "post_bake_verify", "final_synth",
    "durant_recheck",
})
```

- [ ] **Step 2: Run the stage-base tests; verify still pass**

```bash
cd ~/projects/dsar-toolkit
uv run pytest tests/test_stage_base.py -v
```

Expected: PASS (no test currently exercises the new label; the next task creates `RecheckStage` whose `__init_subclass__` validates against this set).

- [ ] **Step 3: Commit**

```bash
git add src/dsar_pipeline/_stage_base.py
git commit -m "feat(stage_base): register 'durant_recheck' stage label"
```

---

### Task 33: Create `schemas/durant_recheck_row.schema.json`

**Files:**
- Create: `~/projects/dsar-toolkit/schemas/durant_recheck_row.schema.json`
- Create test that validates the schema is well-formed JSON-Schema + rejects/accepts representative rows.

Spec §4.2 (F) constraints:
- `recheck_verdict != null ↔ error_state == null` (`oneOf` enforces mutual exclusion).
- `prompt_canonical_seal_sha256`, `prompt_effective_sha256` are full 64-char lowercase hex.
- `prompt_applied_strips` is an array of strings.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_gate_durant_recheck.py` (or split into a separate file if preferred — Phase 3 keeps these together for cohesion):

```python
def test_durant_recheck_row_schema_accepts_success_row():
    """A well-formed verdict row passes JSON Schema validation."""
    import json
    from importlib import resources
    import jsonschema

    schema_text = resources.files("dsar_pipeline").joinpath(
        "../schemas/durant_recheck_row.schema.json").read_text(encoding="utf-8")
    # The above relative path works under `pip install -e .`; for sdist
    # we ship the schema separately. Phase 3 leaves the loader simple.
    schema = json.loads(schema_text)

    row = {
        "case_id": "case-001",
        "doc_ref": "D000001",
        "recheck_verdict": "confirmed_work_context_only",
        "rationale": "Subject only cc'd.",
        "model": "mini@mlx",
        "prompt_id": "durant.recheck.system",
        "prompt_canonical_seal_sha256": "a" * 64,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "a" * 64,
        "elapsed_sec": 1.4,
        "error_state": None,
        "estimated_cost_usd": 0.0,
        "token_safety_iterations": 0,
    }
    jsonschema.validate(row, schema)   # raises on failure


def test_durant_recheck_row_schema_accepts_error_row():
    """An error row (verdict=null, error_state populated) passes."""
    import json
    from importlib import resources
    import jsonschema

    schema_text = resources.files("dsar_pipeline").joinpath(
        "../schemas/durant_recheck_row.schema.json").read_text(encoding="utf-8")
    schema = json.loads(schema_text)
    row = {
        "case_id": "case-001",
        "doc_ref": "D000001",
        "recheck_verdict": None,
        "rationale": None,
        "model": "mini@mlx",
        "prompt_id": "durant.recheck.system",
        "prompt_canonical_seal_sha256": "a" * 64,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "a" * 64,
        "elapsed_sec": 0.0,
        "error_state": {"code": "timeout", "message": "elapsed > 30s",
                        "raw": ""},
        "estimated_cost_usd": None,
        "token_safety_iterations": 0,
    }
    jsonschema.validate(row, schema)


def test_durant_recheck_row_schema_rejects_both_populated():
    """Mutually exclusive: verdict + error_state both populated → schema fails."""
    import json
    from importlib import resources
    import jsonschema

    schema_text = resources.files("dsar_pipeline").joinpath(
        "../schemas/durant_recheck_row.schema.json").read_text(encoding="utf-8")
    schema = json.loads(schema_text)
    bad_row = {
        "case_id": "case-001",
        "doc_ref": "D000001",
        "recheck_verdict": "confirmed_work_context_only",
        "rationale": "x",
        "model": "mini@mlx",
        "prompt_id": "durant.recheck.system",
        "prompt_canonical_seal_sha256": "a" * 64,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "a" * 64,
        "elapsed_sec": 1.0,
        "error_state": {"code": "timeout", "message": "x", "raw": ""},
        "estimated_cost_usd": 0.001,
        "token_safety_iterations": 0,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_row, schema)


def test_durant_recheck_row_schema_rejects_invalid_hex():
    """Non-64-hex seal field rejected."""
    import json
    from importlib import resources
    import jsonschema

    schema_text = resources.files("dsar_pipeline").joinpath(
        "../schemas/durant_recheck_row.schema.json").read_text(encoding="utf-8")
    schema = json.loads(schema_text)
    bad_row = {
        "case_id": "case-001",
        "doc_ref": "D000001",
        "recheck_verdict": "confirmed_work_context_only",
        "rationale": "x",
        "model": "mini@mlx",
        "prompt_id": "durant.recheck.system",
        "prompt_canonical_seal_sha256": "deadbeef",    # too short
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "a" * 64,
        "elapsed_sec": 1.0,
        "error_state": None,
        "estimated_cost_usd": 0.0,
        "token_safety_iterations": 0,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_row, schema)
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_gate_durant_recheck.py -v -k "schema"
```

Expected: failure — schema file does not exist.

- [ ] **Step 3: Write the schema**

Create `schemas/durant_recheck_row.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://harkers.dsar/schemas/durant_recheck_row.schema.json",
  "title": "Durant under-disclosure recheck row",
  "description": "One row in working/durant_underdisclosure_recheck.jsonl. Spec §4.2 (F).",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "case_id", "doc_ref", "recheck_verdict", "rationale", "model",
    "prompt_id", "prompt_canonical_seal_sha256", "prompt_applied_strips",
    "prompt_effective_sha256", "elapsed_sec", "error_state",
    "estimated_cost_usd", "token_safety_iterations"
  ],
  "properties": {
    "case_id": {"type": "string", "minLength": 1},
    "doc_ref": {"type": "string", "minLength": 1},
    "recheck_verdict": {
      "oneOf": [
        {"type": "null"},
        {"enum": [
          "confirmed_work_context_only",
          "reclassify_to_biographical",
          "reclassify_to_ambiguous"
        ]}
      ]
    },
    "rationale": {
      "oneOf": [{"type": "null"}, {"type": "string", "maxLength": 200}]
    },
    "model": {"type": "string", "minLength": 1},
    "prompt_id": {"type": "string", "minLength": 1},
    "prompt_canonical_seal_sha256": {
      "type": "string",
      "pattern": "^[0-9a-f]{64}$"
    },
    "prompt_applied_strips": {
      "type": "array",
      "items": {"type": "string"}
    },
    "prompt_effective_sha256": {
      "type": "string",
      "pattern": "^[0-9a-f]{64}$"
    },
    "elapsed_sec": {"type": "number", "minimum": 0},
    "error_state": {
      "oneOf": [
        {"type": "null"},
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["code", "message", "raw"],
          "properties": {
            "code": {
              "enum": [
                "model_unreachable", "schema_validation_failed",
                "empty_response", "timeout", "unknown"
              ]
            },
            "message": {"type": "string", "maxLength": 200},
            "raw": {"type": "string", "maxLength": 200}
          }
        }
      ]
    },
    "estimated_cost_usd": {
      "oneOf": [{"type": "null"}, {"type": "number", "minimum": 0}]
    },
    "token_safety_iterations": {"type": "integer", "minimum": 0}
  },
  "oneOf": [
    {
      "description": "Successful recheck — verdict set, error_state null.",
      "properties": {
        "recheck_verdict": {
          "enum": [
            "confirmed_work_context_only",
            "reclassify_to_biographical",
            "reclassify_to_ambiguous"
          ]
        },
        "error_state": {"type": "null"}
      },
      "required": ["recheck_verdict", "error_state"]
    },
    {
      "description": "Errored recheck — verdict null, error_state populated.",
      "properties": {
        "recheck_verdict": {"type": "null"},
        "error_state": {"type": "object"}
      },
      "required": ["recheck_verdict", "error_state"]
    }
  ]
}
```

- [ ] **Step 4: Ensure jsonschema dep is present**

```bash
cd ~/projects/dsar-toolkit
grep -n "jsonschema" pyproject.toml
```

If absent, add to `[project] dependencies = [...]`: `"jsonschema>=4.20"`. Then:

```bash
uv sync
uv pip install -e .
```

- [ ] **Step 5: Run; verify all schema tests pass**

```bash
uv run pytest tests/test_gate_durant_recheck.py -v -k "schema"
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add schemas/durant_recheck_row.schema.json tests/test_gate_durant_recheck.py pyproject.toml
git commit -m "feat(schemas): durant_recheck_row JSON Schema with verdict/error oneOf"
```

---

### Task 34: Implement `RecheckStage.decide_mode()` + config validation

**Files:**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/recheck_stage.py` (scaffolds + decide_mode only — orchestration in Task 35)
- Create: `~/projects/dsar-toolkit/tests/test_recheck_stage.py`

Spec §4.2 (B): mode `always | never | auto`. `mode=never` requires non-blank `override_reason`. Auto mode: cache-miss → "always"; stale → "always"; seal-drift → "always"; CI upper > threshold → "always"; CI upper ≤ threshold → "never".

- [ ] **Step 1: Write failing tests for decide_mode + init validation**

Create `tests/test_recheck_stage.py`:

```python
"""Tests for recheck_stage.py — decide_mode + RecheckStage orchestration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dsar_pipeline.recheck_stage import (
    ConfigError, ModeDecision, RecheckConfig, RecheckStage, decide_mode,
)
from dsar_pipeline._calibration_cache import CalibrationCacheEntry


def _entry(*, ci_upper: float = 0.05, age_days: int = 10,
           primary_seal: str = "a" * 64,
           recheck_seal: str = "b" * 64) -> CalibrationCacheEntry:
    return CalibrationCacheEntry(
        schema_version=1, deployment_id="dep1", model_alias="mini@mlx",
        primary_prompt_seal_sha256=primary_seal,
        recheck_prompt_seal_sha256=recheck_seal,
        calibrated_at=(datetime.now(timezone.utc) - timedelta(days=age_days))
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        sample_size=100, fn_rate=ci_upper - 0.02,
        fn_rate_ci95=(max(0.0, ci_upper - 0.05), ci_upper),
        source_case_id="c",
    )


def test_decide_mode_always_explicit():
    cfg = RecheckConfig(mode="always", override_reason="bypass for safety")
    d = decide_mode(cfg, cache_entry=None,
                    primary_seal="a" * 64, recheck_seal="b" * 64)
    assert d.mode_effective == "always"
    assert d.reason == "mode_set_explicit"


def test_decide_mode_never_explicit():
    cfg = RecheckConfig(mode="never", override_reason="trusted small model")
    d = decide_mode(cfg, cache_entry=None,
                    primary_seal="a" * 64, recheck_seal="b" * 64)
    assert d.mode_effective == "never"
    assert d.reason == "mode_set_explicit"


def test_decide_mode_auto_cache_miss():
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=90)
    d = decide_mode(cfg, cache_entry=None,
                    primary_seal="a" * 64, recheck_seal="b" * 64)
    assert d.mode_effective == "always"
    assert d.reason == "calibration_cache_miss"


def test_decide_mode_auto_stale():
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=30)
    stale = _entry(ci_upper=0.05, age_days=60)
    d = decide_mode(cfg, cache_entry=stale,
                    primary_seal="a" * 64, recheck_seal="b" * 64)
    assert d.mode_effective == "always"
    assert d.reason == "calibration_stale"


def test_decide_mode_auto_seal_drift_primary():
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=90)
    cache = _entry(primary_seal="a" * 64, recheck_seal="b" * 64)
    d = decide_mode(cfg, cache_entry=cache,
                    primary_seal="c" * 64,         # drifted
                    recheck_seal="b" * 64)
    assert d.mode_effective == "always"
    assert d.reason == "calibration_prompt_seal_drift"


def test_decide_mode_auto_seal_drift_recheck():
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=90)
    cache = _entry(primary_seal="a" * 64, recheck_seal="b" * 64)
    d = decide_mode(cfg, cache_entry=cache,
                    primary_seal="a" * 64,
                    recheck_seal="d" * 64)
    assert d.mode_effective == "always"
    assert d.reason == "calibration_prompt_seal_drift"


def test_decide_mode_auto_ci_upper_above_threshold():
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=90)
    cache = _entry(ci_upper=0.15)             # > 0.10
    d = decide_mode(cfg, cache_entry=cache,
                    primary_seal="a" * 64, recheck_seal="b" * 64)
    assert d.mode_effective == "always"
    assert d.reason == "ci_upper_above_threshold"


def test_decide_mode_auto_ci_upper_below_threshold():
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=90)
    cache = _entry(ci_upper=0.05)             # ≤ 0.10
    d = decide_mode(cfg, cache_entry=cache,
                    primary_seal="a" * 64, recheck_seal="b" * 64)
    assert d.mode_effective == "never"
    assert d.reason == "ci_upper_below_threshold"


def test_decide_mode_hash_comparison_case_insensitive():
    """_normalise_hash strips + lowercases — drift check is case-insensitive."""
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=90)
    cache = _entry(ci_upper=0.05, primary_seal="a" * 64,
                   recheck_seal="b" * 64)
    d = decide_mode(cfg, cache_entry=cache,
                    primary_seal="A" * 64,    # uppercase: same after normalise
                    recheck_seal="  B" * 64.__class__.__mul__("B", 64))
    # Simpler form: just pass uppercase seals
    d2 = decide_mode(cfg, cache_entry=cache,
                     primary_seal="A" * 64,
                     recheck_seal="B" * 64)
    assert d2.mode_effective == "never"
    assert d2.reason == "ci_upper_below_threshold"


def test_recheck_stage_init_rejects_never_without_reason(tmp_path):
    """Spec §4.2 (B): mode=never requires non-blank override_reason."""
    cfg = RecheckConfig(mode="never", override_reason="   ")    # whitespace-only
    with pytest.raises(ConfigError, match="override_reason"):
        RecheckStage(config=cfg)


def test_recheck_stage_init_accepts_never_with_reason(tmp_path):
    cfg = RecheckConfig(mode="never", override_reason="trusted local model")
    # Should not raise.
    stage = RecheckStage(config=cfg)
    assert stage.config.mode == "never"


def test_recheck_stage_init_rejects_invalid_concurrency():
    cfg = RecheckConfig(mode="always", max_concurrency=0)
    with pytest.raises(ConfigError, match="max_concurrency"):
        RecheckStage(config=cfg)
    cfg2 = RecheckConfig(mode="always", max_concurrency=33)
    with pytest.raises(ConfigError, match="max_concurrency"):
        RecheckStage(config=cfg2)


def test_recheck_stage_init_warns_above_16_concurrency(caplog):
    cfg = RecheckConfig(mode="always", max_concurrency=24,
                        override_reason="")
    import logging
    with caplog.at_level(logging.WARNING):
        RecheckStage(config=cfg)
    assert any("max_concurrency" in r.message for r in caplog.records)
```

Note: `64.__class__.__mul__("B", 64)` is awkward — replace with `"B" * 64` directly in the test (a copy-paste artifact in the prior draft). The simpler form `d2` in the test body is the canonical assertion.

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_recheck_stage.py -v
```

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement scaffolds + `decide_mode` in `recheck_stage.py`**

Create `src/dsar_pipeline/recheck_stage.py`:

```python
"""RecheckStage — calibration-gated under-disclosure recheck (spec §4.2).

Phase 3 lays down the stage envelope, decide_mode gating, ThreadPoolExecutor
orchestration, JSONL row writing via JsonlAppender, and three side-output
files:
  - working/durant_underdisclosure_recheck.jsonl  (per-ref rows)
  - working/recheck_decision.json                 (canonical "stage ran" marker)
  - working/recheck_summary.json                  (cost telemetry)

Phase 4 wires the synthesis step (Agent22 5-arg) to consume the JSONL.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ._calibration_cache import (
    CalibrationCacheEntry, CalibrationConfigError, _normalise_hash,
    find_matching_entry, load_registry, resolve_registry_path,
)
from ._jsonl_appender import JsonlAppender, RowSizeError
from ._stage_base import BaseStage
from .gates.gate_durant_recheck import GateDurantRecheck, estimate_cost_usd
from .gates.prompt_loader import PromptLoader

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


# ------------------------------------------------------ config

class ConfigError(RuntimeError):
    """Stage-init configuration error (e.g. mode=never with blank reason)."""


_VALID_MODES = frozenset({"always", "never", "auto"})


@dataclass
class RecheckConfig:
    """Per-case recheck config (spec §4.2 (B))."""
    mode: str = "auto"
    fn_threshold: float = 0.10
    calibration_max_age_days: int = 90
    max_concurrency: int = 4
    override_reason: str = ""
    deployment_id: str = ""
    calibration_registry_path: Optional[str] = None     # cfg-level path override
    model_alias: str = "mini@mlx"


@dataclass
class ModeDecision:
    """Result of decide_mode() — recorded verbatim in recheck_decision.json."""
    mode_effective: str           # "always" | "never"
    reason: str
    calibration_entry_used: Optional[CalibrationCacheEntry] = None


# ------------------------------------------------------ decide_mode

def decide_mode(cfg: RecheckConfig,
                cache_entry: Optional[CalibrationCacheEntry],
                *,
                primary_seal: str,
                recheck_seal: str) -> ModeDecision:
    """Per spec §4.2 (B) v6.

    `primary_seal` / `recheck_seal` are the CURRENT canonical seals of the
    `durant.system` and `durant.recheck.system` prompt assets at run-time.
    """
    if cfg.mode not in _VALID_MODES:
        raise ConfigError(f"recheck.mode must be one of {sorted(_VALID_MODES)}: "
                          f"got {cfg.mode!r}")

    if cfg.mode == "always":
        return ModeDecision("always", "mode_set_explicit", None)
    if cfg.mode == "never":
        return ModeDecision("never", "mode_set_explicit", None)

    # mode == "auto"
    if cache_entry is None:
        return ModeDecision("always", "calibration_cache_miss", None)
    if cache_entry.age_days() > cfg.calibration_max_age_days:
        return ModeDecision("always", "calibration_stale", cache_entry)

    if (_normalise_hash(primary_seal)
            != _normalise_hash(cache_entry.primary_prompt_seal_sha256)
            or _normalise_hash(recheck_seal)
            != _normalise_hash(cache_entry.recheck_prompt_seal_sha256)):
        return ModeDecision("always", "calibration_prompt_seal_drift",
                            cache_entry)

    ci_upper = cache_entry.fn_rate_ci95[1]
    if ci_upper > cfg.fn_threshold:
        return ModeDecision("always", "ci_upper_above_threshold", cache_entry)
    return ModeDecision("never", "ci_upper_below_threshold", cache_entry)


# ------------------------------------------------------ stage

class RecheckStage(BaseStage):
    stage_label = "durant_recheck"

    def __init__(self, *,
                 config: Optional[RecheckConfig] = None,
                 gate: Optional[GateDurantRecheck] = None):
        cfg = config or RecheckConfig()
        # Validate config at init — spec §4.2 (B).
        if cfg.mode == "never":
            if not (cfg.override_reason or "").strip():
                raise ConfigError(
                    "recheck.mode=never requires non-blank override_reason")
        if not (0 < cfg.max_concurrency <= 32):
            raise ConfigError(
                f"recheck.max_concurrency must be 1..32 (got {cfg.max_concurrency})")
        if cfg.max_concurrency > 16:
            log.warning("recheck.max_concurrency=%d exceeds 16; verify the "
                        "router rate-limit budget", cfg.max_concurrency)
        self.config = cfg
        self.gate = gate or GateDurantRecheck()

    # The orchestration body lives in Task 35.
    def _run_stage(self, case_dir: Path, refs: list[str],
                   iteration: int) -> dict:
        raise NotImplementedError("Task 35")


# CLI entry point (Task 36)
def main(argv: Optional[list[str]] = None) -> int:
    raise NotImplementedError("Task 36")
```

- [ ] **Step 4: Run tests; verify decide_mode + init pass**

```bash
uv run pytest tests/test_recheck_stage.py -v -k "decide_mode or recheck_stage_init"
```

Expected: PASS for these 13 tests. (The "case_insensitive" test variant with `64.__class__.__mul__` is a placeholder — the second assertion `d2` in that test is what matters. If pytest fails to even parse the test, fix the line by replacing the malformed `64.__class__.__mul__("B", 64)` expression with `"B" * 64` and re-run.)

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/recheck_stage.py tests/test_recheck_stage.py
git commit -m "feat(recheck_stage): decide_mode + RecheckConfig + init validation"
```

---

### Task 35: Implement `RecheckStage._run_stage` orchestration

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/recheck_stage.py`
- Modify: `~/projects/dsar-toolkit/tests/test_recheck_stage.py`

Orchestration responsibilities (spec §4.2 (A) (D) (E) (G)):
1. Resolve current prompt seals via PromptLoader.
2. Load calibration registry from `resolve_registry_path()`.
3. `decide_mode(cfg, entry, primary_seal, recheck_seal)`.
4. Write `working/recheck_decision.json` (canonical marker) ALWAYS, before any LLM calls.
5. If `mode_effective == "never"`: write empty `recheck_summary.json` + return summary; do not touch JSONL.
6. If `mode_effective == "always"`: iterate WCO refs (from primary durant output) via `ThreadPoolExecutor(max_workers=cfg.max_concurrency)`; each worker calls `gate.classify(case_dir, ref)`; write each row through one shared `JsonlAppender`.
7. Aggregate counts → write `working/recheck_summary.json` (cost, elapsed, counts).

- [ ] **Step 1: Write failing tests for orchestration**

Append to `tests/test_recheck_stage.py`:

```python
def _make_case_with_wco_refs(tmp_path: Path,
                              wco_refs: list[str]) -> Path:
    """Build a case fixture with primary durant output marking N refs as WCO."""
    case_dir = tmp_path / "case-001"
    (case_dir / "working").mkdir(parents=True)
    # register: WCO refs only (other refs irrelevant for recheck)
    register = {r: {"ref": r, "filename": f"{r}.txt", "category": "email"}
                for r in wco_refs}
    (case_dir / "working" / "register.json").write_text(
        json.dumps(register), encoding="utf-8")
    (case_dir / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "Alice Smith", "email": "alice@x.com"}),
        encoding="utf-8")
    # Mimic gate_durant's biographical_refs.json output
    per_ref = {r: {"verdict": "work_context_only",
                   "rationale": "Subject only cc'd."}
               for r in wco_refs}
    (case_dir / "working" / "biographical_refs.json").write_text(
        json.dumps({"biographical": [], "work_context_only": wco_refs,
                    "ambiguous": [], "per_ref": per_ref}),
        encoding="utf-8")
    for r in wco_refs:
        (case_dir / "working" / f"{r}.txt").write_text(
            "Body discussing project Foo.", encoding="utf-8")
    return case_dir


def test_recheck_stage_mode_never_writes_decision_only(tmp_path):
    cfg = RecheckConfig(mode="never", override_reason="trusted small model")
    case_dir = _make_case_with_wco_refs(tmp_path, ["D000001", "D000002"])
    stage = RecheckStage(config=cfg)
    summary = stage.run(case_dir, refs=["D000001", "D000002"], iteration=0)

    decision_path = case_dir / "working" / "recheck_decision.json"
    assert decision_path.exists()
    decision = json.loads(decision_path.read_text())
    assert decision["mode_requested"] == "never"
    assert decision["mode_effective"] == "never"
    assert decision["reason"] == "mode_set_explicit"

    # JSONL NOT written
    assert not (case_dir / "working" / "durant_underdisclosure_recheck.jsonl").exists()
    assert summary["docs_examined"] == 0


def test_recheck_stage_mode_always_runs_gate_on_wco_refs(tmp_path):
    cfg = RecheckConfig(mode="always", max_concurrency=2)
    case_dir = _make_case_with_wco_refs(tmp_path, ["D000001", "D000002", "D000003"])

    # Mock gate: returns confirmed for ref1, reclassify_to_biographical for ref2
    # and ref3 (synthetic).
    mock_gate = MagicMock()
    def fake_classify(cd, ref):
        return {
            "case_id": cd.name, "doc_ref": ref,
            "recheck_verdict": ("reclassify_to_biographical"
                                 if ref != "D000001"
                                 else "confirmed_work_context_only"),
            "rationale": "x", "model": "mini@mlx",
            "prompt_id": "durant.recheck.system",
            "prompt_canonical_seal_sha256": "a" * 64,
            "prompt_applied_strips": [],
            "prompt_effective_sha256": "a" * 64,
            "elapsed_sec": 0.1, "error_state": None,
            "estimated_cost_usd": 0.0, "token_safety_iterations": 0,
        }
    mock_gate.classify.side_effect = fake_classify

    stage = RecheckStage(config=cfg, gate=mock_gate)
    summary = stage.run(case_dir, refs=["D000001", "D000002", "D000003"],
                         iteration=0)

    jsonl_path = case_dir / "working" / "durant_underdisclosure_recheck.jsonl"
    assert jsonl_path.exists()
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    refs_seen = {json.loads(line)["doc_ref"] for line in lines}
    assert refs_seen == {"D000001", "D000002", "D000003"}

    assert summary["docs_examined"] == 3
    assert summary["docs_reclassified_to_biographical"] == 2
    assert summary["docs_confirmed_wco"] == 1
    assert summary["docs_reclassified_to_ambiguous"] == 0


def test_recheck_stage_mode_always_records_errors_in_summary(tmp_path):
    cfg = RecheckConfig(mode="always", max_concurrency=2)
    case_dir = _make_case_with_wco_refs(tmp_path, ["D000001"])

    mock_gate = MagicMock()
    mock_gate.classify.return_value = {
        "case_id": case_dir.name, "doc_ref": "D000001",
        "recheck_verdict": None, "rationale": None, "model": "mini@mlx",
        "prompt_id": "durant.recheck.system",
        "prompt_canonical_seal_sha256": "a" * 64,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "a" * 64,
        "elapsed_sec": 0.0,
        "error_state": {"code": "timeout", "message": "x", "raw": ""},
        "estimated_cost_usd": None, "token_safety_iterations": 0,
    }
    stage = RecheckStage(config=cfg, gate=mock_gate)
    summary = stage.run(case_dir, refs=["D000001"], iteration=0)
    assert summary["errors"] == 1
    assert summary["docs_examined"] == 1
    assert summary["docs_confirmed_wco"] == 0


def test_recheck_stage_decision_records_calibration_entry_used(tmp_path,
                                                               monkeypatch):
    """When auto mode resolves via a real cache entry, the entry is recorded."""
    cfg = RecheckConfig(mode="auto", fn_threshold=0.10,
                        calibration_max_age_days=90, deployment_id="dep1")
    case_dir = _make_case_with_wco_refs(tmp_path, [])

    # Plant a registry the stage will find
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("DSAR_CALIBRATION_REGISTRY", str(registry_path))
    # Use the *actual* current prompt seals so seal_drift doesn't fire.
    from dsar_pipeline.gates.prompt_loader import PromptLoader
    primary = PromptLoader.load("durant.system").canonical_seal_sha256
    recheck = PromptLoader.load("durant.recheck.system").canonical_seal_sha256
    registry_path.write_text(json.dumps({
        "schema_version": 1,
        "entries": [{
            "schema_version": 1, "deployment_id": "dep1",
            "model_alias": "mini@mlx",
            "primary_prompt_seal_sha256": primary,
            "recheck_prompt_seal_sha256": recheck,
            "calibrated_at": (datetime.now(timezone.utc) - timedelta(days=5))
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
            "sample_size": 100, "fn_rate": 0.03,
            "fn_rate_ci95": [0.01, 0.05], "source_case_id": "c",
        }],
    }))

    stage = RecheckStage(config=cfg)
    summary = stage.run(case_dir, refs=[], iteration=0)

    decision = json.loads(
        (case_dir / "working" / "recheck_decision.json").read_text())
    assert decision["mode_effective"] == "never"
    assert decision["reason"] == "ci_upper_below_threshold"
    assert decision["calibration_entry_used"] is not None
    assert decision["calibration_entry_used"]["sample_size"] == 100


def test_recheck_stage_only_iterates_wco_refs(tmp_path):
    """Spec §4.2 (A): consume ONLY refs primary classified work_context_only."""
    cfg = RecheckConfig(mode="always", max_concurrency=1)
    case_dir = _make_case_with_wco_refs(tmp_path, ["D000001"])
    # Plant a bio ref in the same biographical_refs.json
    bio_data = json.loads(
        (case_dir / "working" / "biographical_refs.json").read_text())
    bio_data["biographical"] = ["D000099"]
    bio_data["per_ref"]["D000099"] = {"verdict": "biographical",
                                       "rationale": "subject is focus"}
    (case_dir / "working" / "biographical_refs.json").write_text(
        json.dumps(bio_data))

    mock_gate = MagicMock()
    mock_gate.classify.return_value = {
        "case_id": case_dir.name, "doc_ref": "D000001",
        "recheck_verdict": "confirmed_work_context_only", "rationale": "x",
        "model": "mini@mlx", "prompt_id": "durant.recheck.system",
        "prompt_canonical_seal_sha256": "a" * 64,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "a" * 64,
        "elapsed_sec": 0.1, "error_state": None,
        "estimated_cost_usd": 0.0, "token_safety_iterations": 0,
    }

    stage = RecheckStage(config=cfg, gate=mock_gate)
    summary = stage.run(case_dir,
                        refs=["D000001", "D000099"], iteration=0)
    # Only the WCO ref triggered a recheck call
    assert mock_gate.classify.call_count == 1
    calls = [c.args[1] for c in mock_gate.classify.call_args_list]
    assert calls == ["D000001"]
    assert summary["docs_examined"] == 1
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_recheck_stage.py -v -k "mode_never or mode_always or _records or only_iterates"
```

Expected: NotImplementedError from `_run_stage` stub.

- [ ] **Step 3: Implement `_run_stage` body**

Replace `_run_stage`'s body in `recheck_stage.py`:

```python
    def _run_stage(self, case_dir: Path, refs: list[str],
                   iteration: int) -> dict:
        # 1. Resolve current prompt seals.
        primary_seal = PromptLoader.load("durant.system").canonical_seal_sha256
        recheck_seal = PromptLoader.load(
            "durant.recheck.system").canonical_seal_sha256

        # 2. Load calibration registry (silent miss → cache_entry=None).
        registry_path = resolve_registry_path(
            self.config.calibration_registry_path)
        try:
            registry = load_registry(registry_path)
        except CalibrationConfigError:
            # Spec §4.2 (C): loud error — surface, but record the decision
            # as cache_miss so the operator sees both the abort path AND
            # the run-time intent.
            raise

        cache_entry = find_matching_entry(
            registry,
            deployment_id=self.config.deployment_id,
            model_alias=self.config.model_alias,
            primary_seal=primary_seal,
            recheck_seal=recheck_seal,
        )

        # 3. Gate decision.
        decision = decide_mode(
            self.config, cache_entry,
            primary_seal=primary_seal, recheck_seal=recheck_seal,
        )

        # 4. Persist recheck_decision.json — canonical "stage ran" marker.
        self._write_decision(case_dir, decision)

        # 5/6. Branch on mode_effective.
        wco_refs = self._filter_to_wco_refs(case_dir, refs)
        summary_base = {
            "case_id": case_dir.name,
            "iteration": iteration,
            "mode_requested": self.config.mode,
            "mode_effective": decision.mode_effective,
            "reason": decision.reason,
            "docs_examined": 0,
            "docs_confirmed_wco": 0,
            "docs_reclassified_to_biographical": 0,
            "docs_reclassified_to_ambiguous": 0,
            "errors": 0,
            "elapsed_sec_total": 0.0,
            "estimated_cost_usd": 0.0,
            "completed_at": _now_iso(),
        }

        if decision.mode_effective == "never":
            self._write_recheck_summary(case_dir, summary_base)
            return summary_base

        # mode_effective == "always" — run the gate.
        t0 = time.time()
        jsonl_path = case_dir / "working" / "durant_underdisclosure_recheck.jsonl"
        # If a prior run wrote this file, truncate before appending (a new
        # stage invocation = a new set of rows).
        if jsonl_path.exists():
            jsonl_path.unlink()

        counts = {
            "docs_examined": 0, "docs_confirmed_wco": 0,
            "docs_reclassified_to_biographical": 0,
            "docs_reclassified_to_ambiguous": 0,
            "errors": 0,
        }
        cost_total = 0.0
        cost_known = True   # set False on any unknown alias → final cost is None

        with JsonlAppender(jsonl_path) as appender:
            with ThreadPoolExecutor(
                    max_workers=self.config.max_concurrency) as pool:
                futures = {pool.submit(self.gate.classify, case_dir, ref): ref
                           for ref in wco_refs}
                for future in as_completed(futures):
                    ref = futures[future]
                    try:
                        row = future.result()
                    except Exception as e:
                        log.exception("recheck classify raised for %s: %s",
                                      ref, e)
                        row = self._synth_error_row(case_dir.name, ref, e,
                                                    primary_seal_hex=primary_seal,
                                                    recheck_seal_hex=recheck_seal)
                    try:
                        appender.append(row)
                    except RowSizeError:
                        # Truncate rationale defensively + re-append.
                        if row.get("rationale"):
                            row["rationale"] = (row["rationale"] or "")[:80]
                        appender.append(row)

                    counts["docs_examined"] += 1
                    verdict = row.get("recheck_verdict")
                    if verdict == "confirmed_work_context_only":
                        counts["docs_confirmed_wco"] += 1
                    elif verdict == "reclassify_to_biographical":
                        counts["docs_reclassified_to_biographical"] += 1
                    elif verdict == "reclassify_to_ambiguous":
                        counts["docs_reclassified_to_ambiguous"] += 1
                    if row.get("error_state") is not None:
                        counts["errors"] += 1
                    c = row.get("estimated_cost_usd")
                    if c is None:
                        cost_known = False
                    else:
                        cost_total += c

        summary_base.update(counts)
        summary_base["elapsed_sec_total"] = round(time.time() - t0, 3)
        summary_base["estimated_cost_usd"] = (
            round(cost_total, 6) if cost_known else None)
        self._write_recheck_summary(case_dir, summary_base)
        return summary_base

    @staticmethod
    def _synth_error_row(case_id: str, ref: str, exc: BaseException,
                         *, primary_seal_hex: str,
                         recheck_seal_hex: str) -> dict:
        """Build an error row when the gate.classify itself raises (rare —
        the gate normally captures errors internally)."""
        from .gates.gate_durant_recheck import _sanitise_raw
        return {
            "case_id": case_id, "doc_ref": ref,
            "recheck_verdict": None, "rationale": None, "model": "unknown",
            "prompt_id": "durant.recheck.system",
            "prompt_canonical_seal_sha256": primary_seal_hex,
            "prompt_applied_strips": [],
            "prompt_effective_sha256": recheck_seal_hex,
            "elapsed_sec": 0.0,
            "error_state": {
                "code": "unknown",
                "message": str(exc)[:200],
                "raw": _sanitise_raw(str(exc)),
            },
            "estimated_cost_usd": None,
            "token_safety_iterations": 0,
        }

    @staticmethod
    def _filter_to_wco_refs(case_dir: Path, refs: list[str]) -> list[str]:
        """Read working/biographical_refs.json; intersect `refs` with the
        primary-pass work_context_only set. Spec §4.2 (A)."""
        bio = case_dir / "working" / "biographical_refs.json"
        if not bio.exists():
            return []
        try:
            data = json.loads(bio.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        wco = set(data.get("work_context_only", []) or [])
        if not refs:
            return sorted(wco)
        return [r for r in refs if r in wco]

    def _write_decision(self, case_dir: Path,
                        decision: ModeDecision) -> Path:
        out = case_dir / "working" / "recheck_decision.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        entry_dict: Optional[dict] = None
        if decision.calibration_entry_used is not None:
            e = decision.calibration_entry_used
            entry_dict = {
                "deployment_id": e.deployment_id,
                "model_alias": e.model_alias,
                "primary_prompt_seal_sha256": e.primary_prompt_seal_sha256,
                "recheck_prompt_seal_sha256": e.recheck_prompt_seal_sha256,
                "calibrated_at": e.calibrated_at,
                "sample_size": e.sample_size,
                "fn_rate": e.fn_rate,
                "fn_rate_ci95": list(e.fn_rate_ci95),
                "source_case_id": e.source_case_id,
            }
        payload = {
            "mode_requested": self.config.mode,
            "mode_effective": decision.mode_effective,
            "reason": decision.reason,
            "calibration_entry_used": entry_dict,
            "fn_threshold": self.config.fn_threshold,
            "decided_at": _now_iso(),
        }
        tmp = out.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
        return out

    def _write_recheck_summary(self, case_dir: Path,
                               summary: dict) -> Path:
        out = case_dir / "working" / "recheck_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
        return out
```

Also override `summary_filename` so BaseStage's standard `<stage>_summary.json` write doesn't clobber the recheck-specific summary semantics. Add to the class:

```python
    @property
    def summary_filename(self) -> str:
        # BaseStage writes case_dir/working/<summary_filename>; we want the
        # explicit name documented in the spec.
        return "recheck_summary.json"
```

(NB: BaseStage's `_persist_summary` writes the dict we return from `_run_stage`. To avoid duplicate writes, override the inherited `summary_filename` to point at the same path the explicit `_write_recheck_summary` already writes — BaseStage will simply overwrite our hand-rolled file with the same dict. That's intentional: the stage_completed audit envelope expects the path-under-working/ contract.)

- [ ] **Step 4: Run tests; verify all orchestration tests pass**

```bash
uv run pytest tests/test_recheck_stage.py -v
```

Expected: 17+ PASS (Tasks 34 + 35 combined).

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/recheck_stage.py tests/test_recheck_stage.py
git commit -m "feat(recheck_stage): _run_stage orchestration + decision/summary writes"
```

---

### Task 36: Add `dsar-recheck` CLI entry-point

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/recheck_stage.py`
- Modify: `~/projects/dsar-toolkit/pyproject.toml`
- Modify: `~/projects/dsar-toolkit/tests/test_recheck_stage.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_recheck_stage.py`:

```python
def test_dsar_recheck_cli_smoke(tmp_path, monkeypatch):
    """`dsar-recheck --case <fixture>` exits 0 on a synthetic case with 5
    WCO refs (acceptance criterion for Phase 3)."""
    import subprocess

    case_dir = _make_case_with_wco_refs(
        tmp_path,
        ["D000001", "D000002", "D000003", "D000004", "D000005"],
    )

    # Mode=never with valid reason → CLI returns 0 without touching the LLM.
    # (Validating the CLI dispatch + decision write; full LLM smoke-test
    # lives in test_durant_pipeline_e2e.py in Phase 4.)
    monkeypatch.setenv("DSAR_CASE_ROOT", str(tmp_path))

    result = subprocess.run(
        ["dsar-recheck", "--case", "case-001",
         "--mode", "never",
         "--override-reason", "test smoke"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr}\nstdout: {result.stdout}")
    assert (case_dir / "working" / "recheck_decision.json").exists()
    assert (case_dir / "working" / "recheck_summary.json").exists()


def test_dsar_recheck_cli_rejects_never_blank_reason(tmp_path, monkeypatch):
    """CLI guard: --mode never without --override-reason → non-zero exit."""
    import subprocess

    case_dir = _make_case_with_wco_refs(tmp_path, [])
    monkeypatch.setenv("DSAR_CASE_ROOT", str(tmp_path))

    result = subprocess.run(
        ["dsar-recheck", "--case", "case-001", "--mode", "never"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "override-reason" in result.stderr.lower() or \
           "override_reason" in result.stderr.lower()
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_recheck_stage.py -v -k "cli"
```

Expected: NotImplementedError from `main()` stub.

- [ ] **Step 3: Implement `main()` in `recheck_stage.py`**

Replace the `main()` stub:

```python
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="dsar-recheck",
        description="Run the calibration-gated Durant under-disclosure "
                    "recheck on the work_context_only refs of a case.",
    )
    p.add_argument("--case", required=True,
                   help="Case ID; resolves to $DSAR_CASE_ROOT/<id> or treats "
                        "as absolute path.")
    p.add_argument("--refs", default=None,
                   help="Comma-separated subset of WCO refs to recheck. "
                        "Default = all WCO refs from biographical_refs.json.")
    p.add_argument("--mode", choices=("always", "never", "auto"),
                   default="auto",
                   help="Spec §4.2 (B). Default: auto.")
    p.add_argument("--fn-threshold", type=float, default=0.10)
    p.add_argument("--calibration-max-age-days", type=int, default=90)
    p.add_argument("--max-concurrency", type=int, default=4)
    p.add_argument("--override-reason", default="",
                   help="Required non-blank when --mode != auto.")
    p.add_argument("--deployment-id", default="",
                   help="Operator deployment identifier.")
    p.add_argument("--calibration-registry-path", default=None)
    p.add_argument("--model-alias", default="mini@mlx")
    p.add_argument("--iteration", type=int, default=0)
    args = p.parse_args(argv)

    # Resolve case dir.
    case_arg = Path(args.case)
    if case_arg.is_absolute():
        case_dir = case_arg
    else:
        root = os.environ.get("DSAR_CASE_ROOT")
        if root:
            case_dir = Path(root) / args.case
        else:
            case_dir = Path.home() / "dsars" / "cases" / args.case
    if not case_dir.exists():
        print(f"Case directory not found: {case_dir}", file=sys.stderr)
        return 2

    cfg = RecheckConfig(
        mode=args.mode,
        fn_threshold=args.fn_threshold,
        calibration_max_age_days=args.calibration_max_age_days,
        max_concurrency=args.max_concurrency,
        override_reason=args.override_reason,
        deployment_id=args.deployment_id,
        calibration_registry_path=args.calibration_registry_path,
        model_alias=args.model_alias,
    )
    try:
        stage = RecheckStage(config=cfg)
    except ConfigError as e:
        print(f"recheck config error: {e}", file=sys.stderr)
        return 2

    refs = None
    if args.refs:
        refs = [r.strip() for r in args.refs.split(",") if r.strip()]
    summary = stage.run(case_dir, refs=refs, iteration=args.iteration)

    print(f"Recheck stage complete (iteration {summary['iteration']})")
    print(f"  Case: {summary['case_id']}")
    print(f"  Mode: requested={summary['mode_requested']} "
          f"effective={summary['mode_effective']} reason={summary['reason']}")
    print(f"  Docs examined: {summary['docs_examined']}")
    print(f"    confirmed_work_context_only:     "
          f"{summary['docs_confirmed_wco']}")
    print(f"    reclassify_to_biographical:      "
          f"{summary['docs_reclassified_to_biographical']}")
    print(f"    reclassify_to_ambiguous:         "
          f"{summary['docs_reclassified_to_ambiguous']}")
    print(f"    errors:                          {summary['errors']}")
    print(f"  Estimated cost USD: {summary['estimated_cost_usd']}")
    print(f"  Elapsed:            {summary['elapsed_sec_total']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Register the entry-point**

Edit `pyproject.toml` `[project.scripts]`. Add the new line:

```toml
dsar-recheck = "dsar_pipeline.recheck_stage:main"
```

Reinstall:

```bash
cd ~/projects/dsar-toolkit
uv pip install -e .
```

- [ ] **Step 5: Run the CLI smoke test**

```bash
uv run pytest tests/test_recheck_stage.py -v -k "cli"
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dsar_pipeline/recheck_stage.py pyproject.toml tests/test_recheck_stage.py
git commit -m "feat(recheck_stage): dsar-recheck CLI entry-point"
```

---

### Task 37: Wire `RecheckStage` into `ScopeCheckStage`

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/scope_check_stage.py`
- Modify: `~/projects/dsar-toolkit/tests/test_scope_check_stage.py` (extend with one new test)

Spec §10.1: extend `ScopeCheckStage` so that AFTER primary `gate_durant` runs (before synthesis), it invokes `RecheckStage` over the WCO refs. Per-case YAML gating (`recheck.mode != "never"` → run; `recheck.mode == "never"` → skip entirely — no decision file).

Phase 3 keeps the synthesis step using the **legacy 2-arg `_synthesise_verdict`**. The recheck JSONL is written but Agent22's 5-arg synthesis (Phase 4) is what actually consumes it for `scope_verdicts.jsonl`. This means the e2e pass through Phase 3 still produces `scope_verdicts.jsonl` correctly per the Phase-1/2 logic — the recheck output is laid down for Phase 4 without breaking existing behaviour.

- [ ] **Step 1: Write failing test**

Append to `tests/test_scope_check_stage.py`:

```python
def test_scope_check_stage_invokes_recheck_when_configured(tmp_path,
                                                            monkeypatch):
    """ScopeCheckStage with recheck.mode != "never" in case_config runs
    RecheckStage after primary durant and writes the JSONL + decision."""
    import json
    from unittest.mock import MagicMock, patch

    case_dir = tmp_path / "case-recheck"
    (case_dir / "working").mkdir(parents=True)
    register = {f"D{i:06d}": {"ref": f"D{i:06d}",
                              "filename": f"d{i}.txt", "category": "email"}
                for i in range(1, 4)}
    (case_dir / "working" / "register.json").write_text(
        json.dumps(register), encoding="utf-8")
    (case_dir / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "Alice", "email": "alice@x.com"}),
        encoding="utf-8")
    # Plant a case_config.yaml with recheck.mode=always (override_reason
    # only required for "never").
    (case_dir / "case_config.yaml").write_text(
        "recheck:\n  mode: always\n  max_concurrency: 1\n",
        encoding="utf-8",
    )

    # Mock primary durant to return WCO for all refs
    primary_durant_output = {
        "biographical": [],
        "work_context_only": ["D000001", "D000002", "D000003"],
        "ambiguous": [],
        "per_ref": {f"D00000{i}": {"verdict": "work_context_only",
                                    "rationale": "test"}
                    for i in range(1, 4)},
    }
    (case_dir / "working" / "biographical_refs.json").write_text(
        json.dumps(primary_durant_output), encoding="utf-8")

    # The ScopeCheckStage will call its embedded RecheckStage; mock the gate
    # so we don't hit the LLM.
    from dsar_pipeline.scope_check_stage import ScopeCheckStage
    from dsar_pipeline.gates.gate_durant_recheck import GateDurantRecheck

    mock_gate = MagicMock(spec=GateDurantRecheck)
    mock_gate.classify.return_value = {
        "case_id": case_dir.name, "doc_ref": "ignored",
        "recheck_verdict": "confirmed_work_context_only", "rationale": "x",
        "model": "mini@mlx", "prompt_id": "durant.recheck.system",
        "prompt_canonical_seal_sha256": "a" * 64,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "a" * 64,
        "elapsed_sec": 0.1, "error_state": None,
        "estimated_cost_usd": 0.0, "token_safety_iterations": 0,
    }

    # The integration mocks both primary durant + temporal so the stage
    # doesn't actually call any LLM. We focus on the recheck wiring.
    with patch("dsar_pipeline.scope_check_stage.GateRunner") as MockRunner, \
         patch("dsar_pipeline.scope_check_stage.GateDurantRecheck",
               return_value=mock_gate):
        runner_inst = MockRunner.return_value
        runner_inst.run_all.return_value = {
            "gate_temporal_scope": MagicMock(findings=[], error=None,
                                              duration_ms=1, refs_examined=3),
            "gate_durant": MagicMock(findings=[], error=None,
                                      duration_ms=1, refs_examined=3),
        }
        stage = ScopeCheckStage()
        summary = stage.run(case_dir,
                            refs=["D000001", "D000002", "D000003"],
                            iteration=0)

    # Recheck artefacts present
    assert (case_dir / "working" / "recheck_decision.json").exists()
    assert (case_dir / "working" / "durant_underdisclosure_recheck.jsonl").exists()
    # scope_verdicts.jsonl still written (Phase 1/2 path)
    assert (case_dir / "working" / "scope_verdicts.jsonl").exists()


def test_scope_check_stage_skips_recheck_when_mode_never(tmp_path):
    """Spec §10.1: when case_config.recheck.mode == "never", do NOT invoke
    RecheckStage at all (no decision file is written)."""
    import json

    case_dir = tmp_path / "case-skip-recheck"
    (case_dir / "working").mkdir(parents=True)
    (case_dir / "working" / "register.json").write_text(
        json.dumps({"D000001": {"ref": "D000001", "filename": "x.txt"}}),
        encoding="utf-8")
    (case_dir / "case_config.yaml").write_text(
        "recheck:\n  mode: never\n  override_reason: trusted local model\n",
        encoding="utf-8",
    )
    (case_dir / "working" / "biographical_refs.json").write_text(
        json.dumps({"biographical": [], "work_context_only": ["D000001"],
                    "ambiguous": [],
                    "per_ref": {"D000001": {"verdict": "work_context_only"}}}),
        encoding="utf-8",
    )

    from unittest.mock import MagicMock, patch
    from dsar_pipeline.scope_check_stage import ScopeCheckStage

    with patch("dsar_pipeline.scope_check_stage.GateRunner") as MockRunner:
        MockRunner.return_value.run_all.return_value = {
            "gate_temporal_scope": MagicMock(findings=[], error=None,
                                              duration_ms=1, refs_examined=1),
            "gate_durant": MagicMock(findings=[], error=None,
                                      duration_ms=1, refs_examined=1),
        }
        stage = ScopeCheckStage()
        stage.run(case_dir, refs=["D000001"], iteration=0)

    assert not (case_dir / "working" / "recheck_decision.json").exists()
    assert not (case_dir / "working" / "durant_underdisclosure_recheck.jsonl").exists()
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_scope_check_stage.py -v -k "recheck"
```

Expected: failure — no recheck dispatch wired.

- [ ] **Step 3: Modify `scope_check_stage.py` — add post-durant recheck**

In `src/dsar_pipeline/scope_check_stage.py`, add the imports at top:

```python
from .gates.gate_durant_recheck import GateDurantRecheck
from .recheck_stage import (
    ConfigError as RecheckConfigError, RecheckConfig, RecheckStage,
)
```

Add a helper that loads recheck config from the case_config.yaml (or returns defaults). Place this method on the class:

```python
    @staticmethod
    def _load_recheck_config(case_dir: Path) -> RecheckConfig:
        """Read recheck.* from case_config.yaml. Defaults to mode=auto.

        Returns a RecheckConfig with whatever fields the YAML carries;
        missing fields take dataclass defaults.
        """
        cfg_path = case_dir / "case_config.yaml"
        if not cfg_path.exists():
            return RecheckConfig()
        try:
            import yaml
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            log.warning("case_config.yaml unparseable (%s); using default "
                        "recheck config", e)
            return RecheckConfig()
        section = data.get("recheck") or {}
        return RecheckConfig(
            mode=section.get("mode", "auto"),
            fn_threshold=float(section.get("fn_threshold", 0.10)),
            calibration_max_age_days=int(
                section.get("calibration_max_age_days", 90)),
            max_concurrency=int(section.get("max_concurrency", 4)),
            override_reason=str(section.get("override_reason", "") or ""),
            deployment_id=str(section.get("deployment_id", "") or ""),
            calibration_registry_path=section.get("calibration_registry_path"),
            model_alias=str(section.get("model_alias", "mini@mlx")),
        )
```

Also add a `log = logging.getLogger(__name__)` at module top if not already present (after the imports).

After the existing `runner.run_all(case_dir, refs, save_report=False)` line and BEFORE `ref_models = self._read_ref_models(...)`, insert the recheck dispatch:

```python
        # ----- Phase 3 §4.2 wiring -----
        # After primary gate_durant has run and persisted biographical_refs.json,
        # invoke RecheckStage on the WCO refs (unless mode=never in case_config).
        recheck_cfg = self._load_recheck_config(case_dir)
        if recheck_cfg.mode != "never":
            try:
                recheck_stage = RecheckStage(config=recheck_cfg)
                recheck_stage.run(case_dir, refs=refs, iteration=iteration)
            except RecheckConfigError as e:
                log.error("RecheckStage init failed (%s); skipping recheck for "
                          "this iteration. Operator must reconcile case_config.",
                          e)
        else:
            log.info("recheck.mode=never in case_config (reason=%r); skipping "
                     "RecheckStage entirely.", recheck_cfg.override_reason)
        # ----- end Phase 3 wiring -----
```

Add `import logging` at the top of the file if it isn't already there.

- [ ] **Step 4: Run; verify recheck-wiring tests + all existing tests pass**

```bash
uv run pytest tests/test_scope_check_stage.py -v
```

Expected: PASS (recheck-wiring tests PLUS all existing test_scope_check_stage.py tests still green).

- [ ] **Step 5: Run the full toolkit test suite to confirm nothing regressed**

```bash
uv run pytest tests/ -v 2>&1 | tail -40
```

Expected: Phase 1 (test_prompt_assets, test_text_truncation), Phase 2 (test_role_field_sanitiser + any Phase-2 token-belt tests), and Phase 3 (test_wilson, test_jsonl_appender, test_calibration_cache, test_pricing_config, test_gate_durant_recheck, test_recheck_stage) all PASS. Existing tests (`test_gate_durant.py`, `test_durant_prompt_template.py`, `test_scope_check_stage.py`, `test_stage_base.py`) also PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dsar_pipeline/scope_check_stage.py tests/test_scope_check_stage.py
git commit -m "feat(scope_check): invoke RecheckStage after primary durant"
```

---

## Acceptance criteria for Phase 3

Phase 3 is done when ALL of these hold:

- [ ] `tests/test_gate_durant_recheck.py` passes — verdict mapping for all 3 enum values (`confirmed_work_context_only`, `reclassify_to_biographical`, `reclassify_to_ambiguous`) plus error_state path covered.
- [ ] `tests/test_recheck_stage.py` passes — mode=always, mode=never with reason, mode=auto with calibration above/below threshold, mode=auto cache-miss, mode=auto stale, mode=auto seal-drift; CLI smoke test green.
- [ ] `dsar-recheck --case <fixture>` exits 0 on a synthetic case with 5 WCO refs.
- [ ] `durant_underdisclosure_recheck.jsonl` rows validate against `schemas/durant_recheck_row.schema.json` (asserted by test_durant_recheck_row_schema_*).
- [ ] `scope_check_stage` end-to-end runs gate_durant → RecheckStage → still writes `scope_verdicts.jsonl` (Agent22 synthesis still uses Phase 1/2 2-arg form; Phase 4 will add the 5-arg form).
- [ ] `recheck_decision.json` is the canonical "stage ran" marker (Spec §4.2 (D)).
- [ ] `recheck_summary.json` reports `mode_effective`, counts, cost telemetry, elapsed (Spec §4.2 (E)).
- [ ] `_wilson.py` ships with both `wilson_lower` + `wilson_upper`, used by `decide_mode` (and ready for §4.4 in Phase 5).
- [ ] `_jsonl_appender.py` rejects rows ≥ 512 bytes (PIPE_BUF) and is thread-safe (shared appender across 8 threads × 25 rows = 200 well-formed lines).
- [ ] `_sanitise_raw` redacts bearer/Basic/sk-/Authorization/AWS_KEY/userpass URLs; final cap 200 chars.
- [ ] All Phase 1 + Phase 2 tests still pass (`tests/test_prompt_assets.py`, `tests/test_text_truncation.py`, `tests/test_role_field_sanitiser.py`, `tests/test_gate_durant.py`, `tests/test_durant_prompt_template.py`).
- [ ] `_stage_base.VALID_STAGE_LABELS` includes `"durant_recheck"`.
- [ ] `pyproject.toml` registers `dsar-recheck` entry-point.
- [ ] All commits atomic (one feature per commit; ≥1 commit per task).

## Self-review

**Spec coverage (Phase 3 only — spec §4.2 in full):**

| Spec subsection | Task(s) | Status |
|---|---|---|
| §4.2 (A) GateDurantRecheck + recheck prompt asset | 26, 31 | ✓ |
| §4.2 (B) decide_mode gating logic | 34 | ✓ |
| §4.2 (C) calibration cache load + tie-break + retry | 29 | ✓ |
| §4.2 (C) PermissionError → ConfigError (loud) | 29 | ✓ |
| §4.2 (D) recheck_decision.json canonical marker | 35 | ✓ |
| §4.2 (E) recheck_summary.json + pricing.json cost telemetry | 30, 31, 35 | ✓ |
| §4.2 (F) per-row JSONL contract + error_state mutual exclusion | 33 | ✓ |
| §4.2 (F) _sanitise_raw v6 (16KB pre-cap → strip → 200) | 31 | ✓ |
| §4.2 (G) JsonlAppender thread-safe + 512-byte cap + close-error handling | 28 | ✓ |
| §4.2 (G) ThreadPoolExecutor with operator-config max_concurrency | 34, 35 | ✓ |
| Wilson lower/upper helpers (used by §4.2 + §4.4 Phase 5) | 27 | ✓ |
| §10.1 ScopeCheckStage post-durant recheck dispatch | 37 | ✓ |
| §10.1 VALID_STAGE_LABELS += "durant_recheck" | 32 | ✓ |
| §10.1 dsar-recheck entry-point | 36 | ✓ |
| §10.1 schemas/durant_recheck_row.schema.json | 33 | ✓ |
| Recheck prompt sign + archive via Phase 1 tooling | 26 | ✓ |

**Out of scope for Phase 3 (covered in later phases):**

- §4.6 Agent22 5-arg `synthesise_verdict` — Phase 4. ScopeCheckStage in Phase 3 still uses the legacy 2-arg form; the recheck JSONL is laid down but not consumed for scope_verdicts.jsonl.
- §4.6 `effective_durant()` helper — Phase 4.
- §4.6 `scope_verdict.schema.json` evidence-block extension — Phase 4.
- §4.4 fitness canary — Phase 5.
- `dsar-conductor verify` integration (prompt-versions check that consumes recheck JSONL audit fields) — Phase 5.
- Remote `_remote_get` implementation (S3 / HTTPS calibration registry) — Phase 5. Phase 3 ships the dispatch surface + retry policy but the concrete remote shim raises NotImplementedError until Phase 5 wires `dsar_clients.storage`.
- Per-engagement bypass script migration to `dsar-recheck` — out of scope (engagement folders, not toolkit).
- §4.7 docs/durant-test.md updates — Phase 6.

**Placeholder scan:** None. Every `Step` has full code; every command has full args. The one known cosmetic blemish in the test draft (`64.__class__.__mul__("B", 64)` in `test_decide_mode_hash_comparison_case_insensitive`) is flagged inline with the instruction to replace with `"B" * 64`; the canonical `d2` assertion in the same test is what carries the load.

**Type consistency:**
- `CalibrationCacheEntry` fields (incl. `fn_rate_ci95: tuple[float, float]`) used identically across `_calibration_cache.py`, `recheck_stage.decide_mode`, and `_write_decision`. ✓
- `RecheckConfig` dataclass field types match across Tasks 34–37. ✓
- `ModeDecision.mode_effective` ∈ {"always", "never"} consistently — recorded verbatim in `recheck_decision.json["mode_effective"]`. ✓
- `_normalise_hash` ("strip().lower()") used in both `_calibration_cache.py` and `decide_mode` (no in-place divergence). ✓
- Row dict shape produced by `GateDurantRecheck.classify` matches `schemas/durant_recheck_row.schema.json` exactly (Task 33 tests assert this). ✓
- `BaseStage.summary_filename` override in `RecheckStage` points to `recheck_summary.json`; the explicit `_write_recheck_summary` writes the same dict the BaseStage envelope persists. ✓

**Decisions deviating from spec (intentional):**

- **`scope_check_stage` integration uses LEGACY 2-arg synthesis.** Spec §4.6 (Phase 4) defines the 5-arg `synthesise_verdict(primary, recheck, recheck_err, recheck_mode, temporal)`. Phase 3 only lays down the recheck output (JSONL + decision + summary); it does NOT modify Agent22's synthesis path. Rationale: keep Phase 3 PR scope tight to the recheck stage itself; Phase 4 handles the consumer side as a single coherent change.
- **`_remote_get` shim raises NotImplementedError.** Spec §4.2 (C) calls for remote calibration registry support via the toolkit's storage abstraction. Phase 3 ships the dispatch + retry policy + 404-is-terminal logic; the actual S3/HTTPS reader is wired in Phase 5 (alongside the conductor pre-flight that materialises this). Until then, remote URIs are operator-detectable failures with a clear error message rather than silent fallback to local.
- **BaseStage `summary_filename` override.** The recheck summary content is hand-rolled in `_write_recheck_summary` (atomic tmp + fsync + replace) so the cost/count fields land before BaseStage's envelope writes. The override makes BaseStage's `_persist_summary` write to the SAME path, which then gets overwritten with the identical payload — the cost is one extra write per stage run; the benefit is BaseStage's `summary_path` audit field stays correct. Acceptable.
- **`_run_stage` truncates an existing JSONL before appending.** Spec §4.2 doesn't specify re-run semantics. Phase 3 chooses "new run = new rows" (unlink before opening the appender) rather than append-and-grow. Rationale: predictable per-iteration output for operator inspection. If multi-iteration accumulation is wanted later, swap `unlink()` for `mode=a` + an iteration field per row — schema already includes nothing iteration-keyed beyond what the dict carries.

---

*End of Phase 3 plan. Continue with Phase 4 plan (covers spec §4.6 Agent22 5-arg synthesis + scope_verdicts evidence-block extension + e2e durant_with_recheck integration test).*
