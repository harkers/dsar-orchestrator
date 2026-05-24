# Contract B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. (This repo has been using Ralph Loop instead of either of those for the last two contract deliveries — that's also acceptable; the plan structure works the same.)

**Goal:** Close dsar-orchestrator#10/#11/#12 with three minimal source changes + a durable "Contract B" principle (conductor adapter set tracks what the toolkit actually ships), enforced by a new AST-walk test. Ship as v0.4.0.

**Architecture:** One feature branch (`feat/contract-b`), four commits (one per issue + one for the contract codification & version bump). #10 removes the duplicate `pii_discovery` stage; #11 introduces a new `adapters/rerank.py` wrapping `dsar_clients.tei_rerank_client.rerank_pairs` (mirror of the existing embed adapter); #12 makes the conductor's `check_pii_classify` agent tolerate an empty `pii_collection.jsonl` when scope_classify produced zero `"present"` verdicts. Contract B's principle lives in `VERSIONING.md §4` plus a new `tests/test_contract_b_no_fictional_modules.py` that AST-walks the conductor sources for every `dsar_*` lazy-import string and asserts each resolves against the installed toolkit (gated by `@pytest.mark.needs_toolkit`).

**Tech Stack:** Python 3.14, pytest, ast (stdlib), importlib (stdlib), dsar-toolkit (HEAD or pinned via pyproject.toml). Runtime stack unchanged.

**Spec:** `docs/superpowers/specs/2026-05-24-contract-b-design-v1.md` (commit `12ad4fd` on `main`).

**Test runner:** `.venv/bin/pytest -q` (NOT `uv run pytest` — the toolkit's extras break uv's resolver in this venv). Real-toolkit smoke: `.venv/bin/pytest -q -m needs_toolkit`.

---

## File structure (created/modified/deleted across all tasks)

### Created
- `src/dsar_orchestrator/adapters/rerank.py` — new TEI-rerank adapter (Task 2)
- `tests/test_adapter_rerank.py` — 5 tests for the new adapter (Task 2)
- `tests/test_module_agent_pii_classify.py` — 3 tests for smart-empty tolerance (Task 3)
- `tests/test_contract_b_no_fictional_modules.py` — AST-walk Contract B enforcement (Task 4)

### Modified
- `src/dsar_orchestrator/pipeline.py` — Tasks 1 & 2 (remove `_run_pii_discovery`, rewire `_run_scope_filter_chain` to call adapter)
- `src/dsar_orchestrator/stages.py` — Task 1 (delete `STAGE_ARTEFACTS["pii_discovery"]` + unused `_hash_register_plus_scope`)
- `src/dsar_orchestrator/module_agents.py` — Tasks 1 & 3 (delete `check_pii_discovery` + entry; update `check_pii_classify`)
- `src/dsar_orchestrator/config.py` — Task 1 (drop `_resolve_bool` for `DISCOVERY_ENABLED`; keep field as no-op)
- `src/dsar_orchestrator/adapters/pii_classify.py` — Task 3 (bump `PRODUCER_VERSION` to `0.4.0`)
- `src/dsar_orchestrator/__init__.py` — Task 4 (add Contract B pointer in module docstring + bump `__version__`)
- `pyproject.toml` — Task 4 (`version = "0.4.0"`)
- `VERSIONING.md` — Task 4 (new §4 *Toolkit-coupling contract*)
- `CHANGELOG.md` — Task 4 (`[Unreleased]` → `0.4.0` block)
- `tests/_toolkit_stubs/stubs.py` — Tasks 1 & 2 (delete `make_pii_discovery_stub`; delete `make_rerank_core_stub`; add `make_tei_rerank_client_stub`)
- `tests/integration/test_real_toolkit_smoke.py` — Task 4 (add `EXPECTED_TOOLKIT_MODULES` list)
- `tests/test_stages.py`, `tests/test_resume_cascade.py`, `tests/test_module_agents.py`, `tests/integration/test_synthetic_case_100.py`, `tests/integration/test_full_pipeline_with_stubs.py` — Task 1 (drop `pii_discovery` from expected stage sets)

### Deleted (none — only function/entry deletions within files)

---

## Task 0: Worktree setup, baseline tests, file companion issues

**Files:** none in repo; this is environmental setup + issue filing.

- [ ] **Step 1: Create the worktree off main**

Run:
```bash
cd /Users/stu/projects/dsar-orchestrator
git fetch origin main
git worktree add .worktrees/contract-b -b feat/contract-b origin/main
cd .worktrees/contract-b
```

Expected: "Preparing worktree (new branch 'feat/contract-b')". Verify `git status` is clean and `git rev-parse HEAD` matches `origin/main`.

- [ ] **Step 2: Pin the conductor editable install to the worktree**

The venv at `/Users/stu/projects/dsar-orchestrator/.venv` may currently be pinned to a different path. Re-pin:

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
VIRTUAL_ENV=../../.venv uv pip install -e .
```

Expected: `Installed 1 package: dsar-orchestrator==0.3.0 (from file:///.../contract-b)`.

- [ ] **Step 3: Run baseline tests**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest -q
```

Expected: `292 passed, 1 skipped` (skipped is real-toolkit-smoke when spaCy model absent; if spaCy installed: `293 passed`).

If the count differs, STOP and reconcile against `main` before proceeding.

- [ ] **Step 4: Run baseline import-linter**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/lint-imports
```

Expected: `Contracts: 9 kept, 0 broken.`

- [ ] **Step 5: File the toolkit-side companion issue**

```bash
gh issue create --repo harkers/dsar-toolkit \
  --title "pii_classifier_stage: write working/pii_collection.jsonl aggregating per-stage findings" \
  --body "$(cat <<'EOF'
## Symptom

`dsar_pipeline.pii_classifier_stage.run()` writes per-stage findings to `~/.dsar-audit/<case>/pii_findings_stage{1,2,3}.jsonl`. Nothing aggregates them. The dsar-orchestrator (conductor) expects a single `<case>/working/pii_collection.jsonl` for its redact stage + module-agent. Cross-ref: dsar-orchestrator#12 (conductor-side interim tolerance ships in conductor v0.4.0 / Contract B).

## Request

At the end of `run()` in `src/dsar_pipeline/pii_classifier_stage.py`, concatenate the 3 stage files into `<case>/working/pii_collection.jsonl`, one row per finding:

\`\`\`json
{"ref": "...", "finding_type": "...", "surface": "...", "confidence": 0.99, "source_stage": 1, "source_detector": "presidio", "schema_version": "1.0", "producer_version": "dsar_pipeline.pii_classifier_stage <ver>"}
\`\`\`

No new env-flags; aggregation always-on. Atomic write (tmp + rename).

## Acceptance

- Running the toolkit alone produces `working/pii_collection.jsonl` whose row count equals the sum of `pii_findings_stage{1,2,3}.jsonl` line counts.
- Empty per-stage files produce an empty (but present) `pii_collection.jsonl`.
- Schema version present on every row.

Once this lands, the conductor's interim smart-empty tolerance retires (tracked in dsar-orchestrator follow-up issue).
EOF
)"
```

Expected: prints the new toolkit issue URL. Record the number (call it `<TOOLKIT_ISSUE>`).

- [ ] **Step 6: File the conductor-side follow-up issue**

```bash
gh issue create --repo harkers/dsar-orchestrator \
  --title "pivot adapters/pii_classify.py to consume toolkit-shipped pii_collection.jsonl" \
  --body "$(cat <<'EOF'
## Background

Contract B v0.4.0 (PR pending) ships an interim smart-empty tolerance in `check_pii_classify` because the toolkit currently writes per-stage findings to `~/.dsar-audit/<case>/pii_findings_stage{1,2,3}.jsonl` with no aggregated `working/pii_collection.jsonl`. Toolkit issue: harkers/dsar-toolkit#<TOOLKIT_ISSUE>.

## Request

Once the toolkit lands `working/pii_collection.jsonl` aggregation:

1. `src/dsar_orchestrator/adapters/pii_classify.py`: remove any conductor-side path workarounds; consume the toolkit's file directly.
2. `src/dsar_orchestrator/module_agents.py::check_pii_classify`: remove the smart-empty branch (the `in_scope_count == 0 → _ok(...)` short-circuit added in Contract B). Restore strict `_critical` when `pii_collection.jsonl` is missing/empty.
3. Bump `PRODUCER_VERSION` on `adapters/pii_classify.py`.
4. Update CHANGELOG: `### Changed`: "removed Contract B interim smart-empty tolerance now that toolkit ships aggregated pii_collection.jsonl".

## Acceptance

- `tests/test_module_agent_pii_classify.py` either updated to reflect strict behaviour or removed (its 3 tests covered interim semantics).
- Cross-test against real toolkit completes pii_classify with a populated pii_collection.jsonl (not the info short-circuit).

Blocked by: harkers/dsar-toolkit#<TOOLKIT_ISSUE>.
EOF
)"
```

Expected: prints the new conductor follow-up issue URL.

- [ ] **Step 7: No commit — Task 0 produces only environmental + GitHub state**

Move to Task 1.

---

## Task 1: Remove `pii_discovery` stage (closes #10)

**Files:**
- Modify: `src/dsar_orchestrator/pipeline.py:42, 57, 303-308, 561-581`
- Modify: `src/dsar_orchestrator/stages.py:69-79, 184-189`
- Modify: `src/dsar_orchestrator/module_agents.py:209-225, 602-617`
- Modify: `src/dsar_orchestrator/config.py:74-75, 161`
- Modify: `tests/_toolkit_stubs/stubs.py` (delete `make_pii_discovery_stub` + registration)
- Modify: `tests/test_stages.py`, `tests/test_resume_cascade.py`, `tests/test_module_agents.py`, `tests/integration/test_synthetic_case_100.py`, `tests/integration/test_full_pipeline_with_stubs.py` (drop `pii_discovery` from expected sets)

This task uses inverted-TDD: update tests first to assert the *new* expected shape (no pii_discovery), watch them fail, then delete the implementation.

- [ ] **Step 1: Audit every pii_discovery reference to know the deletion surface**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
grep -rn 'pii_discovery\|discover_entities\|dsar_pii_discovery' src/ tests/
```

Expected: matches in the files listed above. Confirm no surprises (e.g., docs you forgot to mention).

- [ ] **Step 2: Update `tests/test_stages.py` — remove pii_discovery from expectations**

Find every test asserting `"pii_discovery"` is in `STAGE_ARTEFACTS`, `SUB_STAGES_BY_STAGE["stage_2_parallel"]`, or similar. Replace expected sets to exclude `"pii_discovery"`.

Use `grep -n pii_discovery tests/test_stages.py` to find them, then edit. Common pattern to replace:

```python
# Before
assert "pii_discovery" in STAGE_ARTEFACTS
# After: delete the assertion (and any sibling test asserting its absence after this change is fine)
```

For the SUB_STAGES_BY_STAGE assertion:

```python
# Before
assert SUB_STAGES_BY_STAGE["stage_2_parallel"] == ("embed", "detect_2_1_to_2_4", "pii_discovery")
# After
assert SUB_STAGES_BY_STAGE["stage_2_parallel"] == ("embed", "detect_2_1_to_2_4")
```

- [ ] **Step 3: Update `tests/test_resume_cascade.py`**

```bash
grep -n pii_discovery tests/test_resume_cascade.py
```

For each test that lists pii_discovery in an expected-stages set (e.g., `stages_run` contains `"pii_discovery"`, or a fixture writes `working/pii_discovery.jsonl`), remove the pii_discovery entry. Keep the test's intent (cascade ordering) intact.

- [ ] **Step 4: Update `tests/test_module_agents.py`**

```bash
grep -n pii_discovery tests/test_module_agents.py
```

Delete any `test_check_pii_discovery_*` function bodies. Remove `pii_discovery` from CHECKERS-registry assertions (e.g., `assert "pii_discovery" in CHECKERS` → delete the assertion).

- [ ] **Step 5: Update `tests/integration/test_synthetic_case_100.py`**

Remove `"pii_discovery"` from any tuple/set of expected stages or sub-stages. Don't touch unrelated assertions.

- [ ] **Step 6: Update `tests/integration/test_full_pipeline_with_stubs.py`**

Same — remove `"pii_discovery"` from expected sets; keep everything else.

- [ ] **Step 7: Update `tests/_toolkit_stubs/stubs.py` — remove pii_discovery stub from `all_stubs()`**

Edit `tests/_toolkit_stubs/stubs.py`: in `all_stubs()`, remove the `"dsar_pii_discovery.core": make_pii_discovery_stub()` entry. Delete the `make_pii_discovery_stub` function. Keep everything else identical.

- [ ] **Step 8: Run the test suite — confirm the expected failures**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest -q
```

Expected: failures in tests that still reference the now-removed pii_discovery from the *implementation* (because we haven't deleted it yet). Specifically:
- Tests that assert `pii_discovery NOT in COARSE_TO_SUB["stage_2_parallel"]` should fail because the implementation still has it.
- Tests we updated (Steps 2-7) should now pass against the implementation that still has pii_discovery — which means our updates were wrong, indicating we asserted the *wrong* direction. Re-read.

Actually: after Steps 2-7 the tests assert the *new* expected shape (no pii_discovery). The implementation still has pii_discovery. So the tests that previously asserted the OLD shape and were updated to the NEW shape will now fail because the implementation still produces the OLD shape.

Expected failures: at least one assertion in test_stages.py around `SUB_STAGES_BY_STAGE["stage_2_parallel"]`. If you see zero failures it means the tests weren't actually checking shape — re-read Steps 2-7 and the matching test bodies.

- [ ] **Step 9: Delete `_run_pii_discovery` from `src/dsar_orchestrator/pipeline.py`**

Delete lines 303-308 (the entire `_run_pii_discovery` function):

```python
# DELETE:
def _run_pii_discovery(cfg: CaseConfig) -> None:
    if not cfg.discovery_enabled:
        return
    pii_discovery = _lazy_import("dsar_pii_discovery.core")
    pii_discovery.discover_entities(cfg.case_path)
    _check_module_work(cfg, "pii_discovery")
```

- [ ] **Step 10: Edit `SUB_STAGES_BY_STAGE` in `src/dsar_orchestrator/pipeline.py` line 57**

Change:
```python
"stage_2_parallel": ("embed", "detect_2_1_to_2_4", "pii_discovery"),
```
to:
```python
"stage_2_parallel": ("embed", "detect_2_1_to_2_4"),
```

- [ ] **Step 11: Edit `_run_stage_2_parallel` in `src/dsar_orchestrator/pipeline.py` lines 561-581**

Change:
```python
def _run_stage_2_parallel(cfg: CaseConfig) -> None:
    """ThreadPoolExecutor fan-out for Stage 2.

    Three branches: embed, detect-2.1-2.4, pii-discovery. Joined with
    FIRST_EXCEPTION semantics — if any branch raises, the others are
    cancelled (best-effort; ThreadPoolExecutor cannot truly cancel
    running tasks but does cancel queued ones) and the exception
    propagates immediately.
    """
    targets = [
        ("embed", _run_embed),
        ("detect_2_1_to_2_4", _run_detect_2_1_to_2_4),
    ]
    if cfg.discovery_enabled:
        targets.append(("pii_discovery", _run_pii_discovery))

    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futures = {name: ex.submit(fn, cfg) for name, fn in targets}
        done, _not_done = wait(futures.values(), return_when=FIRST_EXCEPTION)
        for f in done:
            f.result()  # re-raise if it had an exception
```

to:
```python
def _run_stage_2_parallel(cfg: CaseConfig) -> None:
    """ThreadPoolExecutor fan-out for Stage 2.

    Two branches: embed, detect-2.1-2.4. Joined with FIRST_EXCEPTION
    semantics — if any branch raises, the others are cancelled
    (best-effort) and the exception propagates immediately.
    """
    targets = [
        ("embed", _run_embed),
        ("detect_2_1_to_2_4", _run_detect_2_1_to_2_4),
    ]

    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futures = {name: ex.submit(fn, cfg) for name, fn in targets}
        done, _not_done = wait(futures.values(), return_when=FIRST_EXCEPTION)
        for f in done:
            f.result()  # re-raise if it had an exception
```

Also update the comment at line 42 (`# { embed ∥ detect_2_1_to_2_4 ∥ pii_discovery }`) to `# { embed ∥ detect_2_1_to_2_4 }`.

- [ ] **Step 12: Delete `check_pii_discovery` + entry in `src/dsar_orchestrator/module_agents.py`**

Delete lines 209-225 (the entire `check_pii_discovery` function and its `─ pii_discovery ─` divider comment).

Then remove the CHECKERS entry at line 606:
```python
# DELETE this line from CHECKERS dict:
    "pii_discovery": check_pii_discovery,
```

- [ ] **Step 13: Delete `STAGE_ARTEFACTS["pii_discovery"]` + unused helper in `src/dsar_orchestrator/stages.py`**

Delete the entry around lines 184-189:
```python
# DELETE:
    "pii_discovery": StageArtefact(
        "pii_discovery",
        "stage_2_parallel",
        "working/pii_discovery.jsonl",
        _hash_register_plus_scope,
    ),
```

Then delete `_hash_register_plus_scope` (lines 75-79) — confirm no other STAGE_ARTEFACTS entry uses it:

```bash
grep -n '_hash_register_plus_scope' src/dsar_orchestrator/stages.py
```

Should show only the function definition after the delete. If anything else references it, keep it. Also update the docstring at lines 69-71:
```python
# Change:
"""Upstream for ``embed`` + ``detect_2_1_to_2_4`` + ``pii_discovery``:
the register + raw text per ref."""
# To:
"""Upstream for ``embed`` + ``detect_2_1_to_2_4``: the register + raw text per ref."""
```

- [ ] **Step 14: Update `src/dsar_orchestrator/config.py` — drop env-var resolution**

At line 161, change:
```python
        discovery_enabled=_resolve_bool("DISCOVERY_ENABLED", raw.get("discovery_enabled", True)),
```
to:
```python
        # Deprecated in v0.4.0 (Contract B / #10). Kept as no-op for one release;
        # removal target = v0.5.0. The pii_discovery stage no longer exists.
        discovery_enabled=bool(raw.get("discovery_enabled", True)),
```

Leave the `discovery_enabled: bool = True` field at line 75 in the `CaseConfig` dataclass — that's the no-op carrier.

- [ ] **Step 15: Run the test suite — should now pass**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest -q
```

Expected: all green. If failures remain, they're likely tests still referencing pii_discovery — search and fix:
```bash
grep -rn pii_discovery tests/
```
Anything remaining should be addressed.

- [ ] **Step 16: Run import-linter — confirm 9 contracts still kept**

```bash
../../.venv/bin/lint-imports
```

Expected: `Contracts: 9 kept, 0 broken.`

- [ ] **Step 17: Commit**

```bash
git add -A
git status  # sanity-check the staged set
git commit -m "$(cat <<'EOF'
feat(stage): remove pii_discovery (closes #10)

The conductor's `_run_pii_discovery` lazy-imported a non-existent toolkit
module (`dsar_pii_discovery.core`). The discovery functionality is folded
into `dsar_pii_classifier.core.discover_case()` which the pii_classify
stage already calls. The conductor was doing duplicate work pointed at a
fictional module.

Stage 2 now: { embed ∥ detect_2_1_to_2_4 } (was: + pii_discovery).

BREAKING (pre-1.0 waiver, will be reflected in v0.4.0 bump): pii_discovery
no longer a valid `--only` target. `discovery_enabled` config field kept as
deprecated no-op; removal target v0.5.0.

Closes #10.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Rerank via `tei_rerank_client` (closes #11)

**Files:**
- Create: `src/dsar_orchestrator/adapters/rerank.py`
- Create: `tests/test_adapter_rerank.py`
- Modify: `src/dsar_orchestrator/pipeline.py:338-347` (replace `_lazy_import("dsar_rerank.core")` block)
- Modify: `tests/_toolkit_stubs/stubs.py` (add `make_tei_rerank_client_stub`; remove `make_rerank_core_stub`)

Classic TDD: write the adapter's tests first, watch them fail, implement, watch them pass.

- [ ] **Step 1: Write `tests/test_adapter_rerank.py` with 5 failing tests**

Create the file with this exact content:

```python
"""Tests for adapters/rerank.py.

Mirrors tests/test_adapter_embed.py / tests/test_adapter_verify_spec.py
pattern: inject a fake client to assert the adapter's reading + writing
behaviour without needing a live TEI service.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import rerank
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier


def _make_cfg(case_path: Path, scope: str = "test scope") -> CaseConfig:
    return CaseConfig(
        case_no="rerank-test",
        case_path=case_path,
        case_scope=scope,
        subject_identifier=SubjectIdentifier(primary_name="Test Person"),
        rerank_mode="shadow",
        rerank_threshold=0.5,
        rerank_top_n=20,
        rerank_sample_rate=0.05,
        pii_classify_mode="shadow",
        pii_budget_usd=5.0,
        discovery_enabled=False,
        redact_verify_enabled=True,
        llm_concurrency=5,
    )


class _FakeRerankResult:
    def __init__(self, scores: list[float], error: str | None = None) -> None:
        self.scores = scores
        self.model_alias = "rerank"
        self.resolved_model = "BAAI/bge-reranker-large"
        self.endpoint_url = "http://127.0.0.1:8084"
        self.model_revision = "stub-rev"
        self.latency_s = 0.001
        self.error = error

    def as_audit_fields(self) -> dict[str, str | float]:
        return {
            "model_alias": self.model_alias,
            "resolved_model": self.resolved_model,
            "endpoint_url": self.endpoint_url,
            "model_revision": self.model_revision,
        }


def _seed_cosine_prefilter(case_path: Path, refs_and_scores: list[tuple[str, float]]) -> None:
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ref": ref,
            "cosine_score": score,
            "passes": True,
            "verdict": "in_scope_candidate",
            "threshold": 0.01,
            "upstream_hash": "stub-upstream",
            "schema_version": "1.0",
            "producer_version": "test-stub",
        }
        for ref, score in refs_and_scores
    ]
    (working / "cosine_prefilter.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    for ref, _score in refs_and_scores:
        (working / f"{ref}.txt").write_text(f"text for {ref}", encoding="utf-8")


def test_rerank_happy_path_writes_scope_rerank_jsonl(tmp_path) -> None:
    cfg = _make_cfg(tmp_path)
    _seed_cosine_prefilter(tmp_path, [("ref-1", 0.8), ("ref-2", 0.2)])

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        # Return scores per-doc; high then low
        return _FakeRerankResult(scores=[0.9, 0.1])

    rerank.run_for_case(cfg, reranker=fake_reranker)

    out = tmp_path / "working" / "scope_rerank.jsonl"
    assert out.exists()
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert {r["ref"] for r in rows} == {"ref-1", "ref-2"}
    # Required fields present
    for row in rows:
        assert "rerank_score" in row
        assert "would_drop" in row
        assert "mode" in row
        assert "upstream_hash" in row
        assert "schema_version" in row
        assert "producer_version" in row


def test_rerank_would_drop_uses_threshold(tmp_path) -> None:
    cfg = _make_cfg(tmp_path)
    cfg = cfg.__class__(**{**cfg.__dict__, "rerank_threshold": 0.5})
    _seed_cosine_prefilter(tmp_path, [("hi", 1.0), ("lo", 1.0)])

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        return _FakeRerankResult(scores=[0.9, 0.1])

    rerank.run_for_case(cfg, reranker=fake_reranker)
    rows = sorted(
        (json.loads(line) for line in (tmp_path / "working" / "scope_rerank.jsonl").read_text().splitlines() if line.strip()),
        key=lambda r: r["ref"],
    )
    by_ref = {r["ref"]: r for r in rows}
    assert by_ref["hi"]["would_drop"] is False  # 0.9 >= 0.5
    assert by_ref["lo"]["would_drop"] is True  # 0.1 < 0.5


def test_rerank_empty_cosine_prefilter_writes_empty_output(tmp_path) -> None:
    cfg = _make_cfg(tmp_path)
    (tmp_path / "working").mkdir(parents=True)
    (tmp_path / "working" / "cosine_prefilter.jsonl").write_text("")

    called = {"count": 0}

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        called["count"] += 1
        return _FakeRerankResult(scores=[])

    rerank.run_for_case(cfg, reranker=fake_reranker)
    out = tmp_path / "working" / "scope_rerank.jsonl"
    assert out.exists()
    assert out.read_text() == ""
    # Don't hit the TEI client when there's nothing to rerank
    assert called["count"] == 0


def test_rerank_propagates_client_error(tmp_path) -> None:
    from dsar_orchestrator.exceptions import DSARPipelineError

    cfg = _make_cfg(tmp_path)
    _seed_cosine_prefilter(tmp_path, [("ref-1", 0.5)])

    def fake_reranker(query: str, docs: list[str]) -> _FakeRerankResult:
        return _FakeRerankResult(scores=[], error="connection refused")

    with pytest.raises(DSARPipelineError, match="TEI rerank failed"):
        rerank.run_for_case(cfg, reranker=fake_reranker)


def test_rerank_missing_cosine_prefilter_raises(tmp_path) -> None:
    from dsar_orchestrator.exceptions import DSARPipelineError

    cfg = _make_cfg(tmp_path)
    (tmp_path / "working").mkdir(parents=True)
    # Don't write cosine_prefilter.jsonl at all

    with pytest.raises(DSARPipelineError, match="cosine_prefilter.jsonl"):
        rerank.run_for_case(cfg, reranker=lambda q, d: _FakeRerankResult([]))
```

- [ ] **Step 2: Run the new tests — confirm they fail**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest tests/test_adapter_rerank.py -v
```

Expected: 5 failures with `ModuleNotFoundError: No module named 'dsar_orchestrator.adapters.rerank'` (the adapter doesn't exist yet).

- [ ] **Step 3: Create `src/dsar_orchestrator/adapters/rerank.py`**

Write the file with this exact content:

```python
"""Conductor-owned rerank adapter — replaces the lazy-import to the
non-existent `dsar_rerank.core` (toolkit issue: dsar_rerank module
was never written; rerank lives at `dsar_clients.tei_rerank_client`).

Reads `working/cosine_prefilter.jsonl`, calls TEI's bge-reranker-large
via `dsar_clients.tei_rerank_client.rerank_pairs(query=case_scope,
docs=[texts])`, writes `working/scope_rerank.jsonl` with the cascade's
required `upstream_hash` field. Row shape locked by Contract A
helpers + the existing stub fixture.

**Retirement contract.** When the toolkit ships its own
`dsar_pipeline.rerank.run_for_case(case_path)`, this adapter retires;
`pipeline._run_scope_filter_chain` switches to import that. The output
JSONL shape must match what the toolkit eventually writes so downstream
artefacts + the resume cascade are unaffected.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import sha256_file, sha256_text

PRODUCER_VERSION = "dsar_orchestrator.adapters.rerank 0.4.0"
SCHEMA_VERSION = "1.0"


# ─── injectable HTTP-client protocol ────────────────────────────────


class _RerankResultLike(Protocol):
    """Duck-type for `dsar_clients.tei_rerank_client.RerankResult`."""

    scores: list[float]
    model_alias: str
    resolved_model: str
    endpoint_url: str
    model_revision: str
    latency_s: float
    error: str | None

    def as_audit_fields(self) -> dict[str, str | float]: ...


RerankerFn = Callable[[str, list[str]], _RerankResultLike]


def _default_reranker() -> RerankerFn:
    """Resolve the live `tei_rerank_client.rerank_pairs` callable lazily.

    Importing at call time keeps the conductor installable without
    `dsar-pipeline` (`dsar-toolkit`) on the path; if the operator
    actually runs a real case, the toolkit must be installed.
    """
    try:
        mod = importlib.import_module("dsar_clients.tei_rerank_client")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_clients.tei_rerank_client is not installed. The "
            "conductor's rerank adapter needs it to call TEI :8084. "
            "Install dsar-toolkit (pip install -e ~/projects/"
            "dsar-toolkit/) and retry."
        ) from exc

    def _adapt(query: str, docs: list[str]) -> _RerankResultLike:
        return mod.rerank_pairs(query=query, docs=docs)

    return _adapt


# ─── public entry ──────────────────────────────────────────────────


def run_for_case(cfg: CaseConfig, *, reranker: RerankerFn | None = None) -> None:
    """Rerank every cosine-prefilter row under `cfg.case_path` and write
    `working/scope_rerank.jsonl`.

    `reranker` is injectable for tests; in production the default
    resolves to `dsar_clients.tei_rerank_client.rerank_pairs`.
    """
    cosine_path = cfg.case_path / "working" / "cosine_prefilter.jsonl"
    if not cosine_path.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: cosine_prefilter.jsonl missing at {cosine_path}. "
            f"Run scope_prefilter first: dsar-conductor --case {cfg.case_no} "
            f"--only scope_prefilter"
        )

    cosine_rows = [
        json.loads(line) for line in cosine_path.read_text().splitlines() if line.strip()
    ]

    # Empty input — write empty output (deterministic; cascade has the
    # right anchor file even if nothing came through).
    if not cosine_rows:
        _atomic_write(cfg.case_path / "working" / "scope_rerank.jsonl", "")
        return

    refs = [r["ref"] for r in cosine_rows]
    texts = _load_texts(cfg.case_path, refs)
    upstream_hash = _compute_upstream_hash(cfg)

    if reranker is None:
        reranker = _default_reranker()

    result = reranker(cfg.case_scope, texts)

    if result.error:
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI rerank failed: {result.error}. "
            f"Check that TEI is running at {result.endpoint_url}."
        )
    if len(result.scores) != len(refs):
        raise DSARPipelineError(
            f"case={cfg.case_no}: TEI returned {len(result.scores)} scores "
            f"for {len(refs)} refs (mismatch)."
        )

    audit = result.as_audit_fields()
    out_rows = []
    for ref, score in zip(refs, result.scores, strict=True):
        out_rows.append(
            {
                "ref": ref,
                "rerank_score": score,
                "would_drop": score < cfg.rerank_threshold,
                "mode": cfg.rerank_mode,
                "upstream_hash": upstream_hash,
                "schema_version": SCHEMA_VERSION,
                "producer_version": PRODUCER_VERSION,
                "latency_s": result.latency_s,
                **audit,
            }
        )

    _atomic_write(
        cfg.case_path / "working" / "scope_rerank.jsonl",
        "\n".join(json.dumps(r) for r in out_rows) + "\n",
    )


def _load_texts(case_path: Path, refs: list[str]) -> list[str]:
    """Read working/<ref>.txt per Contract A. Missing text files raise."""
    from dsar_orchestrator.register import text_path_for_ref

    texts: list[str] = []
    for ref in refs:
        text_path = text_path_for_ref(case_path, ref)
        if not text_path.exists():
            raise DSARPipelineError(f"missing text file for ref={ref}: {text_path}")
        texts.append(text_path.read_text(encoding="utf-8", errors="replace"))
    return texts


def _compute_upstream_hash(cfg: CaseConfig) -> str:
    """Mirror of stages._hash_cosine_plus_scope so this adapter records the
    cascade-correct upstream hash without needing to import stages."""
    cosine_path = cfg.case_path / "working" / "cosine_prefilter.jsonl"
    cosine = sha256_file(cosine_path) if cosine_path.exists() else ""
    return sha256_text(
        f"{cosine}\x1f{cfg.case_scope}\x1f"
        f"thr={cfg.rerank_threshold}\x1f"
        f"topN={cfg.rerank_top_n}\x1f"
        f"mode={cfg.rerank_mode}"
    )


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
```

- [ ] **Step 4: Rewire `_run_scope_filter_chain` in `src/dsar_orchestrator/pipeline.py`**

Find lines 320-347 (the function definition). Change the rerank branch (lines 334-347) from:

```python
    # Rerank still goes through the toolkit-stub-or-lazy-import path.
    # Awaiting the resolution of toolkit issue #4 (is `dsar_rerank` a
    # standalone module or does it live inside `dsar_pii_classifier`?)
    # before writing a conductor-side rerank adapter.
    if cfg.rerank_mode != "off":
        rerank_core = _lazy_import("dsar_rerank.core")
        rerank_core.rerank_case(
            cfg.case_path,
            mode=cfg.rerank_mode,
            threshold=cfg.rerank_threshold,
            top_n=cfg.rerank_top_n,
            sample_rate=cfg.rerank_sample_rate,
        )
        _check_module_work(cfg, "rerank")
```

to:

```python
    # ADAPTER for rerank (retires when toolkit ships
    # `dsar_pipeline.rerank.run_for_case(case_path)` — see Contract B).
    # The conductor reads cosine_prefilter.jsonl, calls TEI's
    # bge-reranker-large via dsar_clients.tei_rerank_client.rerank_pairs,
    # writes scope_rerank.jsonl with the cascade's required upstream_hash
    # field.
    if cfg.rerank_mode != "off":
        from dsar_orchestrator.adapters import rerank as rerank_adapter

        rerank_adapter.run_for_case(cfg)
        _check_module_work(cfg, "rerank")
```

- [ ] **Step 5: Update `tests/_toolkit_stubs/stubs.py` — swap rerank stub**

Find `make_rerank_core_stub` and delete the function entirely. In `all_stubs()`, remove the `"dsar_rerank.core": make_rerank_core_stub()` entry (or whatever the registration looks like — confirm by grep).

Then add the new TEI-rerank client stub. Place it near `make_tei_embed_client_stub`:

```python
def make_tei_rerank_client_stub() -> types.ModuleType:
    """Stub for `dsar_clients.tei_rerank_client` — the conductor's rerank
    adapter (Contract B / issue #11) calls this directly. Returns
    deterministic per-doc scores so resume-cascade tests stay stable."""
    mod = types.ModuleType("dsar_clients.tei_rerank_client")

    class RerankResult:
        def __init__(self, scores: list[float], *, error: str | None = None) -> None:
            self.scores = scores
            self.model_alias = "rerank"
            self.resolved_model = "BAAI/bge-reranker-large"
            self.endpoint_url = "http://127.0.0.1:8084"
            self.model_revision = "stub-rev"
            self.latency_s = 0.001
            self.error = error

        def as_audit_fields(self) -> dict[str, str | float]:
            return {
                "model_alias": self.model_alias,
                "resolved_model": self.resolved_model,
                "endpoint_url": self.endpoint_url,
                "model_revision": self.model_revision,
            }

    def rerank_pairs(
        *,
        query: str,
        docs: list[str],
        timeout_s: int = 30,
        retries: int = 2,
        backoff_s: float = 1.0,
        raw_scores: bool = False,
        tei_url: str = "http://127.0.0.1:8084",
    ) -> RerankResult:
        # Deterministic per-doc score: first byte of UTF-8 as a value
        # in [0, 1). Avoids randomness in tests; doesn't pretend to be
        # meaningful rerank scores.
        scores = [(d.encode("utf-8")[0] if d else 0) / 255.0 for d in docs]
        return RerankResult(scores=scores)

    def health(tei_url: str = "http://127.0.0.1:8084") -> bool:
        return True

    mod.RerankResult = RerankResult
    mod.rerank_pairs = rerank_pairs
    mod.health = health
    return mod
```

In `all_stubs()`, add `"dsar_clients.tei_rerank_client": make_tei_rerank_client_stub()` to the returned dict.

If the existing integration tests have a runner-fake for the old rerank-core stage, update them to instead exercise the new adapter via its injected `reranker`. Run the tests after this step to see what breaks.

- [ ] **Step 6: Run the test suite — adapter tests should pass + nothing else regresses**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest -q
```

Expected: all green. New tests in `test_adapter_rerank.py` (5) now pass. Integration tests still pass via the new stub.

If `test_resume_cascade.py` or `test_synthetic_case_100.py` fail because they exercised the old `dsar_rerank.core` path, update them to use the new TEI-rerank-client stub registration. The runner-fake patterns in `test_synthetic_case_100.py` may need a parallel `tei_rerank_client` monkeypatch (mirror the `_default_runner` pattern that already exists for ingest/detect/scope_classify).

- [ ] **Step 7: Run import-linter**

```bash
../../.venv/bin/lint-imports
```

Expected: `Contracts: 9 kept, 0 broken.`

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(adapter): rerank via tei_rerank_client (closes #11)

The conductor's `_run_scope_filter_chain` was lazy-importing the
non-existent `dsar_rerank.core`. Toolkit ships rerank as
`dsar_clients.tei_rerank_client.rerank_pairs(query, docs, ...)` →
`RerankResult` (parallel to embed's existing tei-client adapter).

New `src/dsar_orchestrator/adapters/rerank.py` mirrors the embed adapter
pattern: injectable client protocol, reads `working/cosine_prefilter.jsonl`,
calls TEI :8084 via the toolkit client, writes `working/scope_rerank.jsonl`
with the cascade's required upstream_hash. Output JSONL shape matches
what the toolkit will eventually write (when `dsar_pipeline.rerank` lands).

Retirement contract: adapter retires when toolkit ships
`dsar_pipeline.rerank.run_for_case(case_path)`.

Closes #11.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Smart-empty `check_pii_classify` (closes #12 interim)

**Files:**
- Create: `tests/test_module_agent_pii_classify.py`
- Modify: `src/dsar_orchestrator/module_agents.py:387-413` (add scope-cross-reference branch)
- Modify: `src/dsar_orchestrator/adapters/pii_classify.py` (bump `PRODUCER_VERSION` to `0.4.0`)

Classic TDD: 3 tests for the smart-empty cases, then implement.

- [ ] **Step 1: Write `tests/test_module_agent_pii_classify.py` with 3 failing tests**

Create the file:

```python
"""Tests for check_pii_classify smart-empty tolerance (Contract B / #12).

When pii_collection.jsonl is missing/empty:
- If scope_classify produced 0 in-scope ("present") verdicts → info (ok).
- If scope_classify produced ≥1 in-scope verdicts → critical (halts).
When populated: existing strict checks apply.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.module_agents import check_pii_classify


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no="pii-test",
        case_path=case_path,
        case_scope="test scope",
        subject_identifier=SubjectIdentifier(primary_name="Test"),
        rerank_mode="shadow",
        rerank_threshold=0.01,
        rerank_top_n=20,
        rerank_sample_rate=0.05,
        pii_classify_mode="shadow",
        pii_budget_usd=5.0,
        discovery_enabled=False,
        redact_verify_enabled=True,
        llm_concurrency=5,
    )


def _seed_scope_verdicts(case_path: Path, verdicts: list[str]) -> None:
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ref": f"r-{i}", "scope_verdict": "present", "verdict": v}
        for i, v in enumerate(verdicts)
    ]
    (working / "scope_verdicts.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")
    )


def test_empty_pii_collection_no_in_scope_is_info(tmp_path) -> None:
    """When scope had 0 'present' verdicts, empty pii_collection is OK."""
    cfg = _make_cfg(tmp_path)
    _seed_scope_verdicts(tmp_path, ["ambiguous", "not_present", "ambiguous"])
    # pii_collection.jsonl absent

    result = check_pii_classify(cfg)
    assert result.ok is True
    assert result.severity == "info"
    assert any("nothing to classify" in f.lower() or "no in-scope" in f.lower()
               for f in result.findings)


def test_empty_pii_collection_with_in_scope_is_critical(tmp_path) -> None:
    """When scope had ≥1 'present' verdicts, empty pii_collection is wrong."""
    cfg = _make_cfg(tmp_path)
    _seed_scope_verdicts(tmp_path, ["present", "not_present", "present"])
    # pii_collection.jsonl absent

    result = check_pii_classify(cfg)
    assert result.ok is False
    assert result.severity == "critical"


def test_populated_pii_collection_uses_existing_strict_checks(tmp_path) -> None:
    """When pii_collection has rows, existing field-validity checks run."""
    cfg = _make_cfg(tmp_path)
    _seed_scope_verdicts(tmp_path, ["present"])
    working = tmp_path / "working"
    # Row missing in_scope_recheck → triggers existing critical
    (working / "pii_collection.jsonl").write_text(
        json.dumps({"ref": "r-0", "entities": [], "upstream_hash": "h"}) + "\n"
    )

    result = check_pii_classify(cfg)
    # Existing behaviour: critical because required field is missing
    assert result.ok is False
    assert result.severity == "critical"
```

- [ ] **Step 2: Run the new tests — confirm they fail**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest tests/test_module_agent_pii_classify.py -v
```

Expected: `test_empty_pii_collection_no_in_scope_is_info` FAILS (current behaviour returns critical, not info). The other two should already pass. If all fail, re-check the test fixture wiring.

- [ ] **Step 3: Modify `check_pii_classify` in `src/dsar_orchestrator/module_agents.py`**

Find the function at line 387. At the top of the module (near other constants), add:

```python
# Contract B / issue #12: in-scope-positive verdict set from
# dsar_pipeline.scope_check_stage. "present" is the only verdict that
# the toolkit's own pii_classifier actually processes (verified via
# cross-test 2026-05-24 — "ambiguous" and "not_present" both fall out).
IN_SCOPE_POSITIVE_VERDICTS: frozenset[str] = frozenset({"present"})
```

Then replace the function body at lines 387-413 with:

```python
def check_pii_classify(cfg: CaseConfig) -> ModuleCheckResult:
    if cfg.pii_classify_mode == "off":
        return _ok(["PII_CLASSIFY_MODE=off; skipping"])
    path = cfg.case_path / "working" / "pii_collection.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        # Contract B / issue #12 (interim): empty pii_collection is OK
        # when scope_classify produced 0 in-scope-positive verdicts.
        # Critical only when scope had in-scope docs that should have
        # produced findings.
        scope_rows = _load_jsonl(cfg.case_path / "working" / "scope_verdicts.jsonl")
        in_scope_count = sum(
            1 for r in scope_rows if r.get("verdict") in IN_SCOPE_POSITIVE_VERDICTS
        )
        if in_scope_count == 0:
            return _ok([
                "pii_collection.jsonl empty; no in-scope docs from "
                "scope_classify so nothing to classify"
            ])
        return _critical(
            [
                f"pii_collection.jsonl missing or empty at {path}; "
                f"scope_verdicts shows {in_scope_count} in-scope docs",
            ],
            _rerun_hint("pii_classify", cfg.case_no),
        )
    missing = _all_have(rows, ("ref", "in_scope_recheck", "entities", "upstream_hash"))
    if missing:
        return _critical(missing, _rerun_hint("pii_classify", cfg.case_no))
    # Recheck verdict must be in the allowed set
    bad_verdicts: list[str] = []
    for row in rows:
        v = row.get("in_scope_recheck")
        if v not in VALID_RECHECK_VERDICTS:
            bad_verdicts.append(f"ref={row.get('ref')} in_scope_recheck={v!r}")
        if len(bad_verdicts) >= 5:
            break
    if bad_verdicts:
        return _critical(
            bad_verdicts + [f"Allowed: {sorted(VALID_RECHECK_VERDICTS)}"],
            _rerun_hint("pii_classify", cfg.case_no),
        )
    return _ok([f"pii_collection.jsonl: {len(rows)} refs"])
```

- [ ] **Step 4: Bump `PRODUCER_VERSION` in `src/dsar_orchestrator/adapters/pii_classify.py`**

Find the `PRODUCER_VERSION = "dsar_orchestrator.adapters.pii_classify 0.3.0"` line and change `0.3.0` to `0.4.0`.

Even though the adapter body didn't change, its public contract (downstream agent semantics) did, so PRODUCER_VERSION bumps per VERSIONING.md §3.

- [ ] **Step 5: Run the test suite — all green**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest -q
```

Expected: all green. The 3 new tests pass, no regressions.

- [ ] **Step 6: Run import-linter**

```bash
../../.venv/bin/lint-imports
```

Expected: `Contracts: 9 kept, 0 broken.`

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat(agent): smart-empty pii_classify check (closes #12 interim)

The toolkit's pii_classifier_stage writes per-stage findings to
~/.dsar-audit/<case>/pii_findings_stage{1,2,3}.jsonl but nothing
aggregates them into <case>/working/pii_collection.jsonl. The conductor's
check_pii_classify agent was halting with `critical` when the aggregated
file was missing/empty, even on cases where scope_classify legitimately
produced zero in-scope docs.

Interim tolerance: when scope_verdicts.jsonl shows zero "present"
verdicts (the only in-scope-positive value the toolkit processes),
empty pii_collection is `info`. When ≥1 docs are in-scope and PII
findings are still empty, behaviour stays `critical` — that's the
genuine concerning case.

Filed harkers/dsar-toolkit#<TOOLKIT_ISSUE> for the long-term fix
(toolkit ships pii_collection.jsonl aggregation directly). When that
lands, the conductor's follow-up issue retires this interim branch.

Closes #12 (interim).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(Replace `<TOOLKIT_ISSUE>` with the actual number returned in Task 0 Step 5.)

---

## Task 4: Codify Contract B + bump 0.3.0 → 0.4.0

**Files:**
- Create: `tests/test_contract_b_no_fictional_modules.py`
- Modify: `VERSIONING.md` (append new §4)
- Modify: `src/dsar_orchestrator/__init__.py` (add Contract B pointer in docstring + bump `__version__`)
- Modify: `pyproject.toml` (`version = "0.4.0"`)
- Modify: `CHANGELOG.md` (`[Unreleased]` → `[0.4.0] - 2026-05-24` block)
- Modify: `tests/integration/test_real_toolkit_smoke.py` (add `EXPECTED_TOOLKIT_MODULES`)

- [ ] **Step 1: Write `tests/test_contract_b_no_fictional_modules.py`**

Create the file:

```python
"""Contract B enforcement — AST-walk conductor sources for every
`dsar_*` lazy-import string, assert each resolves against the installed
toolkit. Catches the next #1/#10/#11-class drift the moment it appears.

Gated behind `@pytest.mark.needs_toolkit` because verification requires
the real toolkit installed. CI default doesn't select this marker.

See VERSIONING.md §4 for the Contract B principle this test enforces.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

CONDUCTOR_SRC = Path(__file__).parent.parent / "src" / "dsar_orchestrator"


def _is_lazy_import_call(node: ast.Call) -> bool:
    """True if node is `_lazy_import("dsar_*")` or `importlib.import_module("dsar_*")`."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "_lazy_import":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "import_module":
        return True
    return False


def _collect_dsar_lazy_imports() -> set[str]:
    """Walk every src/dsar_orchestrator/*.py; collect every literal-string
    first-arg to _lazy_import / importlib.import_module that starts with
    `dsar_`."""
    targets: set[str] = set()
    for py_file in CONDUCTOR_SRC.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_lazy_import_call(node):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("dsar_"):
                    targets.add(arg.value)
    return targets


@pytest.mark.needs_toolkit
def test_contract_b_no_fictional_toolkit_modules() -> None:
    """Contract B: every `dsar_*` module the conductor lazy-imports must
    resolve against the installed toolkit. See VERSIONING.md §4."""
    targets = _collect_dsar_lazy_imports()
    assert targets, (
        "AST walker found zero `dsar_*` lazy-imports — either the conductor "
        "has stopped using lazy-import or the AST walker is broken."
    )
    missing = sorted(t for t in targets if importlib.util.find_spec(t) is None)
    assert not missing, (
        f"Contract B violated: conductor lazy-imports modules that don't "
        f"exist in the installed toolkit: {missing}. "
        f"Either fix the conductor adapter or install the missing toolkit "
        f"module. See VERSIONING.md §4 (Toolkit-coupling contract)."
    )


def test_contract_b_collector_is_not_silently_broken() -> None:
    """Sanity check on the AST walker itself — runs without `needs_toolkit`
    so default CI catches walker regressions even when toolkit absent."""
    targets = _collect_dsar_lazy_imports()
    # At minimum, the adapters/embed.py + adapters/rerank.py lazy-imports
    # should be found. Don't assert exact set (drifts with each adapter
    # added) — just that the walker finds something.
    assert any(t.startswith("dsar_clients") for t in targets), (
        f"AST walker should find dsar_clients.* lazy-imports; got: {targets}"
    )
```

- [ ] **Step 2: Add `EXPECTED_TOOLKIT_MODULES` to `tests/integration/test_real_toolkit_smoke.py`**

Open the file and after the existing imports / docstring, add:

```python
# Contract B / VERSIONING.md §4: every toolkit module the conductor
# lazy-imports lives here. The AST-walker test
# (tests/test_contract_b_no_fictional_modules.py) validates the *actual*
# call sites resolve; this list documents the *intended* set so new
# additions are visible in one place.
EXPECTED_TOOLKIT_MODULES: tuple[str, ...] = (
    "dsar_clients.tei_embed_client",
    "dsar_clients.tei_rerank_client",  # NEW in Contract B / #11
    "dsar_pii_classifier.core",
    "dsar_pipeline.ingest",
    "dsar_pipeline.detect",
    "dsar_pipeline.people_register",
    "dsar_pipeline.post_bake_verify",
    "dsar_pipeline.verify_spec",
)
```

(Verify the exact existing set by running `grep -rn 'importlib.import_module\|_lazy_import' src/` in the worktree and reconciling. Update the list if anything's missing.)

- [ ] **Step 3: Append §4 to `VERSIONING.md`**

After the existing §3 block, append:

```markdown

## 4. Toolkit-coupling contract (Contract B)

The conductor sits above the operator-installable `dsar-toolkit`. Three invariants:

1. **Every `_lazy_import("dsar_*.module")` target must exist in the toolkit at the version pinned in `pyproject.toml`.** Drift here = silent failure that only surfaces against the real toolkit (see issues #1, #10, #11).

2. **Every conductor adapter writes the artefact its downstream consumers + module-agent expect.** Path, shape, and required fields are part of the adapter's public contract — changes bump `PRODUCER_VERSION` and `SCHEMA_VERSION` per §3 + §2.

3. **New adapters added to the conductor must be exercised by `tests/integration/test_real_toolkit_smoke.py`** before merge. The smoke test is the executable form of this contract.

**Enforcement:** `tests/test_contract_b_no_fictional_modules.py` AST-walks every conductor source file, collects every literal-string `_lazy_import` / `importlib.import_module` target in the `dsar_*` namespace, and (under `@pytest.mark.needs_toolkit`) asserts `importlib.util.find_spec(...)` returns non-None for each. The companion `EXPECTED_TOOLKIT_MODULES` list at the top of `test_real_toolkit_smoke.py` documents the intended set.

**Relationship to Contract A:** Contract A (issue #8) fixed the conductor's shape assumption about `register.json`. Contract B generalises: any conductor assumption about the toolkit (module names, output paths, aggregation, shape) is a coupling that must be verified executably, not just documented.
```

- [ ] **Step 4: Add Contract B pointer to `src/dsar_orchestrator/__init__.py`**

Open the file. If the existing module docstring is brief, append a paragraph. If there's no docstring, add one. Example final shape:

```python
"""dsar_orchestrator — the conductor above harkers/dsar-toolkit.

Sits above the toolkit's modular stages, adding:
- Resume cascade via upstream_hash chain
- Per-module-agent validation
- Log analyser with block flag
- Single CLI surface: `dsar-conductor`

Contracts that govern this package's coupling to the toolkit are
documented in VERSIONING.md (§2 schema, §3 producer, §4 toolkit-coupling).
Contract A (#8) and Contract B (#10-#12) are the established precedents.
"""

from __future__ import annotations

__version__ = "0.4.0"
__all__ = ["__version__"]
```

If `__all__` already lists more, preserve those entries.

- [ ] **Step 5: Bump `pyproject.toml`**

Find the `[project]` table. Change `version = "0.3.0"` to `version = "0.4.0"`.

- [ ] **Step 6: Update `CHANGELOG.md`**

Move the current `## [Unreleased]` content (none of the prior v5.5/v5.0 entries need touching — they're under their own version blocks) into a new `## [0.4.0] - 2026-05-24` section, and create a fresh empty `## [Unreleased]` above it.

Add the 0.4.0 content:

```markdown
## [Unreleased]

## [0.4.0] - 2026-05-24

### Changed — Contract B (issues #10/#11/#12)

- **BREAKING (pre-1.0 waiver):** Removed `pii_discovery` stage from `stage_2_parallel` (closes #10). The toolkit doesn't ship `dsar_pii_discovery.core`; the discovery functionality is folded into `dsar_pii_classifier.core.discover_case()` which the pii_classify stage already calls. `pii_discovery` no longer a valid `--only` target. `discovery_enabled` config field kept as deprecated no-op for one release; removal target = v0.5.0.
- Rewired `_run_scope_filter_chain` rerank branch to use new `adapters/rerank.py` (closes #11). The conductor was lazy-importing the non-existent `dsar_rerank.core`. New adapter calls `dsar_clients.tei_rerank_client.rerank_pairs(query=case_scope, docs=[texts])` directly — mirror of the embed adapter's existing tei-client rewire.
- `check_pii_classify` now tolerates empty `pii_collection.jsonl` when scope_classify produced zero `"present"` verdicts (closes #12, interim). Halts critical only when ≥1 docs are in-scope and PII findings missing. Filed harkers/dsar-toolkit#<TOOLKIT_ISSUE> for the long-term aggregation fix; conductor follow-up issue tracks the pivot when toolkit lands aggregation.

### Added — Contract B principle (durable)

- `VERSIONING.md §4` *Toolkit-coupling contract*: every conductor lazy-import target must exist in the toolkit; every adapter writes what consumers + agents expect; new adapters must be exercised by the real-toolkit smoke test.
- `tests/test_contract_b_no_fictional_modules.py` — AST-walk enforcement under `@pytest.mark.needs_toolkit`.
- `tests/integration/test_real_toolkit_smoke.py` now exports `EXPECTED_TOOLKIT_MODULES` documenting the intended toolkit-module set.
- Contract B pointer added to `src/dsar_orchestrator/__init__.py` module docstring.

### Added — new adapter

- `src/dsar_orchestrator/adapters/rerank.py` (Task 2). Mirror of the embed adapter pattern: injectable client protocol, `working/cosine_prefilter.jsonl` → `working/scope_rerank.jsonl` with cascade-correct upstream_hash. Retires when toolkit ships `dsar_pipeline.rerank.run_for_case`.

### Tests

- 3 new tests in `tests/test_module_agent_pii_classify.py` covering smart-empty tolerance.
- 5 new tests in `tests/test_adapter_rerank.py` covering happy path, threshold edge, empty input, client error, missing prerequisite.
- 2 new tests in `tests/test_contract_b_no_fictional_modules.py` (AST walker + the gated `needs_toolkit` enforcement).
- Removed: pii_discovery-specific assertions in `test_stages.py`, `test_resume_cascade.py`, `test_module_agents.py`, `test_synthetic_case_100.py`, `test_full_pipeline_with_stubs.py`.

### Coordination

- Toolkit-side issue filed: harkers/dsar-toolkit#<TOOLKIT_ISSUE> (`pii_classifier_stage: write working/pii_collection.jsonl aggregating per-stage findings`). Conductor v0.4.0 ships interim smart-empty tolerance; conductor's `adapters/pii_classify.py` pivots to consume the toolkit's aggregated file in a follow-up release (tracked as dsar-orchestrator#<CONDUCTOR_FOLLOWUP>).
```

(Replace both `<TOOLKIT_ISSUE>` and `<CONDUCTOR_FOLLOWUP>` with the actual issue numbers from Task 0.)

Also update the comparison links at the bottom of the file:

```markdown
[Unreleased]: https://github.com/harkers/dsar-orchestrator/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/harkers/dsar-orchestrator/compare/v0.3.0...v0.4.0
```

- [ ] **Step 7: Run the full test suite**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
../../.venv/bin/pytest -q
```

Expected: all green. Counts: ~297 tests (depends on what was added/removed). The `test_contract_b_collector_is_not_silently_broken` test runs without the marker.

- [ ] **Step 8: Run with `needs_toolkit` marker — Contract B AST test exercises**

```bash
../../.venv/bin/pytest -q -m needs_toolkit
```

Expected: passes if toolkit is installed (TEI doesn't need to be running for the AST test — only `find_spec`). The existing `test_real_toolkit_smoke.py` tests will self-skip if TEI :8085/:8084 unreachable.

If `test_contract_b_no_fictional_toolkit_modules` fails, it means a conductor lazy-import points at a still-fictional toolkit module — investigate and either fix the conductor or file a new issue.

- [ ] **Step 9: Run import-linter**

```bash
../../.venv/bin/lint-imports
```

Expected: `Contracts: 9 kept, 0 broken.`

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs+test: codify Contract B + bump 0.3.0 → 0.4.0

Contract B is the durable principle behind the three fixes in this PR:
the conductor's adapter set must track what the toolkit ships — module
names, output paths, aggregation responsibilities. Codified in:

- VERSIONING.md §4 *Toolkit-coupling contract* (three invariants).
- tests/test_contract_b_no_fictional_modules.py — AST-walks every
  conductor `_lazy_import` / `importlib.import_module` literal, asserts
  each `dsar_*` target resolves against the installed toolkit. Gated
  by `@pytest.mark.needs_toolkit`; CI default doesn't select it.
- EXPECTED_TOOLKIT_MODULES list at top of test_real_toolkit_smoke.py
  documents the intended set in one place.
- Contract B pointer in `src/dsar_orchestrator/__init__.py` docstring
  (visible in IDE hovers).

Version bump: 0.3.0 → 0.4.0 (pre-1.0 MINOR per VERSIONING.md §1 waiver:
#10 is breaking, #11/#12 additive, waiver collapses to MINOR).

Closes dsar-orchestrator#10, #11, #12.
Coordinates with: harkers/dsar-toolkit#<TOOLKIT_ISSUE>.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

(Replace `<TOOLKIT_ISSUE>` with the actual number from Task 0 Step 5.)

---

## Task 5: Cross-test acceptance + open PR

**Files:** none. This is the acceptance gate that mirrors the cross-test that surfaced the bugs.

- [ ] **Step 1: Re-pin venv to the worktree (in case prior tasks shifted it)**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
VIRTUAL_ENV=../../.venv uv pip install -e .
```

Expected: `Installed 1 package: dsar-orchestrator==0.4.0 (from file:///.../contract-b)`.

- [ ] **Step 2: Generate the synthetic case**

```bash
rm -rf /tmp/contract-b-accept
mkdir /tmp/contract-b-accept
../../.venv/bin/dsar-synthesize-case --case-no 900200 --out-dir /tmp/contract-b-accept --doc-count 5
```

Expected: `Case 900200 generated at /tmp/contract-b-accept/900200`.

- [ ] **Step 3: Run conductor end-to-end with no env-flag workarounds**

```bash
../../.venv/bin/dsar-conductor --case 900200 --case-root /tmp/contract-b-accept/900200
```

Expected: all 10 stages complete (`ingest`, `stage_2_parallel`, `stage_3_parallel`, `scope_classify`, `pii_classify`, `redact`, `verify_spec`, `bake`, `verify_pdf`, `export`). Final status shows export wrote 5 PDFs to `/tmp/contract-b-accept/900200/output/`.

The pii_classify stage should produce an `info` row in `~/.dsar-audit/900200/module_checks.jsonl` (verifiable in Step 4); not a critical halt.

If the run halts:
- On stage_2_parallel → did you actually delete `_run_pii_discovery`? (Task 1)
- On stage_3_parallel → does `_run_scope_filter_chain` now import the adapter? (Task 2)
- On pii_classify → did the smart-empty branch land? (Task 3)

Re-run after fix; resume from the failed stage with `--from <stage>`.

- [ ] **Step 4: Verify pii_classify info-row**

```bash
grep '"sub_stage": "pii_classify"' ~/.dsar-audit/900200/module_checks.jsonl
```

Expected: one or more rows with `"severity": "info"` and a message about "no in-scope docs" / "nothing to classify".

- [ ] **Step 5: Verify exports written**

```bash
ls /tmp/contract-b-accept/900200/output/
```

Expected: 5 `.pdf` files plus `manifest.json`.

- [ ] **Step 6: Push the branch**

```bash
cd /Users/stu/projects/dsar-orchestrator/.worktrees/contract-b
git push -u origin feat/contract-b
```

- [ ] **Step 7: Open the PR**

```bash
gh pr create --repo harkers/dsar-orchestrator --base main --title "Contract B: drop pii_discovery, rewire rerank, smart-empty pii_classify (v0.4.0)" --body "$(cat <<'EOF'
## Summary

Closes #10, #11, #12. Implements Contract B per design at
`docs/superpowers/specs/2026-05-24-contract-b-design-v1.md`.

Three minimal source changes + a durable principle codified in
VERSIONING.md §4 and enforced by a new AST-walk test.

## Commits (4)

| Commit | What |
|---|---|
| `feat(stage)` | Remove `pii_discovery` stage entirely (toolkit folded it into `dsar_pii_classifier.core.discover_case()`; conductor was duplicate work pointed at a fictional `dsar_pii_discovery.core`). Closes #10. |
| `feat(adapter)` | New `adapters/rerank.py` mirroring the embed adapter — wraps `dsar_clients.tei_rerank_client.rerank_pairs`. Replaces the lazy-import to the non-existent `dsar_rerank.core`. Closes #11. |
| `feat(agent)` | `check_pii_classify` tolerates empty `pii_collection.jsonl` when scope_classify shows zero `"present"` verdicts; critical only when ≥1 in-scope docs found nothing. Closes #12 (interim). |
| `docs+test` | VERSIONING.md §4 (Contract B principle) + new AST-walk test + 0.3.0 → 0.4.0 bump + CHANGELOG. |

## Why this wasn't caught before

Same class as #1 + #8 — hermetic tests synthesised the toolkit's
behaviour through stubs that didn't match toolkit reality. Surfaced
2026-05-24 when the conductor was first run end-to-end against the
real toolkit. The new AST-walk test
(`tests/test_contract_b_no_fictional_modules.py`) under
`@pytest.mark.needs_toolkit` catches the next instance of this class.

## Test plan

- [x] `.venv/bin/pytest -q` → green (counts: ~297 tests, see CHANGELOG)
- [x] `lint-imports` → 9 contracts kept, 0 broken
- [x] `-m needs_toolkit` → Contract B AST test passes against installed toolkit
- [x] End-to-end acceptance: `dsar-conductor --case 900200 --case-root /tmp/contract-b-accept/900200` completes all 10 stages, exports 5 PDFs, no env-flag workarounds
- [x] `pyproject.toml` + `__init__.py` both at `0.4.0`
- [x] CHANGELOG `[0.4.0]` block populated
- [x] Toolkit-side companion issue filed: harkers/dsar-toolkit#<TOOLKIT_ISSUE>
- [x] Conductor follow-up issue filed: dsar-orchestrator#<CONDUCTOR_FOLLOWUP>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: prints the PR URL. Record it.

- [ ] **Step 8: When PR is mergeable (CLEAN), merge with rebase, tag v0.4.0, push tag**

```bash
gh pr view <PR_NUM> --repo harkers/dsar-orchestrator --json mergeable,mergeStateStatus
# When mergeable:CLEAN, mergeStateStatus:CLEAN
gh pr merge <PR_NUM> --repo harkers/dsar-orchestrator --rebase
cd /Users/stu/projects/dsar-orchestrator
git fetch origin main
git tag -a v0.4.0 origin/main -m "v0.4.0 - Contract B (drop pii_discovery, rewire rerank, smart-empty pii_classify)"
git push origin v0.4.0
```

Expected: tag pushed.

- [ ] **Step 9: Post-merge sanity check on main (V5.0 lesson)**

```bash
cd /Users/stu/projects/dsar-orchestrator
git checkout main
git pull --ff-only origin main
.venv/bin/pytest -q
```

Expected: all green on main. If anything is red, the rebase auto-merged something silently broken — fix forward immediately (lesson from v5.0 → v5.5 sequence where a bake-fake site was auto-merged into a broken state).

- [ ] **Step 10: Re-run cross-test against main**

```bash
rm -rf /tmp/contract-b-postmerge
mkdir /tmp/contract-b-postmerge
.venv/bin/dsar-synthesize-case --case-no 900300 --out-dir /tmp/contract-b-postmerge --doc-count 5
.venv/bin/dsar-conductor --case 900300 --case-root /tmp/contract-b-postmerge/900300
```

Expected: all 10 stages complete on main. Confirms the merged result behaves identically to the pre-merge cross-test.

- [ ] **Step 11: Clean up worktree**

```bash
cd /Users/stu/projects/dsar-orchestrator
git worktree remove .worktrees/contract-b
git branch -D feat/contract-b
```

Contract B delivered.
