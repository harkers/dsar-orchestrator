# Durant Pipeline Hardening — Phase 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Scope of this plan: Phase 4 only.** The 7-section spec is being implemented across 5 phase plans (matches the design spec §10.5 phasing). This plan covers Phase 4 (§4.6 — recheck → `scope_verdicts.jsonl` synthesis in `Agent22ScopeCheck`). Preceding plans (already landed):
>
> - Phase 1 plan: §4.1 prompt asset + loader; §4.3 head_tail truncation + subject-mention scan
> - Phase 2 plan: §4.3 token safety belt + `RoleRouter` additions + `GateDurant` truncation integration; §4.5 role field sanitiser + prompt template change
> - Phase 3 plan: §4.2 recheck stage (`GateDurantRecheck`, `RecheckStage`, `dsar-recheck` CLI, `working/durant_underdisclosure_recheck.jsonl`, `working/recheck_decision.json`, calibration cache READ)
>
> Subsequent phase plans:
>
> - Phase 5 plan: §4.4 fitness canary + conductor pre-flight + `dsar-conductor verify --check prompt-versions`; vendored zipapp build
> - Phase 6 plan: §4.7 durant-test.md updates + CI lint (`tools/check_durant_doc.py`)
>
> Each phase plan stands on its own. Phase 4 depends on Phase 2 (truncation/role audit fields are referenced in the synthesis evidence block) and Phase 3 (recheck JSONL + `recheck_decision.json` are the inputs Agent22 reads). Phase 4 unblocks Phase 5's end-to-end canary fixture (which exercises the synthesis path).

**Goal:** Land the §4.6 synthesis layer that wires Phase 3's recheck output into `scope_verdicts.jsonl`. By end of Phase 4: `Agent22ScopeCheck` reads `working/durant_verdicts.jsonl` (the Phase 2 dual-write), `working/durant_underdisclosure_recheck.jsonl` (Phase 3 output), `working/temporal_verdicts.jsonl`, and `working/recheck_decision.json`; emits one `scope_verdicts.jsonl` row per primary ref with an extended `evidence` block (incl. `recheck_verdict`, `error_state`, `recheck_mode_effective`, `effective_durant`); also writes `working/synthesis_summary.json` for cost/decision telemetry. The end-to-end safety net is now wired: a primary `work_context_only` verdict reclassified by recheck reaches the redaction stage as `scope_verdict=present`. The existing 2-arg `_synthesise_verdict(durant, temporal)` form is retained as a `DeprecationWarning`-emitting shim for out-of-tree callers.

**Architecture:** Modified files: `agents/agent22_scope_check.py` (new 5-arg `synthesise_verdict` + `effective_durant` helper + orchestration in `Agent22ScopeCheck.run`); `schemas/scope_verdict.schema.json` (extend `evidence` block). New file (toolkit-side): none — synthesis lives in the existing agent module. New tests: `tests/test_agent22_synthesis.py`. All existing tests must keep passing: `tests/test_scope_decisions.py`, `tests/test_gate_durant.py`, `tests/test_durant_prompt_template.py`, `tests/test_recheck_stage.py`, `tests/test_gate_durant_recheck.py`.

**Tech Stack:** Python 3.11+ (`typing` features used); no new third-party deps. Existing toolkit deps (`pytest`, `jsonschema`) suffice.

---

## File structure

### dsar-toolkit (modifies 2 files; creates 1 test file)

```
src/dsar_pipeline/
├── agents/
│   └── agent22_scope_check.py               # MODIFY: add effective_durant, synthesise_verdict (5-arg),
│                                              #         helpers (_safe_extract_error_code, _normalise_primary,
│                                              #         _truncate_any, _trim_error_state, _iter_jsonl_safe,
│                                              #         _build_index_first_wins), SynthesisSummary class,
│                                              #         Agent22ScopeCheck.run orchestration, 2-arg shim
schemas/
└── scope_verdict.schema.json                # MODIFY: extend evidence block (recheck_verdict, error_state,
                                              #         recheck_mode_effective, effective_durant);
                                              #         backwards-compat (existing rows still validate).
tests/
└── test_agent22_synthesis.py                # CREATE — unit tests for the 9 new units above + e2e fixture
```

### dsar-orchestrator

No changes. The synthesis is internal to the toolkit; the orchestrator's `scope_classify` adapter is unaffected (still shells out to `dsar-scope-check`, which internally invokes `Agent22ScopeCheck`).

---

## Phase 4 — Agent22 synthesis

### Task 38: Extend `scope_verdict.schema.json` evidence block

**Files:**
- Modify: `~/projects/dsar-toolkit/schemas/scope_verdict.schema.json`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py` (create)

The current schema's `evidence` field is an **array** of typed evidence items (used by the LLM `scope_check` role for the older multi-evidence shape). Per spec §4.6 (D), the Agent22 synthesis row uses an **object** evidence block (durant_verdict / recheck_verdict / error_state / recheck_mode_effective / effective_durant / temporal_verdict). The schema must accept BOTH forms (existing array rows + new object rows) to preserve backwards compatibility with pre-§4.6 rows persisted before this phase. This is the only backwards-compat requirement called out in the Phase 4 acceptance criteria.

- [ ] **Step 1: Write failing test for the extended schema**

Create `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py` with the first test:

```python
"""Tests for §4.6 — Agent22 synthesis of scope_verdicts from primary
durant + recheck + temporal inputs.

Spec: docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md §4.6
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


_SCHEMA_PATH = (
    Path(__file__).parent.parent / "schemas" / "scope_verdict.schema.json"
)


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_accepts_legacy_array_evidence_row():
    """Pre-§4.6 rows with `evidence` as an array of typed items still validate."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    legacy_row = {
        "ref": "D000001",
        "verdict": "present",
        "rationale": "subject is direct addressee",
        "iteration": 0,
        "model": "claude-opus-4-7",
        "ts": "2026-05-26T10:00:00Z",
        "evidence": [
            {"type": "header", "quote": "To: Alice", "location": "line 1"},
        ],
    }
    errors = list(validator.iter_errors(legacy_row))
    assert errors == [], f"legacy row should validate, got: {errors}"


def test_schema_accepts_new_object_evidence_row():
    """Post-§4.6 rows with `evidence` as an object validate."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    new_row = {
        "ref": "D000002",
        "verdict": "present",
        "rationale": "effective_durant=present (recheck_reclassified_biographical)",
        "iteration": 0,
        "model": "claude-opus-4-7",
        "ts": "2026-05-26T10:00:00Z",
        "evidence": {
            "durant_verdict": "work_context_only",
            "recheck_verdict": "reclassify_to_biographical",
            "error_state": None,
            "recheck_mode_effective": "always",
            "effective_durant": "present",
            "temporal_verdict": "in_scope",
        },
    }
    errors = list(validator.iter_errors(new_row))
    assert errors == [], f"new row should validate, got: {errors}"


def test_schema_object_evidence_allows_nullable_recheck_fields():
    """recheck_verdict, error_state, temporal_verdict may all be null
    (e.g. when recheck_mode_effective == 'never')."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    row = {
        "ref": "D000003",
        "verdict": "not_present",
        "rationale": "effective_durant=not_present (primary_wco_recheck_disabled)",
        "iteration": 0,
        "model": "claude-opus-4-7",
        "ts": "2026-05-26T10:00:00Z",
        "evidence": {
            "durant_verdict": "work_context_only",
            "recheck_verdict": None,
            "error_state": None,
            "recheck_mode_effective": "never",
            "effective_durant": "not_present",
            "temporal_verdict": None,
        },
    }
    errors = list(validator.iter_errors(row))
    assert errors == [], f"nullable-fields row should validate, got: {errors}"


def test_schema_object_evidence_error_state_shape():
    """error_state, when non-null, has code+message and optional _extra."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    row = {
        "ref": "D000004",
        "verdict": "ambiguous",
        "rationale": "effective_durant=ambiguous (recheck_errored:model_unreachable)",
        "iteration": 0,
        "model": "claude-opus-4-7",
        "ts": "2026-05-26T10:00:00Z",
        "evidence": {
            "durant_verdict": "work_context_only",
            "recheck_verdict": None,
            "error_state": {
                "code": "model_unreachable",
                "message": "connection refused",
                "_extra": {"upstream": "litellm"},
            },
            "recheck_mode_effective": "always",
            "effective_durant": "ambiguous",
            "temporal_verdict": None,
        },
    }
    errors = list(validator.iter_errors(row))
    assert errors == [], f"error_state row should validate, got: {errors}"
```

- [ ] **Step 2: Run; verify failures**

```bash
cd ~/projects/dsar-toolkit
uv run pytest tests/test_agent22_synthesis.py -v -k "schema"
```

Expected: the legacy test PASSES (current schema allows it), but the new object-evidence tests FAIL because `evidence` is currently constrained to `type: array`.

- [ ] **Step 3: Extend the schema**

Edit `~/projects/dsar-toolkit/schemas/scope_verdict.schema.json` to make `evidence` accept either the legacy array form OR the new object form via `oneOf`. Replace the current `evidence` property block with:

```json
    "evidence": {
      "oneOf": [
        {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["type", "quote", "location"],
            "properties": {
              "type": { "enum": ["header", "body_named", "identifier", "action_subject", "alias", "absence_observed"] },
              "quote": { "type": "string" },
              "location": { "type": "string" }
            }
          }
        },
        {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "durant_verdict": {
              "type": ["string", "null"],
              "enum": ["biographical", "work_context_only", "ambiguous", "present", "not_present", null]
            },
            "recheck_verdict": {
              "type": ["string", "null"],
              "enum": ["reclassify_to_biographical", "reclassify_to_ambiguous", "confirmed_work_context_only", null]
            },
            "error_state": {
              "oneOf": [
                { "type": "null" },
                {
                  "type": "object",
                  "additionalProperties": false,
                  "required": ["code", "message"],
                  "properties": {
                    "code": { "type": "string" },
                    "message": { "type": "string" },
                    "_extra": { "type": "object" }
                  }
                }
              ]
            },
            "recheck_mode_effective": {
              "type": ["string", "null"],
              "enum": ["always", "never", null]
            },
            "effective_durant": {
              "type": ["string", "null"],
              "enum": ["present", "not_present", "ambiguous", null]
            },
            "temporal_verdict": {
              "type": ["string", "null"],
              "enum": ["in_scope", "out_of_scope", null]
            }
          }
        }
      ]
    },
```

Note: the `durant_verdict` enum intentionally accepts both the gate-level vocabulary (`biographical`/`work_context_only`/`ambiguous`) and the scope-axis vocabulary (`present`/`not_present`/`ambiguous`) — the synthesis records the AS-OBSERVED primary verdict, which is in gate-level form.

- [ ] **Step 4: Run the schema tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "schema"
```

Expected: all 4 schema tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/dsar-toolkit
git add schemas/scope_verdict.schema.json tests/test_agent22_synthesis.py
git commit -m "feat(scope_verdict): extend evidence to accept object form (§4.6)"
```

---

### Task 39: Implement `_safe_extract_error_code` defensive helper

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 (A): `effective_durant` calls `_safe_extract_error_code(recheck_err)` when an error is present. The recheck row's `error_state` field SHOULD be a dict with a `code` key (per §4.2's schema), but the synthesis code must defend against malformed/legacy inputs: dict without code key, plain string, None passed through unexpectedly, or any other type.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- _safe_extract_error_code -----------------------------------------------

from dsar_pipeline.agents.agent22_scope_check import _safe_extract_error_code


def test_safe_extract_error_code_from_dict_with_code():
    assert _safe_extract_error_code({"code": "model_unreachable", "message": "x"}) == "model_unreachable"


def test_safe_extract_error_code_from_dict_without_code():
    """Dict without a 'code' key → returns 'unknown'."""
    assert _safe_extract_error_code({"message": "no code here"}) == "unknown"


def test_safe_extract_error_code_from_string():
    """A bare string error → use the string itself (truncated if huge)."""
    assert _safe_extract_error_code("timeout") == "timeout"


def test_safe_extract_error_code_from_none_returns_unknown():
    assert _safe_extract_error_code(None) == "unknown"


def test_safe_extract_error_code_from_unexpected_type():
    """Numbers, lists, etc. → 'unknown'. Defensive."""
    assert _safe_extract_error_code(42) == "unknown"
    assert _safe_extract_error_code([1, 2, 3]) == "unknown"


def test_safe_extract_error_code_truncates_long_strings():
    long_code = "x" * 500
    result = _safe_extract_error_code(long_code)
    assert len(result) <= 100
    assert result.startswith("x")


def test_safe_extract_error_code_dict_code_not_string():
    """Defensive: dict has 'code' key but value isn't a string."""
    assert _safe_extract_error_code({"code": 12345}) == "unknown"
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "safe_extract"
```

Expected: `ImportError` (function doesn't exist yet).

- [ ] **Step 3: Implement `_safe_extract_error_code`**

At the top of `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`, after the existing imports (preserve `from .base import AgentAdapter`), add:

```python
from __future__ import annotations

import copy
import json
import logging
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .base import AgentAdapter

_log = logging.getLogger(__name__)

_ERROR_CODE_MAX_CHARS = 100


def _safe_extract_error_code(err: Any) -> str:
    """Defensively extract an error 'code' from a recheck row's error_state.

    Per §4.2 the recheck row's `error_state` is a mapping with `code`/`message`/
    `raw` keys, but malformed inputs (legacy fixtures, partial writes, future
    schema drift) must NOT crash synthesis. Returns 'unknown' for anything
    that doesn't yield a usable string.
    """
    if isinstance(err, Mapping):
        code = err.get("code")
        if isinstance(code, str) and code:
            return code[:_ERROR_CODE_MAX_CHARS]
        return "unknown"
    if isinstance(err, str) and err:
        return err[:_ERROR_CODE_MAX_CHARS]
    return "unknown"
```

(The other imports — `copy`, `json`, `logging`, `os`, `warnings`, `dataclass`, `field`, `Path`, `Iterator`, `Mapping`, `Optional` — are needed by Tasks 40–47. Adding them all here keeps later tasks focused on logic, not import-shuffling.)

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "safe_extract"
```

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): _safe_extract_error_code defensive helper (§4.6)"
```

---

### Task 40: Implement `_normalise_primary` (gate-vocabulary → scope-axis)

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 (A): `effective_durant` consumes scope-axis primary verdicts (`present`/`not_present`/`ambiguous`), but `working/durant_verdicts.jsonl` stores gate-level verdicts (`biographical`/`work_context_only`/`ambiguous`). The translation is encapsulated in `_normalise_primary(durant_verdict)` so the rest of the synthesis logic can think in scope-axis terms. Unknown vocabulary raises — synthesis is the wrong place to silently degrade verdicts.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- _normalise_primary -----------------------------------------------------

from dsar_pipeline.agents.agent22_scope_check import _normalise_primary


def test_normalise_primary_biographical_to_present():
    assert _normalise_primary("biographical") == "present"


def test_normalise_primary_work_context_only_to_not_present():
    assert _normalise_primary("work_context_only") == "not_present"


def test_normalise_primary_ambiguous_passthrough():
    assert _normalise_primary("ambiguous") == "ambiguous"


def test_normalise_primary_scope_axis_passthrough():
    """If the input is ALREADY in scope-axis form, pass through."""
    assert _normalise_primary("present") == "present"
    assert _normalise_primary("not_present") == "not_present"


def test_normalise_primary_raises_on_unknown():
    with pytest.raises(ValueError, match="unknown primary durant verdict"):
        _normalise_primary("nonsense")
    with pytest.raises(ValueError, match="unknown primary durant verdict"):
        _normalise_primary("")


def test_normalise_primary_raises_on_none():
    with pytest.raises(ValueError, match="unknown primary durant verdict"):
        _normalise_primary(None)


def test_normalise_primary_strips_and_lowercases():
    """Be a bit forgiving with whitespace/case on input from the JSONL."""
    assert _normalise_primary("  Biographical ") == "present"
    assert _normalise_primary("WORK_CONTEXT_ONLY") == "not_present"
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "normalise_primary"
```

Expected: ImportError.

- [ ] **Step 3: Implement `_normalise_primary`**

Append to `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py` (after `_safe_extract_error_code`):

```python
_PRIMARY_NORMALISE = {
    "biographical": "present",
    "work_context_only": "not_present",
    "ambiguous": "ambiguous",
    "present": "present",
    "not_present": "not_present",
}


def _normalise_primary(durant_verdict: Any) -> str:
    """Translate a gate_durant verdict into the scope-axis vocabulary
    used by `effective_durant`.

    Accepts:
      - "biographical"        → "present"
      - "work_context_only"   → "not_present"
      - "ambiguous"           → "ambiguous"
      - "present" / "not_present" → passthrough (in case caller already
        normalised)

    Tolerates surrounding whitespace and case differences. Raises
    ValueError on anything else (including None / empty string) — silent
    degradation here would mask data-quality bugs in upstream gates.
    """
    if not isinstance(durant_verdict, str):
        raise ValueError(
            f"unknown primary durant verdict: {durant_verdict!r} "
            f"(expected str, got {type(durant_verdict).__name__})"
        )
    key = durant_verdict.strip().lower()
    if key not in _PRIMARY_NORMALISE:
        raise ValueError(
            f"unknown primary durant verdict: {durant_verdict!r}"
        )
    return _PRIMARY_NORMALISE[key]
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "normalise_primary"
```

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): _normalise_primary gate→scope vocabulary (§4.6)"
```

---

### Task 41: Implement `effective_durant` (8-branch decision table)

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 (A): `effective_durant(primary, recheck, recheck_err, recheck_mode)` returns `(verdict, reason_code)`. This is the durant-axis decision before temporal overlay. Critical distinction: when `recheck is None`, the reason code depends on `recheck_mode`:
- `mode == "never"` → `("not_present", "primary_wco_recheck_disabled")` — operator opted out, primary WCO stands.
- `mode in ("always", *)` → `("ambiguous", "recheck_expected_but_missing_for_ref")` — the recheck was supposed to run but no row exists for this ref (architectural anomaly; surface to operator).

- [ ] **Step 1: Write failing tests — the full 8-row branch table**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- effective_durant -------------------------------------------------------

from dsar_pipeline.agents.agent22_scope_check import effective_durant


@pytest.mark.parametrize(
    "primary,recheck,recheck_err,recheck_mode,expected_verdict,expected_reason",
    [
        # Row 1: primary=present → present (no recheck triggered)
        ("present", None, None, "always", "present", "primary_biographical"),
        # Row 2: primary=ambiguous → ambiguous
        ("ambiguous", None, None, "always", "ambiguous", "primary_ambiguous"),
        # Row 3: primary=not_present + recheck errored → ambiguous (recheck_errored:<code>)
        ("not_present", None, {"code": "timeout", "message": "x"}, "always",
         "ambiguous", "recheck_errored:timeout"),
        # Row 4: primary=not_present + recheck confirmed_work_context_only → not_present
        ("not_present", "confirmed_work_context_only", None, "always",
         "not_present", "recheck_confirms_wco"),
        # Row 5: primary=not_present + recheck reclassify_to_biographical → present
        ("not_present", "reclassify_to_biographical", None, "always",
         "present", "recheck_reclassified_biographical"),
        # Row 6: primary=not_present + recheck reclassify_to_ambiguous → ambiguous
        ("not_present", "reclassify_to_ambiguous", None, "always",
         "ambiguous", "recheck_reclassified_ambiguous"),
        # Row 7: primary=not_present + no recheck + mode=never → not_present (operator opt-out)
        ("not_present", None, None, "never",
         "not_present", "primary_wco_recheck_disabled"),
        # Row 8: primary=not_present + no recheck + mode=always → ambiguous (anomaly)
        ("not_present", None, None, "always",
         "ambiguous", "recheck_expected_but_missing_for_ref"),
    ],
)
def test_effective_durant_branch_table(
    primary, recheck, recheck_err, recheck_mode, expected_verdict, expected_reason
):
    verdict, reason = effective_durant(primary, recheck, recheck_err, recheck_mode)
    assert verdict == expected_verdict
    assert reason == expected_reason


def test_effective_durant_unknown_recheck_value_returns_ambiguous():
    """A recheck_verdict value not in the §4.2 enum surfaces as ambiguous
    with an `unknown_recheck_verdict:<v>` reason code."""
    v, r = effective_durant("not_present", "some_future_recheck_value", None, "always")
    assert v == "ambiguous"
    assert r == "unknown_recheck_verdict:some_future_recheck_value"


def test_effective_durant_recheck_err_takes_precedence_over_recheck_value():
    """If error_state is non-null, error path runs even if recheck_verdict
    is also non-null (defends against legacy/malformed rows violating the
    §4.2 oneOf invariant)."""
    v, r = effective_durant(
        "not_present",
        "reclassify_to_biographical",   # would normally → present
        {"code": "schema_validation_failed"},
        "always",
    )
    assert v == "ambiguous"
    assert r == "recheck_errored:schema_validation_failed"
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "effective_durant"
```

Expected: ImportError.

- [ ] **Step 3: Implement `effective_durant`**

Append to `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`:

```python
def effective_durant(
    primary: str,
    recheck: Optional[str],
    recheck_err: Any,
    recheck_mode: str,
) -> tuple[str, str]:
    """Return (scope_axis_verdict, reason_code) — the durant-axis decision
    before temporal overlay.

    `primary` is in scope-axis form (use `_normalise_primary` first).
    `recheck` is the recheck row's `recheck_verdict` field or None.
    `recheck_err` is the recheck row's `error_state` field (mapping/None).
    `recheck_mode` is the resolved `mode_effective` from
    `recheck_decision.json` ("always" or "never").

    Branches (spec §4.6 (A)):
      1. primary=present              → present, "primary_biographical"
      2. primary=ambiguous            → ambiguous, "primary_ambiguous"
      3. primary=not_present + recheck_err → ambiguous, "recheck_errored:<code>"
      4. primary=not_present + recheck=confirmed_wco → not_present, "recheck_confirms_wco"
      5. primary=not_present + recheck=reclassify_bio → present,    "recheck_reclassified_biographical"
      6. primary=not_present + recheck=reclassify_amb → ambiguous,  "recheck_reclassified_ambiguous"
      7. primary=not_present + recheck=None + mode=never → not_present, "primary_wco_recheck_disabled"
      8. primary=not_present + recheck=None + mode!=never → ambiguous, "recheck_expected_but_missing_for_ref"
      9. primary=not_present + unrecognised recheck value → ambiguous, "unknown_recheck_verdict:<v>"
    """
    if primary == "present":
        return ("present", "primary_biographical")
    if primary == "ambiguous":
        return ("ambiguous", "primary_ambiguous")
    # primary == "not_present"
    if recheck_err is not None:
        code = _safe_extract_error_code(recheck_err)
        return ("ambiguous", f"recheck_errored:{code}")
    if recheck == "confirmed_work_context_only":
        return ("not_present", "recheck_confirms_wco")
    if recheck == "reclassify_to_biographical":
        return ("present", "recheck_reclassified_biographical")
    if recheck == "reclassify_to_ambiguous":
        return ("ambiguous", "recheck_reclassified_ambiguous")
    if recheck is None:
        if recheck_mode == "never":
            return ("not_present", "primary_wco_recheck_disabled")
        return ("ambiguous", "recheck_expected_but_missing_for_ref")
    return ("ambiguous", f"unknown_recheck_verdict:{recheck}")
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "effective_durant"
```

Expected: 10 PASS (8 parametrized rows + 2 extra).

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): effective_durant 8-branch decision (§4.6 A)"
```

---

### Task 42: Implement 5-arg `synthesise_verdict` with conflict surfacing

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 (B): `synthesise_verdict(primary, recheck, recheck_err, recheck_mode, temporal)` returns a 3-tuple `(scope_verdict, rationale, effective_durant_label)`. Critical guarantee: **`temporal == "out_of_scope"` does NOT silently flatten `effective_durant=ambiguous` to `not_present`** — that would mask cases where recheck escalated to `ambiguous` but the temporal gate disagreed. Instead, conflict cases route to `ambiguous` with an operator-readable rationale.

Inputs to this function are scope-axis primary (use `_normalise_primary` upstream), the raw `recheck_verdict` enum or None, `recheck_err` mapping/None, `recheck_mode` string, and `temporal` ∈ `{"in_scope", "out_of_scope", None}`.

- [ ] **Step 1: Write failing tests — cartesian matrix of effective × temporal**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- synthesise_verdict (5-arg) ---------------------------------------------

from dsar_pipeline.agents.agent22_scope_check import synthesise_verdict


# Cartesian matrix: 3 effective_durant outcomes × 3 temporal values = 9 cells.

def test_synthesise_present_temporal_in_scope():
    """effective_durant=present + temporal=in_scope → present."""
    scope, rationale, eff = synthesise_verdict(
        "present", None, None, "always", "in_scope"
    )
    assert scope == "present"
    assert eff == "present"
    assert "primary_biographical" in rationale


def test_synthesise_present_temporal_none():
    """effective_durant=present + temporal=None → present (no temporal data)."""
    scope, rationale, eff = synthesise_verdict(
        "present", None, None, "always", None
    )
    assert scope == "present"
    assert eff == "present"


def test_synthesise_present_temporal_out_of_scope_surfaces_conflict():
    """The §4.6 (B v3) conflict-surfacing case: durant=present but
    temporal=out_of_scope must route to ambiguous, not silently override."""
    scope, rationale, eff = synthesise_verdict(
        "present", None, None, "always", "out_of_scope"
    )
    assert scope == "ambiguous"
    assert eff == "present"   # the effective_durant label preserved
    assert "out_of_scope" in rationale
    assert "operator" in rationale.lower()  # operator-readable conflict


def test_synthesise_not_present_temporal_in_scope():
    """effective_durant=not_present + temporal=in_scope → not_present."""
    scope, rationale, eff = synthesise_verdict(
        "not_present", "confirmed_work_context_only", None, "always", "in_scope"
    )
    assert scope == "not_present"
    assert eff == "not_present"
    assert "recheck_confirms_wco" in rationale


def test_synthesise_not_present_temporal_out_of_scope():
    """Not_present + out_of_scope → not_present (consistent; either alone
    would have eliminated the doc)."""
    scope, rationale, eff = synthesise_verdict(
        "not_present", "confirmed_work_context_only", None, "always", "out_of_scope"
    )
    assert scope == "not_present"
    assert eff == "not_present"
    assert "temporal=out_of_scope" in rationale


def test_synthesise_ambiguous_temporal_in_scope():
    scope, rationale, eff = synthesise_verdict(
        "ambiguous", None, None, "always", "in_scope"
    )
    assert scope == "ambiguous"
    assert eff == "ambiguous"


def test_synthesise_ambiguous_temporal_out_of_scope_stays_ambiguous():
    """Critical: ambiguous + out_of_scope MUST stay ambiguous (not collapse
    to not_present) — operator may reconcile."""
    scope, rationale, eff = synthesise_verdict(
        "ambiguous", None, None, "always", "out_of_scope"
    )
    assert scope == "ambiguous"
    assert eff == "ambiguous"
    assert "out_of_scope" in rationale


def test_synthesise_returns_recheck_promoted_path():
    """End-to-end the §4.6 safety-net path: primary=WCO, recheck reclassifies
    to biographical → final scope=present."""
    scope, rationale, eff = synthesise_verdict(
        "not_present", "reclassify_to_biographical", None, "always", "in_scope"
    )
    assert scope == "present"
    assert eff == "present"
    assert "recheck_reclassified_biographical" in rationale


def test_synthesise_recheck_error_path_routes_to_ambiguous():
    scope, rationale, eff = synthesise_verdict(
        "not_present", None,
        {"code": "model_unreachable", "message": "x"}, "always", "in_scope"
    )
    assert scope == "ambiguous"
    assert eff == "ambiguous"
    assert "recheck_errored:model_unreachable" in rationale


def test_synthesise_mode_never_no_recheck_returns_not_present():
    """Operator opted out of recheck; primary WCO stands."""
    scope, rationale, eff = synthesise_verdict(
        "not_present", None, None, "never", "in_scope"
    )
    assert scope == "not_present"
    assert eff == "not_present"
    assert "primary_wco_recheck_disabled" in rationale


def test_synthesise_missing_recheck_anomaly():
    """mode=always but no recheck row for this WCO ref → ambiguous anomaly."""
    scope, rationale, eff = synthesise_verdict(
        "not_present", None, None, "always", "in_scope"
    )
    assert scope == "ambiguous"
    assert eff == "ambiguous"
    assert "recheck_expected_but_missing_for_ref" in rationale
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "synthesise"
```

Expected: ImportError or NameError for `synthesise_verdict`.

- [ ] **Step 3: Implement 5-arg `synthesise_verdict`**

Append to `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`:

```python
def synthesise_verdict(
    primary: str,
    recheck: Optional[str],
    recheck_err: Any,
    recheck_mode: str,
    temporal: Optional[str],
) -> tuple[str, str, str]:
    """Return (scope_verdict, rationale, effective_durant_label) — the
    canonical 5-arg synthesis per spec §4.6 (B).

    `primary` is scope-axis (`present` / `not_present` / `ambiguous`); call
    `_normalise_primary(durant_verdicts_jsonl_row['durant_verdict'])` first.
    `recheck`, `recheck_err` come from the durant_underdisclosure_recheck.jsonl
    row matching this ref (or None if no row exists).
    `recheck_mode` is from `recheck_decision.json:mode_effective` ("always"|"never").
    `temporal` is from `temporal_verdicts.jsonl` ("in_scope"|"out_of_scope"|None).

    Conflict-surfacing guarantee: `temporal == 'out_of_scope'` does NOT
    silently override an ambiguous effective_durant. Recheck-promoted refs
    with conflicting temporal escalate to ambiguous (operator-reconcilable).
    """
    eff, reason = effective_durant(primary, recheck, recheck_err, recheck_mode)
    if temporal == "out_of_scope":
        if eff == "present":
            return (
                "ambiguous",
                f"durant=present ({reason}) vs temporal=out_of_scope; "
                f"operator should reconcile",
                eff,
            )
        if eff == "ambiguous":
            return (
                "ambiguous",
                f"durant=ambiguous ({reason}) + temporal=out_of_scope",
                eff,
            )
        # eff == "not_present" — consistent
        return (
            "not_present",
            f"effective_durant=not_present ({reason}); temporal=out_of_scope",
            eff,
        )
    if eff == "not_present":
        return (
            "not_present",
            f"effective_durant=not_present ({reason})",
            eff,
        )
    if eff == "present" and (temporal == "in_scope" or temporal is None):
        return (
            "present",
            f"effective_durant=present ({reason})",
            eff,
        )
    # eff == "ambiguous" with temporal in_scope/None — stays ambiguous.
    return (
        "ambiguous",
        f"effective_durant={eff} ({reason}), temporal={temporal}",
        eff,
    )
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "synthesise"
```

Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): synthesise_verdict 5-arg with conflict surfacing (§4.6 B)"
```

---

### Task 43: Implement `_truncate_any` and `_trim_error_state` (audit footprint)

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 (G) and §4.6 v9: the scope_verdicts.jsonl `evidence.error_state` keeps only `code` + `message` from the recheck row's `error_state` and shoves any other keys into an `_extra` sub-dict (per the schema). The `raw` field is intentionally DROPPED — full sanitised trace lives in the recheck JSONL per §4.2's contract; the synthesis row only needs the structured fields. Helpers:

- `_truncate_any(v, max_chars)` → `(value, was_truncated)`. Preserves primitive types when small enough; emits a "TypeName[N items]" descriptor for very large containers; uses `copy.copy()` on the success path so callers can't mutate cached state.
- `_trim_error_state(err)` → mapping suitable for the evidence block, or None.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- _truncate_any ----------------------------------------------------------

from dsar_pipeline.agents.agent22_scope_check import _truncate_any, _trim_error_state


def test_truncate_any_short_string_untruncated():
    v, was = _truncate_any("hello", max_chars=400)
    assert v == "hello"
    assert was is False


def test_truncate_any_long_string_truncated_with_ellipsis():
    long = "x" * 1000
    v, was = _truncate_any(long, max_chars=100)
    assert was is True
    assert len(v) == 100
    assert v.endswith("…")


def test_truncate_any_int_preserved():
    """Primitives within budget pass through with copy semantics."""
    v, was = _truncate_any(42, max_chars=400)
    assert v == 42
    assert was is False


def test_truncate_any_small_list_preserved_with_copy():
    """List under threshold: returned as a copy, not the original reference."""
    src = [1, 2, 3]
    v, was = _truncate_any(src, max_chars=400)
    assert v == [1, 2, 3]
    assert was is False
    assert v is not src   # defensive copy


def test_truncate_any_huge_list_becomes_descriptor():
    huge = list(range(5000))
    v, was = _truncate_any(huge, max_chars=400)
    assert was is True
    assert v == "list[5000 items]"


def test_truncate_any_huge_dict_becomes_descriptor():
    huge = {f"k{i}": i for i in range(2000)}
    v, was = _truncate_any(huge, max_chars=400)
    assert was is True
    assert v == "dict[2000 items]"


def test_truncate_any_unserialisable_object_str_fallback():
    class Weird:
        def __str__(self):
            return "weird-instance"
    v, was = _truncate_any(Weird(), max_chars=400)
    # Either was truncated (long str() output) or returned untruncated;
    # the key point is no crash and `v` is something stringy.
    assert v == "weird-instance" or v == Weird() or isinstance(v, str)


def test_truncate_any_str_fallback_on_repr_failure():
    class BadStr:
        def __str__(self):
            raise RuntimeError("nope")
    v, was = _truncate_any(BadStr(), max_chars=400)
    assert was is True
    assert isinstance(v, str)
    assert "str_failed" in v


# ---- _trim_error_state ------------------------------------------------------

def test_trim_error_state_none():
    assert _trim_error_state(None) is None


def test_trim_error_state_preserves_code_and_message():
    err = {"code": "timeout", "message": "request timed out"}
    out = _trim_error_state(err)
    assert out == {"code": "timeout", "message": "request timed out"}


def test_trim_error_state_drops_raw_field():
    """Per §4.2 contract, the `raw` field (sanitised trace) lives in the
    recheck JSONL — synthesis does not duplicate it."""
    err = {
        "code": "timeout",
        "message": "x",
        "raw": "very long raw trace " * 100,
    }
    out = _trim_error_state(err)
    assert "raw" not in out
    assert "_extra" not in out  # raw is dropped, not relocated


def test_trim_error_state_unknown_keys_go_to_extra():
    err = {
        "code": "timeout",
        "message": "x",
        "upstream_id": "litellm-abc",
        "retry_count": 3,
    }
    out = _trim_error_state(err)
    assert out["code"] == "timeout"
    assert out["message"] == "x"
    assert out["_extra"]["upstream_id"] == "litellm-abc"
    assert out["_extra"]["retry_count"] == 3


def test_trim_error_state_truncates_long_message():
    err = {"code": "x", "message": "y" * 10_000}
    out = _trim_error_state(err)
    assert len(out["message"]) <= 401   # 400 chars + ellipsis
    assert out["message"].endswith("…")


def test_trim_error_state_handles_non_string_code():
    """Defensive: if code/message aren't strings, still produce something."""
    err = {"code": 123, "message": ["not", "a", "string"]}
    out = _trim_error_state(err)
    # Either str-converted or wrapped in _extra; either way no crash.
    assert "code" in out
    assert "message" in out


def test_trim_error_state_large_extra_container_descriptor():
    """Large values in unknown keys collapse to descriptors."""
    err = {
        "code": "x",
        "message": "y",
        "trace_frames": list(range(2000)),
    }
    out = _trim_error_state(err)
    assert out["_extra"]["trace_frames"] == "list[2000 items]"


def test_trim_error_state_drops_top_level_when_not_mapping():
    """If error_state isn't a mapping → return None (can't synthesise)."""
    assert _trim_error_state("just a string") is None
    assert _trim_error_state(42) is None
    assert _trim_error_state([]) is None
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "_truncate_any or _trim_error_state"
```

Expected: ImportError.

- [ ] **Step 3: Implement `_truncate_any` and `_trim_error_state`**

Append to `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`:

```python
_TRIM_VALUE_MAX_CHARS = 400
_TRIM_EXTRA_MAX_KEYS = 8
_LARGE_CONTAINER_THRESHOLD = 1000
_TRIM_PRESERVED_KEYS = ("code", "message")
_TRIM_DROPPED_KEYS = ("raw",)


def _truncate_any(v: Any, max_chars: int = _TRIM_VALUE_MAX_CHARS) -> tuple[Any, bool]:
    """Return (value, was_truncated). Strings >max_chars get ellipsis;
    containers >_LARGE_CONTAINER_THRESHOLD items collapse to a descriptor
    string; other objects are stringified; primitives are returned by
    `copy.copy()` so caller mutations cannot reach cached state.
    """
    if isinstance(v, str):
        if len(v) > max_chars:
            return (v[: max_chars - 1] + "…", True)
        return (v, False)
    if isinstance(v, (list, tuple, dict, set)):
        try:
            n = len(v)
            if n > _LARGE_CONTAINER_THRESHOLD:
                descriptor = f"{type(v).__name__}[{n} items]"
                return (descriptor, True)
        except TypeError:
            pass
    if isinstance(v, (int, float, bool)) or v is None:
        return (v, False)
    try:
        s = str(v)
    except Exception as e:
        return (f"<str_failed:{type(v).__name__}:{type(e).__name__}>", True)
    if len(s) > max_chars:
        return (s[: max_chars - 1] + "…", True)
    # Primitive-safe path: defensive copy so caller can't mutate cached state.
    try:
        return (copy.copy(v), False)
    except Exception:
        # Last-resort: return the str-form if copy fails.
        return (s, False)


def _trim_error_state(err: Any) -> Optional[dict]:
    """Trim a recheck-row error_state into the synthesis evidence shape.

    Preserves: code, message (truncated per _TRIM_VALUE_MAX_CHARS).
    Drops: `raw` (lives in recheck JSONL per §4.2; not duplicated).
    Unknown keys: relocated under `_extra` (first _TRIM_EXTRA_MAX_KEYS only).

    Non-mapping inputs → None (the synthesis evidence block requires a
    dict or null; a string error_state can't be relocated structurally).
    """
    if not isinstance(err, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in _TRIM_PRESERVED_KEYS:
        if key in err:
            val = err[key]
            if isinstance(val, str):
                trimmed, _was = _truncate_any(val)
                out[key] = trimmed
            else:
                # Coerce non-string code/message via _truncate_any (str fallback).
                trimmed, _was = _truncate_any(val)
                out[key] = trimmed if isinstance(trimmed, str) else str(trimmed)
    extras: dict[str, Any] = {}
    n_added = 0
    for key, val in err.items():
        if key in _TRIM_PRESERVED_KEYS or key in _TRIM_DROPPED_KEYS:
            continue
        if n_added >= _TRIM_EXTRA_MAX_KEYS:
            break
        trimmed, _was = _truncate_any(val)
        extras[key] = trimmed
        n_added += 1
    if extras:
        out["_extra"] = extras
    return out
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "_truncate_any or _trim_error_state"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): _truncate_any + _trim_error_state audit helpers (§4.6 G)"
```

---

### Task 44: Implement `_iter_jsonl_safe` and `_build_index_first_wins`

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 v8: a unified JSONL index builder used for both recheck and temporal inputs. Safety properties:
- Missing file → empty dict + log message (NOT error: temporal/recheck JSONL may legitimately be absent).
- OSError (permissions, mid-stream IO failure) → return whatever partial index was built, log warning.
- Per-line: lines >1MB skipped + warned; non-UTF-8 bytes skipped + warned; malformed JSON skipped + warned.
- Collision (duplicate `doc_ref`) → first-wins; log warning naming the file + ref.

`_iter_jsonl_safe(path, name)` is the generator; `_build_index_first_wins(path, name, key_field='doc_ref')` consumes it.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- _iter_jsonl_safe + _build_index_first_wins ----------------------------

from dsar_pipeline.agents.agent22_scope_check import (
    _iter_jsonl_safe, _build_index_first_wins,
)


def test_iter_jsonl_safe_missing_file_yields_nothing(tmp_path, caplog):
    """Missing file → empty iterator + log message (not an exception)."""
    path = tmp_path / "nonexistent.jsonl"
    with caplog.at_level("DEBUG"):
        rows = list(_iter_jsonl_safe(path, "test"))
    assert rows == []


def test_iter_jsonl_safe_reads_valid_lines(tmp_path):
    path = tmp_path / "data.jsonl"
    path.write_text(
        '{"doc_ref": "D000001", "x": 1}\n'
        '{"doc_ref": "D000002", "x": 2}\n',
        encoding="utf-8",
    )
    rows = list(_iter_jsonl_safe(path, "test"))
    assert rows == [
        {"doc_ref": "D000001", "x": 1},
        {"doc_ref": "D000002", "x": 2},
    ]


def test_iter_jsonl_safe_skips_malformed_json(tmp_path, caplog):
    path = tmp_path / "broken.jsonl"
    path.write_text(
        '{"doc_ref": "ok1", "x": 1}\n'
        '{not valid json at all}\n'
        '{"doc_ref": "ok2", "x": 2}\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        rows = list(_iter_jsonl_safe(path, "test"))
    assert len(rows) == 2
    assert rows[0]["doc_ref"] == "ok1"
    assert rows[1]["doc_ref"] == "ok2"
    assert any("malformed" in r.message.lower() or "json" in r.message.lower()
               for r in caplog.records)


def test_iter_jsonl_safe_skips_huge_lines(tmp_path, caplog):
    """Lines >1MB are skipped + warned."""
    path = tmp_path / "huge.jsonl"
    huge_line = '{"x": "' + ("a" * (1024 * 1024 + 100)) + '"}\n'
    path.write_text(
        '{"doc_ref": "ok1"}\n' + huge_line + '{"doc_ref": "ok2"}\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        rows = list(_iter_jsonl_safe(path, "test"))
    refs = [r.get("doc_ref") for r in rows]
    assert "ok1" in refs
    assert "ok2" in refs
    assert len(rows) == 2


def test_iter_jsonl_safe_skips_non_utf8_lines(tmp_path, caplog):
    path = tmp_path / "mixed.jsonl"
    # Mix valid UTF-8 with a line containing invalid bytes.
    path.write_bytes(
        b'{"doc_ref": "ok1"}\n'
        b'{"doc_ref": "\xff\xfeBAD"}\n'
        b'{"doc_ref": "ok2"}\n',
    )
    with caplog.at_level("WARNING"):
        rows = list(_iter_jsonl_safe(path, "test"))
    refs = [r.get("doc_ref") for r in rows]
    assert "ok1" in refs
    assert "ok2" in refs


def test_iter_jsonl_safe_handles_blank_lines(tmp_path):
    path = tmp_path / "blanks.jsonl"
    path.write_text(
        '{"doc_ref": "ok1"}\n'
        '\n'
        '   \n'
        '{"doc_ref": "ok2"}\n',
        encoding="utf-8",
    )
    rows = list(_iter_jsonl_safe(path, "test"))
    assert len(rows) == 2


def test_build_index_first_wins_basic(tmp_path):
    path = tmp_path / "idx.jsonl"
    path.write_text(
        '{"doc_ref": "A", "v": 1}\n'
        '{"doc_ref": "B", "v": 2}\n',
        encoding="utf-8",
    )
    idx = _build_index_first_wins(path, name="test", key_field="doc_ref")
    assert idx["A"] == {"doc_ref": "A", "v": 1}
    assert idx["B"] == {"doc_ref": "B", "v": 2}


def test_build_index_first_wins_duplicate_first_wins(tmp_path, caplog):
    path = tmp_path / "dup.jsonl"
    path.write_text(
        '{"doc_ref": "A", "v": 1}\n'
        '{"doc_ref": "A", "v": 99}\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        idx = _build_index_first_wins(path, name="test", key_field="doc_ref")
    assert idx["A"] == {"doc_ref": "A", "v": 1}   # first wins
    assert any("duplicate" in r.message.lower() or "collision" in r.message.lower()
               for r in caplog.records)


def test_build_index_first_wins_missing_file_returns_empty(tmp_path):
    idx = _build_index_first_wins(tmp_path / "nope.jsonl", name="test",
                                   key_field="doc_ref")
    assert idx == {}


def test_build_index_first_wins_rows_without_key_field_skipped(tmp_path, caplog):
    path = tmp_path / "incomplete.jsonl"
    path.write_text(
        '{"doc_ref": "A", "v": 1}\n'
        '{"v": 2}\n'
        '{"doc_ref": "B", "v": 3}\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        idx = _build_index_first_wins(path, name="test", key_field="doc_ref")
    assert set(idx.keys()) == {"A", "B"}
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "_iter_jsonl_safe or _build_index_first_wins"
```

Expected: ImportError.

- [ ] **Step 3: Implement the two helpers**

Append to `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`:

```python
_JSONL_MAX_LINE_BYTES = 1024 * 1024   # 1 MB per row guard


def _iter_jsonl_safe(path: Path, name: str) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, defensively.

    Per spec §4.6 v8:
      - Missing file: empty iterator, debug log.
      - OSError mid-stream: stop iteration, warning log (partial yields kept).
      - Per-line oversize (>_JSONL_MAX_LINE_BYTES): skip + warn.
      - Non-UTF-8 bytes on a line: skip + warn.
      - Malformed JSON: skip + warn.
      - Non-object JSON (array/string/number): skip + warn.
    """
    if not path.exists():
        _log.debug("jsonl %s missing at %s; empty index", name, path)
        return
    try:
        with open(path, "rb") as fh:
            for lineno, raw in enumerate(fh, start=1):
                if len(raw) > _JSONL_MAX_LINE_BYTES:
                    _log.warning(
                        "jsonl %s line %d skipped: oversize (%d bytes)",
                        name, lineno, len(raw),
                    )
                    continue
                try:
                    text = raw.decode("utf-8").strip()
                except UnicodeDecodeError as e:
                    _log.warning(
                        "jsonl %s line %d skipped: non-UTF-8 (%s)",
                        name, lineno, e,
                    )
                    continue
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as e:
                    _log.warning(
                        "jsonl %s line %d skipped: malformed JSON (%s)",
                        name, lineno, e,
                    )
                    continue
                if not isinstance(obj, dict):
                    _log.warning(
                        "jsonl %s line %d skipped: not a JSON object (got %s)",
                        name, lineno, type(obj).__name__,
                    )
                    continue
                yield obj
    except OSError as e:
        _log.warning("jsonl %s read failed at %s: %s; partial index", name, path, e)
        return


def _build_index_first_wins(
    path: Path,
    name: str,
    *,
    key_field: str = "doc_ref",
) -> dict[str, dict]:
    """Build {key_field_value: row} from a JSONL file. First-wins on
    collisions (later rows discarded with a warning); rows without the
    key_field skipped with a warning.

    Missing file → empty dict (no error: the input is OPTIONAL — temporal
    verdicts and recheck JSONL may both legitimately be absent in some
    case configurations).
    """
    out: dict[str, dict] = {}
    for row in _iter_jsonl_safe(path, name):
        key = row.get(key_field)
        if not isinstance(key, str) or not key:
            _log.warning(
                "jsonl %s row skipped: missing %s field", name, key_field,
            )
            continue
        if key in out:
            _log.warning(
                "jsonl %s duplicate %s=%r; first-wins (later row discarded)",
                name, key_field, key,
            )
            continue
        out[key] = row
    return out
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "_iter_jsonl_safe or _build_index_first_wins"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): _iter_jsonl_safe + first-wins index builder (§4.6 v8)"
```

---

### Task 45: Implement `SynthesisSummary` counter class

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 (F): a per-run counter aggregating decisions across primary refs. Counters:

- `primary_present`, `primary_not_present`, `primary_ambiguous` — primary durant verdict distribution.
- `recheck_promoted` — primary=WCO → final scope=present (the safety net firing).
- `recheck_escalated` — primary=WCO → final scope=ambiguous via recheck reclassify_to_ambiguous.
- `recheck_confirmed` — primary=WCO + recheck confirms WCO.
- `recheck_errored` — recheck row had an `error_state`.
- `recheck_missing_anomaly` — primary=WCO + mode=always + no recheck row.
- `primary_wco_recheck_disabled` — primary=WCO + mode=never.
- `recheck_other` — recheck row exists for a non-WCO primary (architectural invariant monitor).
- `scope_present`, `scope_not_present`, `scope_ambiguous` — final scope distribution.
- `temporal_out_blocked` — temporal=out_of_scope kicked scope to not_present.
- `temporal_recheck_conflict` — temporal=out_of_scope kicked scope to ambiguous despite present/ambiguous effective_durant.

`add(primary, recheck, recheck_err, recheck_mode, temporal, scope, eff)` increments per the inputs. `write(path)` atomic-writes JSON to `working/synthesis_summary.json`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- SynthesisSummary -------------------------------------------------------

from dsar_pipeline.agents.agent22_scope_check import SynthesisSummary


def test_synthesis_summary_initial_counts_zero():
    s = SynthesisSummary()
    d = s.to_dict()
    for key in (
        "primary_present", "primary_not_present", "primary_ambiguous",
        "recheck_promoted", "recheck_escalated", "recheck_confirmed",
        "recheck_errored", "recheck_missing_anomaly",
        "primary_wco_recheck_disabled", "recheck_other",
        "scope_present", "scope_not_present", "scope_ambiguous",
        "temporal_out_blocked", "temporal_recheck_conflict",
    ):
        assert d[key] == 0, f"{key} should start at 0"


def test_synthesis_summary_add_primary_present():
    s = SynthesisSummary()
    s.add(primary="present", recheck=None, recheck_err=None,
          recheck_mode="always", temporal="in_scope",
          scope="present", eff="present")
    d = s.to_dict()
    assert d["primary_present"] == 1
    assert d["scope_present"] == 1


def test_synthesis_summary_recheck_promoted():
    """The headline safety-net counter: primary=WCO promoted to scope=present
    via recheck reclassify_to_biographical."""
    s = SynthesisSummary()
    s.add(primary="not_present", recheck="reclassify_to_biographical",
          recheck_err=None, recheck_mode="always", temporal="in_scope",
          scope="present", eff="present")
    d = s.to_dict()
    assert d["primary_not_present"] == 1
    assert d["recheck_promoted"] == 1
    assert d["scope_present"] == 1
    assert d["recheck_confirmed"] == 0
    assert d["recheck_escalated"] == 0


def test_synthesis_summary_recheck_escalated():
    s = SynthesisSummary()
    s.add(primary="not_present", recheck="reclassify_to_ambiguous",
          recheck_err=None, recheck_mode="always", temporal="in_scope",
          scope="ambiguous", eff="ambiguous")
    d = s.to_dict()
    assert d["recheck_escalated"] == 1
    assert d["scope_ambiguous"] == 1


def test_synthesis_summary_recheck_confirmed():
    s = SynthesisSummary()
    s.add(primary="not_present", recheck="confirmed_work_context_only",
          recheck_err=None, recheck_mode="always", temporal="in_scope",
          scope="not_present", eff="not_present")
    d = s.to_dict()
    assert d["recheck_confirmed"] == 1
    assert d["scope_not_present"] == 1


def test_synthesis_summary_recheck_errored():
    s = SynthesisSummary()
    s.add(primary="not_present", recheck=None,
          recheck_err={"code": "timeout", "message": "x"},
          recheck_mode="always", temporal="in_scope",
          scope="ambiguous", eff="ambiguous")
    d = s.to_dict()
    assert d["recheck_errored"] == 1
    assert d["scope_ambiguous"] == 1


def test_synthesis_summary_primary_wco_recheck_disabled():
    s = SynthesisSummary()
    s.add(primary="not_present", recheck=None, recheck_err=None,
          recheck_mode="never", temporal="in_scope",
          scope="not_present", eff="not_present")
    d = s.to_dict()
    assert d["primary_wco_recheck_disabled"] == 1
    assert d["scope_not_present"] == 1


def test_synthesis_summary_recheck_missing_anomaly():
    """primary=WCO + mode=always + no recheck row → anomaly counter."""
    s = SynthesisSummary()
    s.add(primary="not_present", recheck=None, recheck_err=None,
          recheck_mode="always", temporal="in_scope",
          scope="ambiguous", eff="ambiguous")
    d = s.to_dict()
    assert d["recheck_missing_anomaly"] == 1


def test_synthesis_summary_recheck_other_invariant_violation():
    """A recheck row paired with a non-WCO primary is unexpected; tracked
    as an architectural invariant monitor."""
    s = SynthesisSummary()
    s.add(primary="present", recheck="reclassify_to_biographical",
          recheck_err=None, recheck_mode="always", temporal="in_scope",
          scope="present", eff="present")
    d = s.to_dict()
    assert d["recheck_other"] == 1
    assert d["primary_present"] == 1


def test_synthesis_summary_temporal_out_blocked():
    """temporal=out_of_scope flattened effective_durant=not_present (consistent)."""
    s = SynthesisSummary()
    s.add(primary="not_present", recheck="confirmed_work_context_only",
          recheck_err=None, recheck_mode="always", temporal="out_of_scope",
          scope="not_present", eff="not_present")
    d = s.to_dict()
    assert d["temporal_out_blocked"] == 1


def test_synthesis_summary_temporal_recheck_conflict():
    """temporal=out_of_scope vs effective_durant=present → conflict."""
    s = SynthesisSummary()
    s.add(primary="not_present", recheck="reclassify_to_biographical",
          recheck_err=None, recheck_mode="always", temporal="out_of_scope",
          scope="ambiguous", eff="present")
    d = s.to_dict()
    assert d["temporal_recheck_conflict"] == 1
    assert d["scope_ambiguous"] == 1


def test_synthesis_summary_write_atomic(tmp_path):
    s = SynthesisSummary()
    s.add(primary="present", recheck=None, recheck_err=None,
          recheck_mode="always", temporal="in_scope",
          scope="present", eff="present")
    out_path = tmp_path / "synthesis_summary.json"
    s.write(out_path)
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["primary_present"] == 1
    assert payload["scope_present"] == 1
    # No .tmp leftover
    assert not (tmp_path / "synthesis_summary.json.tmp").exists()
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "SynthesisSummary or synthesis_summary"
```

Expected: ImportError.

- [ ] **Step 3: Implement `SynthesisSummary`**

Append to `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`:

```python
@dataclass
class SynthesisSummary:
    """Per-run counters for the synthesis pass (§4.6 F).

    Counters are pure decision-flow telemetry; cost/runtime is not tracked
    here — that lives in `recheck_summary.json` (§4.2 E).
    """

    primary_present: int = 0
    primary_not_present: int = 0
    primary_ambiguous: int = 0
    recheck_promoted: int = 0
    recheck_escalated: int = 0
    recheck_confirmed: int = 0
    recheck_errored: int = 0
    recheck_missing_anomaly: int = 0
    primary_wco_recheck_disabled: int = 0
    recheck_other: int = 0
    scope_present: int = 0
    scope_not_present: int = 0
    scope_ambiguous: int = 0
    temporal_out_blocked: int = 0
    temporal_recheck_conflict: int = 0

    def add(
        self,
        *,
        primary: str,
        recheck: Optional[str],
        recheck_err: Any,
        recheck_mode: str,
        temporal: Optional[str],
        scope: str,
        eff: str,
    ) -> None:
        # Primary durant distribution (scope-axis form).
        if primary == "present":
            self.primary_present += 1
        elif primary == "not_present":
            self.primary_not_present += 1
        elif primary == "ambiguous":
            self.primary_ambiguous += 1

        # Recheck outcome buckets (only meaningful when primary=WCO).
        if primary == "not_present":
            if recheck_err is not None:
                self.recheck_errored += 1
            elif recheck == "reclassify_to_biographical":
                self.recheck_promoted += 1
            elif recheck == "reclassify_to_ambiguous":
                self.recheck_escalated += 1
            elif recheck == "confirmed_work_context_only":
                self.recheck_confirmed += 1
            elif recheck is None:
                if recheck_mode == "never":
                    self.primary_wco_recheck_disabled += 1
                else:
                    self.recheck_missing_anomaly += 1
            # else: unknown recheck enum — not tracked here (synthesise_verdict
            # surfaces "unknown_recheck_verdict:<v>" in the rationale).
        else:
            # Recheck row for non-WCO primary: architectural invariant violation.
            if recheck is not None or recheck_err is not None:
                self.recheck_other += 1

        # Final scope distribution.
        if scope == "present":
            self.scope_present += 1
        elif scope == "not_present":
            self.scope_not_present += 1
        elif scope == "ambiguous":
            self.scope_ambiguous += 1

        # Temporal interaction.
        if temporal == "out_of_scope":
            if eff == "not_present":
                self.temporal_out_blocked += 1
            elif eff in ("present", "ambiguous"):
                self.temporal_recheck_conflict += 1

    def to_dict(self) -> dict[str, int]:
        return {
            "primary_present": self.primary_present,
            "primary_not_present": self.primary_not_present,
            "primary_ambiguous": self.primary_ambiguous,
            "recheck_promoted": self.recheck_promoted,
            "recheck_escalated": self.recheck_escalated,
            "recheck_confirmed": self.recheck_confirmed,
            "recheck_errored": self.recheck_errored,
            "recheck_missing_anomaly": self.recheck_missing_anomaly,
            "primary_wco_recheck_disabled": self.primary_wco_recheck_disabled,
            "recheck_other": self.recheck_other,
            "scope_present": self.scope_present,
            "scope_not_present": self.scope_not_present,
            "scope_ambiguous": self.scope_ambiguous,
            "temporal_out_blocked": self.temporal_out_blocked,
            "temporal_recheck_conflict": self.temporal_recheck_conflict,
        }

    def write(self, path: Path) -> None:
        """Atomic write: tmp+fsync+os.replace; tmp cleaned on exception."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "SynthesisSummary or synthesis_summary"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): SynthesisSummary counters + atomic write (§4.6 F)"
```

---

### Task 46: Wire orchestration into `Agent22ScopeCheck.run` + 2-arg backwards-compat shim

**Files:**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py`
- Modify: `~/projects/dsar-toolkit/tests/test_agent22_synthesis.py`

Per spec §4.6 (E): `Agent22ScopeCheck.run()` reads four inputs, calls `synthesise_verdict` per primary ref, writes `working/scope_verdicts.jsonl` atomically, and writes `working/synthesis_summary.json`. Also:
- Backwards-compat: the existing `_synthesise_verdict(durant, temporal)` 2-arg function stays, but emits `DeprecationWarning` and delegates to the 5-arg form with `recheck=None, recheck_err=None, recheck_mode="never"`.
- The existing `AgentAdapter._run_one` per-record interface is preserved BUT a new `run(case_dir)` entrypoint orchestrates the multi-input read + synthesis loop. Existing callers using the per-record adapter still work (they synthesise on the inputs they provide, no file IO).
- Atomic output: write to `working/scope_verdicts.jsonl.tmp`, then `os.replace`. On exception, clean tmp + re-raise. Each invocation of `run()` REPLACES the existing scope_verdicts.jsonl (idempotent: re-running synthesis on the same inputs produces the same file).

- [ ] **Step 1: Write failing tests — orchestration + shim**

Append to `tests/test_agent22_synthesis.py`:

```python
# ---- Agent22ScopeCheck.run orchestration ------------------------------------

from dsar_pipeline.agents.agent22_scope_check import (
    Agent22ScopeCheck, _synthesise_verdict,
)


def _make_case_dir(tmp_path: Path,
                    primary_rows: list[dict],
                    recheck_rows: list[dict] | None = None,
                    temporal_rows: list[dict] | None = None,
                    recheck_mode_effective: str = "always") -> Path:
    """Build a fixture case directory with the four input artefacts."""
    case_dir = tmp_path / "case_fixture"
    working = case_dir / "working"
    working.mkdir(parents=True, exist_ok=True)
    (working / "durant_verdicts.jsonl").write_text(
        "\n".join(json.dumps(r) for r in primary_rows) + "\n",
        encoding="utf-8",
    )
    if recheck_rows is not None:
        (working / "durant_underdisclosure_recheck.jsonl").write_text(
            "\n".join(json.dumps(r) for r in recheck_rows) + "\n",
            encoding="utf-8",
        )
    if temporal_rows is not None:
        (working / "temporal_verdicts.jsonl").write_text(
            "\n".join(json.dumps(r) for r in temporal_rows) + "\n",
            encoding="utf-8",
        )
    (working / "recheck_decision.json").write_text(
        json.dumps({
            "mode_requested": "auto",
            "mode_effective": recheck_mode_effective,
            "reason": "test_fixture",
        }),
        encoding="utf-8",
    )
    return case_dir


def test_run_writes_scope_verdicts_and_summary(tmp_path):
    case_dir = _make_case_dir(
        tmp_path,
        primary_rows=[
            {"case_id": "c1", "doc_ref": "D000001", "durant_verdict": "biographical"},
            {"case_id": "c1", "doc_ref": "D000002", "durant_verdict": "work_context_only"},
        ],
        recheck_rows=[
            {"case_id": "c1", "doc_ref": "D000002",
             "recheck_verdict": "reclassify_to_biographical",
             "error_state": None},
        ],
        temporal_rows=[
            {"case_id": "c1", "doc_ref": "D000001", "temporal_verdict": "in_scope"},
            {"case_id": "c1", "doc_ref": "D000002", "temporal_verdict": "in_scope"},
        ],
    )
    Agent22ScopeCheck().run(case_dir)
    scope_path = case_dir / "working" / "scope_verdicts.jsonl"
    assert scope_path.exists()
    rows = [json.loads(l) for l in scope_path.read_text(encoding="utf-8").splitlines() if l]
    by_ref = {r["doc_ref"]: r for r in rows}
    assert by_ref["D000001"]["scope_verdict"] == "present"
    assert by_ref["D000002"]["scope_verdict"] == "present"   # recheck promoted
    assert by_ref["D000002"]["evidence"]["effective_durant"] == "present"
    assert by_ref["D000002"]["evidence"]["recheck_verdict"] == "reclassify_to_biographical"
    summary_path = case_dir / "working" / "synthesis_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["recheck_promoted"] == 1
    assert summary["primary_present"] == 1
    assert summary["scope_present"] == 2


def test_run_missing_primary_jsonl_raises(tmp_path):
    """If durant_verdicts.jsonl is absent, the agent must raise (it's the
    REQUIRED input — recheck/temporal are optional)."""
    case_dir = tmp_path / "no_primary"
    (case_dir / "working").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        Agent22ScopeCheck().run(case_dir)


def test_run_missing_recheck_decision_defaults_to_always(tmp_path, caplog):
    """No recheck_decision.json → default mode_effective='always' + warning."""
    case_dir = _make_case_dir(
        tmp_path,
        primary_rows=[{"case_id": "c1", "doc_ref": "D1", "durant_verdict": "work_context_only"}],
    )
    # Delete the decision file.
    (case_dir / "working" / "recheck_decision.json").unlink()
    with caplog.at_level("WARNING"):
        Agent22ScopeCheck().run(case_dir)
    summary = json.loads(
        (case_dir / "working" / "synthesis_summary.json").read_text(encoding="utf-8")
    )
    # mode default to 'always' → missing recheck row counts as anomaly, not opt-out.
    assert summary["recheck_missing_anomaly"] == 1
    assert summary["primary_wco_recheck_disabled"] == 0


def test_run_malformed_recheck_decision_defaults_to_always(tmp_path, caplog):
    case_dir = _make_case_dir(
        tmp_path,
        primary_rows=[{"case_id": "c1", "doc_ref": "D1", "durant_verdict": "work_context_only"}],
    )
    (case_dir / "working" / "recheck_decision.json").write_text(
        "{not valid json}", encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        Agent22ScopeCheck().run(case_dir)
    summary = json.loads(
        (case_dir / "working" / "synthesis_summary.json").read_text(encoding="utf-8")
    )
    assert summary["recheck_missing_anomaly"] == 1


def test_run_idempotent_rerun_produces_same_output(tmp_path):
    """Running twice on the same inputs produces byte-identical output
    (atomic-replace semantics + deterministic field order)."""
    case_dir = _make_case_dir(
        tmp_path,
        primary_rows=[
            {"case_id": "c1", "doc_ref": "D000001", "durant_verdict": "biographical"},
            {"case_id": "c1", "doc_ref": "D000002", "durant_verdict": "work_context_only"},
        ],
        recheck_rows=[
            {"case_id": "c1", "doc_ref": "D000002",
             "recheck_verdict": "confirmed_work_context_only", "error_state": None},
        ],
        temporal_rows=[
            {"case_id": "c1", "doc_ref": "D000001", "temporal_verdict": "in_scope"},
            {"case_id": "c1", "doc_ref": "D000002", "temporal_verdict": "in_scope"},
        ],
    )
    agent = Agent22ScopeCheck()
    agent.run(case_dir)
    bytes_run1 = (case_dir / "working" / "scope_verdicts.jsonl").read_bytes()
    summary_run1 = (case_dir / "working" / "synthesis_summary.json").read_bytes()
    agent.run(case_dir)
    bytes_run2 = (case_dir / "working" / "scope_verdicts.jsonl").read_bytes()
    summary_run2 = (case_dir / "working" / "synthesis_summary.json").read_bytes()
    assert bytes_run1 == bytes_run2
    assert summary_run1 == summary_run2


def test_run_duplicate_ref_first_wins_in_indexes(tmp_path, caplog):
    """Duplicate doc_ref in recheck JSONL → first-wins; later rows logged."""
    case_dir = _make_case_dir(
        tmp_path,
        primary_rows=[
            {"case_id": "c1", "doc_ref": "D1", "durant_verdict": "work_context_only"},
        ],
        recheck_rows=[
            {"case_id": "c1", "doc_ref": "D1",
             "recheck_verdict": "reclassify_to_biographical", "error_state": None},
            {"case_id": "c1", "doc_ref": "D1",
             "recheck_verdict": "confirmed_work_context_only", "error_state": None},
        ],
        temporal_rows=[
            {"case_id": "c1", "doc_ref": "D1", "temporal_verdict": "in_scope"},
        ],
    )
    with caplog.at_level("WARNING"):
        Agent22ScopeCheck().run(case_dir)
    rows = [json.loads(l) for l in
            (case_dir / "working" / "scope_verdicts.jsonl").read_text(encoding="utf-8").splitlines()
            if l]
    # First wins → reclassify_to_biographical → scope=present.
    assert rows[0]["scope_verdict"] == "present"


def test_run_parse_error_in_recheck_recovers(tmp_path, caplog):
    """A malformed line in recheck JSONL is skipped; surrounding valid rows
    are still indexed; synthesis proceeds (the ref with no usable recheck
    row gets the missing-anomaly treatment)."""
    case_dir = _make_case_dir(
        tmp_path,
        primary_rows=[
            {"case_id": "c1", "doc_ref": "Dgood", "durant_verdict": "work_context_only"},
            {"case_id": "c1", "doc_ref": "Dbad", "durant_verdict": "work_context_only"},
        ],
        recheck_rows=None,    # we'll write a custom file
        temporal_rows=[
            {"case_id": "c1", "doc_ref": "Dgood", "temporal_verdict": "in_scope"},
            {"case_id": "c1", "doc_ref": "Dbad", "temporal_verdict": "in_scope"},
        ],
    )
    # Custom recheck JSONL: one valid row for Dgood, malformed line, no row for Dbad.
    (case_dir / "working" / "durant_underdisclosure_recheck.jsonl").write_text(
        json.dumps({"case_id": "c1", "doc_ref": "Dgood",
                    "recheck_verdict": "reclassify_to_biographical",
                    "error_state": None}) + "\n"
        + "{this is not valid json}\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        Agent22ScopeCheck().run(case_dir)
    rows = [json.loads(l) for l in
            (case_dir / "working" / "scope_verdicts.jsonl").read_text(encoding="utf-8").splitlines()
            if l]
    by_ref = {r["doc_ref"]: r for r in rows}
    assert by_ref["Dgood"]["scope_verdict"] == "present"        # promoted
    assert by_ref["Dbad"]["scope_verdict"] == "ambiguous"       # missing anomaly
    assert "recheck_expected_but_missing_for_ref" in by_ref["Dbad"]["decision_rationale"]


def test_run_e2e_30_docs_5_reclassified(tmp_path):
    """Phase 4 acceptance criterion: 30 primary docs, 5 reclassified by
    recheck to biographical → scope_verdicts shows scope=present for those
    5, synthesis_summary records recheck_promoted=5."""
    primary_rows = []
    recheck_rows = []
    temporal_rows = []
    # 10 primary biographical (will pass through unchanged)
    for i in range(10):
        primary_rows.append({"case_id": "c1", "doc_ref": f"BIO{i:03d}",
                              "durant_verdict": "biographical"})
        temporal_rows.append({"case_id": "c1", "doc_ref": f"BIO{i:03d}",
                               "temporal_verdict": "in_scope"})
    # 15 primary WCO confirmed by recheck (stay not_present)
    for i in range(15):
        ref = f"WCO{i:03d}"
        primary_rows.append({"case_id": "c1", "doc_ref": ref,
                              "durant_verdict": "work_context_only"})
        recheck_rows.append({"case_id": "c1", "doc_ref": ref,
                              "recheck_verdict": "confirmed_work_context_only",
                              "error_state": None})
        temporal_rows.append({"case_id": "c1", "doc_ref": ref,
                               "temporal_verdict": "in_scope"})
    # 5 primary WCO reclassified to biographical (the safety net firing)
    for i in range(5):
        ref = f"PROMO{i:03d}"
        primary_rows.append({"case_id": "c1", "doc_ref": ref,
                              "durant_verdict": "work_context_only"})
        recheck_rows.append({"case_id": "c1", "doc_ref": ref,
                              "recheck_verdict": "reclassify_to_biographical",
                              "error_state": None})
        temporal_rows.append({"case_id": "c1", "doc_ref": ref,
                               "temporal_verdict": "in_scope"})

    case_dir = _make_case_dir(
        tmp_path,
        primary_rows=primary_rows,
        recheck_rows=recheck_rows,
        temporal_rows=temporal_rows,
    )
    Agent22ScopeCheck().run(case_dir)

    rows = [json.loads(l) for l in
            (case_dir / "working" / "scope_verdicts.jsonl").read_text(encoding="utf-8").splitlines()
            if l]
    assert len(rows) == 30
    by_ref = {r["doc_ref"]: r for r in rows}
    for i in range(5):
        ref = f"PROMO{i:03d}"
        assert by_ref[ref]["scope_verdict"] == "present"
        assert by_ref[ref]["evidence"]["effective_durant"] == "present"

    summary = json.loads(
        (case_dir / "working" / "synthesis_summary.json").read_text(encoding="utf-8")
    )
    assert summary["recheck_promoted"] == 5
    assert summary["recheck_confirmed"] == 15
    assert summary["primary_present"] == 10
    assert summary["scope_present"] == 15
    assert summary["scope_not_present"] == 15
    assert summary["scope_ambiguous"] == 0


# ---- Backwards-compat _synthesise_verdict(durant, temporal) -----------------

def test_legacy_synthesise_verdict_2arg_still_works():
    """Out-of-tree callers using the 2-arg form keep working."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        verdict, rationale = _synthesise_verdict("biographical", "in_scope")
    assert verdict == "present"
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_legacy_synthesise_verdict_2arg_temporal_out_of_scope():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        verdict, rationale = _synthesise_verdict("biographical", "out_of_scope")
    # In the legacy shim, recheck=None+mode=never path → effective_durant=
    # "present"; temporal=out_of_scope routes to ambiguous via conflict-surfacing.
    assert verdict == "ambiguous"


def test_legacy_synthesise_verdict_2arg_work_context_only():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        verdict, rationale = _synthesise_verdict("work_context_only", "in_scope")
    # Legacy: recheck=None + mode=never → primary_wco_recheck_disabled → not_present.
    assert verdict == "not_present"
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_agent22_synthesis.py -v -k "test_run_ or legacy_synthesise"
```

Expected: failures because `Agent22ScopeCheck.run` does not exist and `_synthesise_verdict` returns 2-tuple by old logic (not the shim path).

- [ ] **Step 3: Implement `Agent22ScopeCheck.run` + shim + replace existing `_synthesise_verdict`**

Replace the existing module-level `_synthesise_verdict` function and `Agent22ScopeCheck` class in `~/projects/dsar-toolkit/src/dsar_pipeline/agents/agent22_scope_check.py` with:

```python
# ---------------------------------------------------------------------------
# Backwards-compat 2-arg shim
# ---------------------------------------------------------------------------


def _synthesise_verdict(
    durant: Optional[str],
    temporal: Optional[str],
) -> tuple[str, str]:
    """DEPRECATED: 2-arg form retained for out-of-tree callers.

    Delegates to the 5-arg `synthesise_verdict(primary, recheck, recheck_err,
    recheck_mode, temporal)` with `recheck=None, recheck_err=None,
    recheck_mode='never'` — i.e. recheck is treated as disabled. This means
    the legacy semantics for `durant='work_context_only'` map to
    `effective_durant=not_present (primary_wco_recheck_disabled)`, which
    matches what the original 2-arg function returned.

    New callers must use `synthesise_verdict(...)` directly.
    """
    warnings.warn(
        "_synthesise_verdict(durant, temporal) is deprecated; use "
        "synthesise_verdict(primary, recheck, recheck_err, recheck_mode, temporal) "
        "from agent22_scope_check.",
        DeprecationWarning,
        stacklevel=2,
    )
    if durant is None:
        # Original 2-arg form treated None primary as ambiguous fall-through.
        primary = "ambiguous"
    else:
        try:
            primary = _normalise_primary(durant)
        except ValueError:
            primary = "ambiguous"
    scope, rationale, _eff = synthesise_verdict(
        primary, None, None, "never", temporal
    )
    return scope, rationale


# ---------------------------------------------------------------------------
# Agent22ScopeCheck — orchestrates the synthesis pass on a case directory
# ---------------------------------------------------------------------------


_DEFAULT_RECHECK_MODE = "always"


def _load_recheck_mode_effective(case_dir: Path) -> str:
    """Read recheck_decision.json:mode_effective with safe defaults.

    Missing file or malformed JSON → return _DEFAULT_RECHECK_MODE ("always")
    + warn. The safe default is 'always' because the alternative — silently
    treating recheck as disabled — would convert recheck-expected anomalies
    into 'primary_wco_recheck_disabled' counters, masking under-disclosure.
    """
    path = case_dir / "working" / "recheck_decision.json"
    if not path.exists():
        _log.warning(
            "recheck_decision.json missing at %s; defaulting mode_effective=%r",
            path, _DEFAULT_RECHECK_MODE,
        )
        return _DEFAULT_RECHECK_MODE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.warning(
            "recheck_decision.json unreadable (%s); defaulting mode_effective=%r",
            e, _DEFAULT_RECHECK_MODE,
        )
        return _DEFAULT_RECHECK_MODE
    mode = data.get("mode_effective")
    if mode not in ("always", "never"):
        _log.warning(
            "recheck_decision.json:mode_effective=%r unexpected; defaulting to %r",
            mode, _DEFAULT_RECHECK_MODE,
        )
        return _DEFAULT_RECHECK_MODE
    return mode


class Agent22ScopeCheck(AgentAdapter):
    """Agent 22 — Scope Check (Stage 2.5).

    Two entrypoints:
      1. `_run_one(record)` — per-record adapter form (kept for the agent
         registry / batch runner). Falls back to the legacy 2-arg
         synthesis using whatever durant/temporal are on the record.
      2. `run(case_dir)` — multi-input orchestrator: reads durant_verdicts.jsonl,
         durant_underdisclosure_recheck.jsonl, temporal_verdicts.jsonl,
         and recheck_decision.json from `case_dir/working/`; writes
         scope_verdicts.jsonl (atomic) + synthesis_summary.json.
    """

    agent_id = "agent22_scope_check"

    # ---------------- per-record adapter (legacy callers) ----------------

    def _run_one(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        doc_ref = record.get("doc_ref")
        if not doc_ref:
            return []
        case_id = record.get("case_id", "")
        durant = record.get("durant_verdict")
        temporal = record.get("temporal_verdict")
        # Legacy adapter path: emits the 2-arg synthesis (no recheck).
        scope, rationale = _synthesise_verdict(durant, temporal)
        return [
            {
                "case_id": case_id,
                "doc_ref": doc_ref,
                "scope_verdict": scope,
                "evidence": {
                    "durant_verdict": durant,
                    "temporal_verdict": temporal,
                },
                "decision_rationale": rationale,
            }
        ]

    # ---------------- multi-input orchestrator ----------------

    def run(self, case_dir: Path) -> None:
        """Synthesise scope_verdicts.jsonl + synthesis_summary.json from
        the four input artefacts in `case_dir/working/`.

        Raises:
          FileNotFoundError if working/durant_verdicts.jsonl is absent (it's
          the REQUIRED primary input — recheck/temporal are optional).
        """
        working = case_dir / "working"
        primary_path = working / "durant_verdicts.jsonl"
        if not primary_path.exists():
            raise FileNotFoundError(
                f"required input missing: {primary_path}; "
                f"run gate_durant before Agent22 synthesis."
            )

        recheck_index = _build_index_first_wins(
            working / "durant_underdisclosure_recheck.jsonl",
            name="recheck",
            key_field="doc_ref",
        )
        temporal_index = _build_index_first_wins(
            working / "temporal_verdicts.jsonl",
            name="temporal",
            key_field="doc_ref",
        )
        recheck_mode = _load_recheck_mode_effective(case_dir)

        scope_path = working / "scope_verdicts.jsonl"
        tmp_path = scope_path.with_suffix(scope_path.suffix + ".tmp")
        summary = SynthesisSummary()
        seen_refs: set[str] = set()

        working.mkdir(parents=True, exist_ok=True)
        try:
            with open(tmp_path, "w", encoding="utf-8") as out_fh:
                for primary_row in _iter_jsonl_safe(primary_path, "primary"):
                    doc_ref = primary_row.get("doc_ref")
                    if not isinstance(doc_ref, str) or not doc_ref:
                        _log.warning(
                            "primary row missing doc_ref; skipped: %r",
                            primary_row,
                        )
                        continue
                    if doc_ref in seen_refs:
                        _log.warning(
                            "primary duplicate doc_ref=%r; first-wins",
                            doc_ref,
                        )
                        continue
                    seen_refs.add(doc_ref)

                    case_id = primary_row.get("case_id", "")
                    raw_durant = primary_row.get("durant_verdict")
                    try:
                        primary = _normalise_primary(raw_durant)
                    except ValueError as e:
                        _log.warning(
                            "primary doc_ref=%s has unknown verdict %r; "
                            "treating as ambiguous: %s",
                            doc_ref, raw_durant, e,
                        )
                        primary = "ambiguous"

                    recheck_row = recheck_index.get(doc_ref) or {}
                    recheck_verdict = recheck_row.get("recheck_verdict")
                    recheck_err = recheck_row.get("error_state")

                    temporal_row = temporal_index.get(doc_ref) or {}
                    temporal = temporal_row.get("temporal_verdict")

                    scope, rationale, eff = synthesise_verdict(
                        primary,
                        recheck_verdict,
                        recheck_err,
                        recheck_mode,
                        temporal,
                    )

                    output_row = {
                        "case_id": case_id,
                        "doc_ref": doc_ref,
                        "scope_verdict": scope,
                        "evidence": {
                            "durant_verdict": raw_durant,
                            "recheck_verdict": recheck_verdict,
                            "error_state": _trim_error_state(recheck_err),
                            "recheck_mode_effective": recheck_mode,
                            "effective_durant": eff,
                            "temporal_verdict": temporal,
                        },
                        "decision_rationale": rationale,
                    }
                    out_fh.write(
                        json.dumps(output_row, ensure_ascii=False, sort_keys=True) + "\n"
                    )

                    summary.add(
                        primary=primary,
                        recheck=recheck_verdict,
                        recheck_err=recheck_err,
                        recheck_mode=recheck_mode,
                        temporal=temporal,
                        scope=scope,
                        eff=eff,
                    )
                out_fh.flush()
                os.fsync(out_fh.fileno())
            os.replace(tmp_path, scope_path)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

        summary.write(working / "synthesis_summary.json")
```

Delete the OLD `Agent22ScopeCheck` class definition + the OLD `_synthesise_verdict(durant, temporal)` function from the module (replaced by the new versions above). Also delete the now-obsolete docstring text block that documents the old per-record-only shape — the new module docstring at the top of the file documents both entrypoints.

- [ ] **Step 4: Update the module-level docstring**

Replace the existing module-level docstring at the top of `agent22_scope_check.py` with:

```python
"""Agent 22 — Scope Check (Stage 2.5).

Synthesises per-ref `scope_verdict` (present / not_present / ambiguous) from
the upstream gate signals. Two entrypoints:

  1. `Agent22ScopeCheck._run_one(record)` — per-record adapter form, kept
     for the legacy AgentAdapter batch runner. Uses the deprecated 2-arg
     synthesis (no recheck).
  2. `Agent22ScopeCheck.run(case_dir)` — multi-input orchestrator (§4.6).
     Reads:
       - working/durant_verdicts.jsonl     (Phase 2 dual-write; REQUIRED)
       - working/durant_underdisclosure_recheck.jsonl (§4.2 output; optional)
       - working/temporal_verdicts.jsonl   (optional)
       - working/recheck_decision.json     (§4.2 D; optional, default mode=always)
     Writes:
       - working/scope_verdicts.jsonl      (atomic tmp+replace)
       - working/synthesis_summary.json    (counters per §4.6 F)

Spec: docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md §4.6

Decision flow:
  1. Normalise primary gate verdict from gate-vocabulary
     (biographical/work_context_only) to scope-axis form
     (present/not_present) via _normalise_primary.
  2. effective_durant(primary, recheck, recheck_err, recheck_mode):
     8-branch durant-axis decision (recheck wins for primary=WCO; mode
     determines None-recheck interpretation).
  3. synthesise_verdict overlays temporal:
       - temporal=out_of_scope does NOT silently flatten ambiguous to
         not_present — recheck-promoted refs with conflicting temporal
         escalate to ambiguous (operator-reconcilable).

Backwards compat: `_synthesise_verdict(durant, temporal)` 2-arg form is
retained as a DeprecationWarning-emitting shim that delegates to the
5-arg form with recheck=None, recheck_err=None, recheck_mode='never'.
"""
```

- [ ] **Step 5: Run all Phase 4 tests; verify pass**

```bash
cd ~/projects/dsar-toolkit
uv run pytest tests/test_agent22_synthesis.py -v
```

Expected: ALL tests pass (schema + safe_extract + normalise_primary + effective_durant + synthesise_verdict + _truncate_any + _trim_error_state + _iter_jsonl_safe + _build_index_first_wins + SynthesisSummary + run + legacy shim).

- [ ] **Step 6: Run the existing scope-decisions test suite; verify still pass**

```bash
uv run pytest tests/test_scope_decisions.py -v
```

Expected: PASS (existing scope-decisions tests use the per-record adapter path through `Agent22ScopeCheck._run_one` — that path still uses the legacy semantics via the shim, which produces the same outputs as before for the inputs those tests exercise).

If any of those tests fail because the legacy `_synthesise_verdict` is now emitting a `DeprecationWarning`, suppress it with `pytest.warns(DeprecationWarning)` in the test, or — preferred — update the test to use the new 5-arg form directly. (Decision deferred to the executing agent: read the test first, then decide. The constraint is that `test_scope_decisions.py` MUST keep passing.)

- [ ] **Step 7: Run the full toolkit test suite to confirm no other breakage**

```bash
uv run pytest -v 2>&1 | tail -40
```

Expected: no NEW failures vs the Phase 3 baseline. Pre-existing failures (if any) are tracked separately.

- [ ] **Step 8: Commit**

```bash
git add src/dsar_pipeline/agents/agent22_scope_check.py tests/test_agent22_synthesis.py
git commit -m "feat(agent22): 5-arg synthesise_verdict + run orchestrator + 2-arg shim (§4.6)"
```

---

## Acceptance criteria for Phase 4

Phase 4 is done when ALL of these hold:

- [ ] `tests/test_agent22_synthesis.py` is green and covers:
  - `effective_durant` full 8-branch table (Task 41 parametrised test) + unknown-recheck-value + recheck_err precedence
  - `synthesise_verdict` 9-cell cartesian (effective × temporal) + recheck-promoted + recheck-error + mode-never + missing-anomaly
  - `_safe_extract_error_code` defensive paths (dict-with-code / dict-no-code / string / None / unexpected types / truncation)
  - `_normalise_primary` (3 gate-vocab → 3 scope-axis + passthrough + raises-on-unknown)
  - `_truncate_any` (primitives preserved + long strings truncated + huge container descriptor + str fallback + copy-on-success)
  - `_trim_error_state` (None passthrough / preserves code+message / drops raw / _extra for unknown keys / large containers / non-mapping → None)
  - `_iter_jsonl_safe` + `_build_index_first_wins` (missing file / malformed JSON / huge lines / non-UTF-8 / duplicates / missing key field)
  - `SynthesisSummary` (all 15 counters + atomic write)
  - `Agent22ScopeCheck.run` (writes both files / raises on missing primary / defaults mode=always on missing/malformed decision file / idempotent rerun / duplicate ref first-wins / parse-error recovery / 30-doc e2e)
  - Legacy 2-arg `_synthesise_verdict` shim emits `DeprecationWarning` and delegates correctly
- [ ] `tests/test_scope_decisions.py` continues to pass (existing per-record adapter behaviour preserved via the shim).
- [ ] End-to-end fixture (Task 46 step 3 `test_run_e2e_30_docs_5_reclassified`): 30 docs, 5 reclassified to biographical via recheck → `scope_verdicts.jsonl` shows `scope_verdict=present` for those 5 refs; `synthesis_summary.json` records `recheck_promoted: 5`.
- [ ] `working/scope_verdicts.jsonl` rows validate against the extended schema; existing pre-§4.6 rows (legacy array-evidence form) still validate.
- [ ] Backwards-compat: any out-of-tree caller using the old 2-arg `_synthesise_verdict(durant, temporal)` still works with `DeprecationWarning`.
- [ ] All commits are atomic (one logical unit per commit; ≥1 commit per task).

## Self-review

**Spec coverage (Phase 4 only — spec §4.6):**

| Spec subsection | Task(s) | Status |
|---|---|---|
| §4.6 (A) `effective_durant` 8-branch helper | 41 | covered |
| §4.6 (B) 5-arg `synthesise_verdict` with conflict surfacing | 42 | covered |
| §4.6 (C) Field names (`recheck_verdict`, `error_state`) | 38, 46 | covered (schema + row writer) |
| §4.6 (D) `scope_verdicts.jsonl` row shape (extended evidence) | 38, 46 | covered |
| §4.6 (E) Pipeline orchestration in `Agent22ScopeCheck.run` | 46 | covered |
| §4.6 (F) `SynthesisSummary` counters | 45 | covered |
| §4.6 (G) `_trim_error_state` + `_truncate_any` (audit footprint) | 43 | covered |
| §4.6 v8 `_iter_jsonl_safe` + `_build_index_first_wins` | 44 | covered |
| Helpers: `_safe_extract_error_code` (defensive) | 39 | covered |
| Helpers: `_normalise_primary` (gate→scope vocab) | 40 | covered |
| Backwards-compat: 2-arg `_synthesise_verdict` shim | 46 | covered |
| Schema extension (backwards-compat object/array via oneOf) | 38 | covered |

**Out of scope for Phase 4 (covered in later phases or follow-ups):**
- `working/durant_verdicts.jsonl` is produced by Phase 2's dual-write (`GateDurant._persist_verdicts`). Phase 4 only READS it.
- `working/durant_underdisclosure_recheck.jsonl` and `working/recheck_decision.json` are produced by Phase 3's `RecheckStage`. Phase 4 only READS them.
- `scope_check_stage.py` is NOT modified in this phase — it's the orchestrating stage that runs primary durant + temporal in parallel and produces `biographical_refs.json` + (Phase 2) `durant_verdicts.jsonl`. The Phase 3 plan extended it to invoke `RecheckStage` after primary durant when configured. Phase 4's `Agent22ScopeCheck.run(case_dir)` is invoked LATER — either by `scope_check_stage` after recheck completes (Phase 3 wiring), or by the existing `dsar-scope-check` CLI's final synthesis step. The Phase 3 plan determined where exactly the `Agent22ScopeCheck.run(case_dir)` call is wired; Phase 4 only provides the implementation.
- Migrating `_run_one` per-record callers to the 5-arg form: deferred. The shim path is sufficient indefinitely; in-tree callers (`scope_check_stage._synthesise_verdict`) keep working unchanged.
- `working/temporal_verdicts.jsonl` is assumed to exist (produced by `gate_temporal_scope` via the existing `scope_check_stage`). If it doesn't exist in older case directories, `_build_index_first_wins` returns `{}` and synthesis proceeds with `temporal=None` per ref — the existing fallback behaviour from the 2-arg form's `_synthesise_verdict("biographical", None)` → `present` is preserved.
- Mid-pass cost-budget abort, ambiguous-recheck, multi-pass recheck-of-recheck — explicit §4.6 OUT-of-scope items per spec.

**Placeholder scan:** None. Every Step has full code; every command has full args.

**Type consistency:**
- `effective_durant` and `synthesise_verdict` return tuples (2-tuple and 3-tuple respectively) used identically across Tasks 41, 42, 46. ✓
- `_truncate_any` returns `(Any, bool)` used by `_trim_error_state` (Task 43) without re-typing. ✓
- `_iter_jsonl_safe` returns `Iterator[dict]` consumed by `_build_index_first_wins` (Task 44). ✓
- `SynthesisSummary.add(...)` keyword-only args match the call site in `Agent22ScopeCheck.run` (Task 46). ✓
- `_safe_extract_error_code(Any) → str` called from `effective_durant` (Task 41) with `recheck_err` (`Any`); type-consistent. ✓
- The output row's `evidence` block fields match the schema additions in Task 38 (`durant_verdict`, `recheck_verdict`, `error_state`, `recheck_mode_effective`, `effective_durant`, `temporal_verdict`). ✓

**Decisions deviating from spec (intentional):**
- The schema's `evidence` field is extended via `oneOf [array, object]` rather than a hard-replace with the new object form. Rationale: pre-§4.6 rows (written by the legacy `_synthesise_verdict` path or the LLM `scope_check` role) use the array form; a hard-replace would break re-validation of historical case data. The `oneOf` is the minimum-disruption schema change and matches the spec's "backwards-compat" requirement in §10.1's `scope_verdict.schema.json` row.
- The legacy 2-arg `_synthesise_verdict(durant, temporal)` delegates to the 5-arg form with `recheck_mode='never'` (NOT `'always'`) so that `durant='work_context_only'` maps to `effective_durant=not_present (primary_wco_recheck_disabled)` — which is what the original function returned (work_context_only → not_present). Using `mode='always'` would change the legacy semantics (work_context_only → ambiguous via missing-anomaly), breaking the backwards-compat guarantee.
- `_run_one` (per-record adapter) keeps using the 2-arg shim — i.e. it does not READ the recheck JSONL. Rationale: the per-record adapter contract is per-record, not per-case-directory; injecting file IO into `_run_one` would break the AgentAdapter interface. Callers wanting recheck-aware synthesis must use `Agent22ScopeCheck.run(case_dir)`.
- `recheck_mode_effective` defaults to `'always'` on missing/malformed `recheck_decision.json` (safe default per spec §4.6 E). The legacy shim defaults to `'never'` instead, because the legacy callers never had a recheck stage to opt into.

---

*End of Phase 4 plan. Continue with Phase 5 plan (covers spec §4.4 fitness canary + conductor pre-flight, including end-to-end fixtures that exercise the synthesis path landed here).*
