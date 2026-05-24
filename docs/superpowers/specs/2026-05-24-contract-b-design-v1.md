# Contract B — conductor↔toolkit coupling contract + 3 fixes

**Status:** approved 2026-05-24, ready for implementation plan.
**Closes:** dsar-orchestrator#10, #11, #12.
**Files toolkit-side issue:** harkers/dsar-toolkit (pii_classifier_stage aggregation).
**Target version:** `0.3.0 → 0.4.0` (pre-1.0 MINOR per VERSIONING.md §1).

## Context

Cross-test on 2026-05-24 (real `dsar-conductor` against synthetic case `900100` + real `dsar-toolkit` at HEAD) surfaced three conductor↔toolkit drift bugs that hermetic tests can't catch — the same failure class as `#1` (fictional `dsar_redact_verify`) and `#8` (register dict-vs-list). Contract A (issue #8) fixed one shape; Contract B fixes the remaining adapter-set drift *and* installs a structural defence so the next instance is caught at PR time, not at first real run.

## 1. The durable principle (Contract B)

Added as a new **§4 *Toolkit-coupling contract*** in `VERSIONING.md`:

> The conductor sits above the operator-installable `dsar-toolkit`. Three invariants:
>
> 1. **Every `_lazy_import("dsar_*.module")` target must exist in the toolkit at the version pinned in `pyproject.toml`.** Drift here = silent failure that only surfaces against the real toolkit (see #1, #10, #11).
> 2. **Every conductor adapter writes the artefact its downstream consumers + module-agent expect.** Path, shape, and required fields are part of the adapter's public contract — changes bump `PRODUCER_VERSION` and `SCHEMA_VERSION` per §3 + §2.
> 3. **New adapters added to the conductor must be exercised by `tests/integration/test_real_toolkit_smoke.py`** before merge. The smoke test is the executable form of this contract.

A pointer to §4 lives in `src/dsar_orchestrator/__init__.py`'s module docstring so contributors hit it on IDE hover. Contract A and Contract B both live in `VERSIONING.md` — coupled in spirit (both "conductor matches toolkit reality"), kept in one file for the small repo. Split to `docs/contracts.md` only if a Contract C/D arrives.

## 2. The codified check

New test `tests/test_contract_b_no_fictional_modules.py`:

- Walks every file under `src/dsar_orchestrator/` with `ast`.
- Collects every literal-string argument to `importlib.import_module(...)` and `_lazy_import(...)`.
- Filters for `dsar_*` namespace.
- Under `@pytest.mark.needs_toolkit`: asserts `importlib.util.find_spec(name) is not None` for each.
- Without the marker: skips (default CI stays green).

A new `EXPECTED_TOOLKIT_MODULES` list at the top of `tests/integration/test_real_toolkit_smoke.py` makes the contract visible in one place. The AST walker validates the *actual* call sites; the list documents the *intended* set. They should match — divergence is the signal to update one or the other.

## 3. The three fixes

### Issue #10 — Remove `pii_discovery` stage

Toolkit doesn't ship `dsar_pii_discovery.core`; the functionality is folded into `dsar_pii_classifier.core.discover_case()` which the pii_classify stage already calls. The conductor's `_run_pii_discovery` was duplicate work pointed at a fictional module.

**Changes:**

- `src/dsar_orchestrator/pipeline.py`:
  - Delete `_run_pii_discovery` (around line 303–308).
  - `COARSE_TO_SUB["stage_2_parallel"]` becomes `("embed", "detect_2_1_to_2_4")`.
  - Drop the `cfg.discovery_enabled` branch in `_run_stage_2_parallel`.
- `src/dsar_orchestrator/stages.py`: delete `STAGE_ARTEFACTS["pii_discovery"]` + `_hash_register_plus_scope` (no other consumer).
- `src/dsar_orchestrator/module_agents.py`: delete `check_pii_discovery` function and its `CHECKERS` registry entry.
- `src/dsar_orchestrator/config.py`: keep `discovery_enabled` field as a deprecated no-op for one release; remove env-var resolution; document removal target = v0.5.0.
- `tests/_toolkit_stubs/stubs.py`: delete `make_pii_discovery_stub`; remove from `all_stubs()`.
- Update affected tests (per `grep -rln pii_discovery tests/`): `tests/test_stages.py`, `tests/test_resume_cascade.py`, `tests/test_module_agents.py`, `tests/integration/test_synthetic_case_100.py`, `tests/integration/test_full_pipeline_with_stubs.py`, `tests/integration/test_real_toolkit_smoke.py`. Drop `pii_discovery` from expected stage sets, stages-run assertions, and stub registration.
- CHANGELOG: `### Changed — v0.4.0 — BREAKING (pre-1.0 waiver)`: `pii_discovery` stage removed.

### Issue #11 — Rewire rerank to `tei_rerank_client`

Toolkit doesn't ship `dsar_rerank.core`. The real entry is `dsar_clients.tei_rerank_client.rerank_pairs(query, docs, ...)` → `RerankResult` (parallel to the embed adapter's existing tei-client rewire).

**Changes:**

- New `src/dsar_orchestrator/adapters/rerank.py`:
  - `PRODUCER_VERSION = "dsar_orchestrator.adapters.rerank 0.4.0"`, `SCHEMA_VERSION = "1.0"`.
  - `run_for_case(cfg, *, client=None)` — injectable client protocol parallel to `_EmbedResultLike` in `adapters/embed.py`.
  - Reads `working/cosine_prefilter.jsonl` for `(ref, text)` pairs via the Contract A helper `text_path_for_ref` from `register.py`.
  - Calls `tei_rerank_client.rerank_pairs(query=cfg.case_scope, docs=[texts])`.
  - Writes `working/scope_rerank.jsonl` with per-row `{ref, rerank_score, would_drop, mode, upstream_hash, schema_version, producer_version}` — shape preserved from existing stub fixture.
  - Module docstring states the retirement contract: retires when toolkit ships `dsar_pipeline.rerank.run_for_case(case_path)`.
- `src/dsar_orchestrator/pipeline.py`: replace `_run_rerank` body with `from dsar_orchestrator.adapters import rerank as _rerank; _rerank.run_for_case(cfg)`.
- `tests/_toolkit_stubs/stubs.py`: new `make_tei_rerank_client_stub` mirroring `make_tei_embed_client_stub`; update `all_stubs()`. Delete `make_rerank_core_stub` (pipeline.py no longer imports `dsar_rerank.core` after the rewire).
- New `tests/test_adapter_rerank.py`: 4–6 tests — happy path, empty input, threshold edge, would_drop=true, injected-client (mirror of `tests/test_adapter_verify_spec.py`).
- `tests/integration/test_real_toolkit_smoke.py`: extend `EXPECTED_TOOLKIT_MODULES` to include `dsar_clients.tei_rerank_client`; add a stage-3 assertion in the smoke test if the gated test currently stops earlier.

### Issue #12 — Smart-empty `pii_classify` check (interim)

Toolkit writes per-stage findings to `~/.dsar-audit/<case>/pii_findings_stage{1,2,3}.jsonl`; the conductor expects an aggregated `<case>/working/pii_collection.jsonl` that nothing writes. Long-term fix lives in the toolkit (issue to be filed); conductor side is interim.

**Changes (conductor):**

- `src/dsar_orchestrator/module_agents.py::check_pii_classify`:
  - Read `working/scope_verdicts.jsonl` if it exists.
  - Compute `in_scope_count = sum(1 for r in scope_verdicts if r.get("verdict") in {"present", "absent_but_subject_mentioned"})` — verify exact verdict-string set against toolkit's `dsar_pipeline.scope_check_stage`.
  - If `pii_collection.jsonl` missing/empty:
    - `in_scope_count == 0` → `severity=info`, `message="no in-scope docs; nothing to classify"`. Pipeline continues.
    - `in_scope_count > 0` → `severity=critical` (current behaviour preserved for the genuine concerning case). Pipeline halts.
  - Bump `PRODUCER_VERSION` on `adapters/pii_classify.py` to `0.4.0` (agent contract changed; producer in lockstep).
- New `tests/test_module_agent_pii_classify.py`: 3 cases — empty + no-in-scope (info), empty + in-scope (critical), populated (ok).

**Changes (toolkit side, filed separately):**

Issue on `harkers/dsar-toolkit`:
- Title: `pii_classifier_stage: write working/pii_collection.jsonl aggregating per-stage findings`
- Body: cites conductor#12; requests aggregation at end of `pii_classifier_stage.run()` into `<case>/working/pii_collection.jsonl`; row shape `{ref, finding_type, surface, confidence, source_stage, source_detector, schema_version, producer_version}`.

**Conductor follow-up (filed at same time):**

Issue on `harkers/dsar-orchestrator`:
- Title: `pivot adapters/pii_classify.py to consume toolkit-shipped pii_collection.jsonl`
- Body: links the toolkit issue + this PR; describes the one-line pivot when the toolkit lands aggregation; removes the smart-empty interim once the contract is owned end-to-end by the toolkit.

## 4. Testing strategy

**Hermetic** (default `.venv/bin/pytest -q`):

- Existing 292/1 baseline preserved minus pii_discovery-specific tests, plus the new ones.
- Expected count: **~297 tests** (`292 - 1 (pii_discovery cascade) + 6 (new adapter rerank tests + new agent tests + Contract B AST test)`).

**Real-toolkit** (`-m needs_toolkit`):

- Existing `test_real_toolkit_smoke.py` extended: stage-3 rerank now exercised; `EXPECTED_TOOLKIT_MODULES` list grows by 1.
- New `test_contract_b_no_fictional_modules.py` runs under the same marker: AST walk of conductor sources validates every literal-string `_lazy_import` / `importlib.import_module` target resolves.
- Gated count grows from 2 → ~4.

**Manual end-to-end acceptance gate** (before merge):

Run identical to the cross-test that surfaced these bugs:

```
dsar-conductor --case 900100 --case-root /tmp/e2e-cross/900100
```

Expected outcome:
- All 10 stages complete (ingest, stage_2_parallel, stage_3_parallel, scope_classify, pii_classify, redact, verify_spec, bake, verify_pdf, export).
- `output/` contains 5 redacted PDFs.
- `~/.dsar-audit/900100/module_checks.jsonl` shows an `info` row for pii_classify (`"no in-scope docs; nothing to classify"`) — not a critical halt.
- No `DISCOVERY_ENABLED=false` env override needed.

## 5. Version bump + release shape

| Axis | Change |
|---|---|
| Package version | `0.3.0` → `0.4.0` (pre-1.0 MINOR; #10 is breaking, waiver collapses to MINOR) |
| Schema version | none (no artefact wire format changes) |
| `adapters/rerank.py` | new at `0.4.0` |
| `adapters/pii_classify.py` PRODUCER_VERSION | `0.3.0` → `0.4.0` (agent contract changed) |
| All other adapter PRODUCER_VERSION | stays `0.3.0` (no behaviour change) |

**Branch + commit shape (mirrors Contract A / PR #9):**

| # | Commit | Files |
|---|---|---|
| 1 | `feat(stage): remove pii_discovery (closes #10)` | pipeline.py, stages.py, module_agents.py, config.py, stubs.py, affected tests |
| 2 | `feat(adapter): rerank via tei_rerank_client (closes #11)` | new adapters/rerank.py, pipeline.py, stubs.py, new test_adapter_rerank.py |
| 3 | `feat(agent): smart-empty pii_classify check (closes #12, interim)` | module_agents.py, new test_module_agent_pii_classify.py, adapters/pii_classify.py (PRODUCER_VERSION) |
| 4 | `docs+test: codify Contract B + bump 0.3.0 → 0.4.0` | VERSIONING.md §4, __init__.py docstring, new test_contract_b_no_fictional_modules.py, pyproject.toml + __init__.py version, CHANGELOG |

**Coordination:** toolkit-side issue + conductor follow-up issue filed *before* opening the conductor PR. Conductor PR does not block on the toolkit fix; the interim smart-empty agent keeps the pipeline green until the toolkit ships aggregation.

**Post-merge:** tag `v0.4.0`, push tag, re-run the manual cross-test on `main` to confirm no auto-merge silent breakage (the v5.0 → v5.5 rebase taught us this).

## Out of scope

- The toolkit-side `pii_collection.jsonl` aggregation — filed as a separate toolkit issue. Not in this PR.
- Pivoting `adapters/pii_classify.py` to consume the toolkit's aggregated file — filed as a conductor follow-up issue. Lands after the toolkit ships.
- Renaming `discovery_enabled` config field — kept as deprecated no-op for one release; removal at v0.5.0.
- New stages, new adapters beyond rerank, new orchestration features. This PR is strictly the three fixes + the durable contract codification.
