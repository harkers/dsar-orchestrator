# dsar-toolkit pipeline orchestration — design (v5.5)

**Status:** v5.5 — 2026-05-24. Rollout B phase 2. Adds the pre-bake
`verify_spec` stage to the conductor; sibling of v5.0's `verify_pdf`.

**Relationship to other specs.**
[`2026-05-24-pipeline-orchestration-design-v5.md`](2026-05-24-pipeline-orchestration-design-v5.md)
is the immediate predecessor (v5.0 — rollout B phase 1: bake split + verify_pdf rename + #1 fix).
v5.5 adds a single new coarse stage between `redact` and `bake`, mirroring v5.0's `verify_pdf` adapter at a different pipeline position.

## Version history

| Version | Date | Summary |
|---|---|---|
| v5 (rollout B v5.0) | 2026-05-24 | Bake split + redact_verify→verify_pdf rename + #1 fix. |
| v5.5 (rollout B phase 2) | 2026-05-24 | Add `verify_spec` as new pre-bake stage. Conductor `0.2.0 → 0.3.0`. |

---

## Why v5.5

v5 brainstorm locked the "two passes" decision: `verify_spec` (pre-bake, fails fast on plan errors) AND `verify_pdf` (post-bake, catches bake-introduced failures). v5.0 shipped `verify_pdf` and the structural reshape. v5.5 closes the loop by adding the pre-bake verifier.

**Why pre-bake matters.** Bake is the most expensive stage (multi-minute on real cases — produces redacted PDFs via `dsar-bake`). A doomed redaction plan (missing high-confidence findings, schema mismatch, orphan redactions) should be caught **before** spending bake time. Spec-verify reads the plan + the upstream evidence and surfaces failures cheaply.

## Toolkit state

`dsar_pipeline.verify_spec.verify_for_conductor(case_path: Path) -> Verdict` is **already merged on toolkit main** (2026-05-24). Returns the shared 4-field `Verdict` from `dsar_pipeline.verifier_verdict` (same dataclass `verify_pdf` returns). Writes failures to `<case>/working/verify_spec_findings.jsonl`. Implements 4 named checks (C1–C4). Never raises on bad data — always returns a `Verdict`.

## Stage shape

**v5.0 (today):**

```
... → 6 redact → 7 bake → 8 verify_pdf → 9 export
```

**v5.5 (this work):**

```
... → 6 redact → 7 verify_spec → 8 bake → 9 verify_pdf → 10 export
```

Three downstream stages renumber (bake 7→8, verify_pdf 8→9, export 9→10). `verify_spec` becomes the new Stage 7.

## Scope

**In v5.5:**

- New `adapters/verify_spec.py` mirroring `adapters/verify_pdf.py` shape.
- New `_run_verify_spec` helper in `pipeline.py`; StageBanner block between Stage 6 redact and Stage 8 bake.
- `STAGE_ORDER` and `SUB_STAGES_BY_STAGE` updated to insert `"verify_spec"` between `"redact"` and `"bake"`.
- New `STAGE_ARTEFACTS["verify_spec"]` entry in `stages.py` — artefact `working/verify_spec_findings.jsonl`, upstream hash over both `redaction_input.jsonl` and `pii_findings.jsonl`.
- New `check_verify_spec` in `module_agents.py`; same shape as `check_verify_pdf`.
- New `make_verify_spec_stub` in `tests/_toolkit_stubs/stubs.py`; `sys.modules` key `dsar_pipeline.verify_spec`.
- New `tests/test_adapter_verify_spec.py` — mirror of `test_adapter_verify_pdf.py`.
- Integration test updates: new `_fake_verify_spec_runner` per stub pattern; `stages_run` assertions add `verify_spec`.
- `log_analyser/collectors.py` — add `verify_spec_findings.jsonl` to the working-dir collection alongside `post_bake_findings.jsonl`. Same `severity == "high"` counting in `basic_stats.verify_failed_count`.
- Conductor MINOR bump: `0.2.0 → 0.3.0` per [`VERSIONING.md`](../../../VERSIONING.md) pre-1.0 waiver (additive new stage + breaking on stage numbering shift).
- CHANGELOG entries under `[Unreleased]`.

**NOT in v5.5:**

- `verify_pdf`, `bake`, `export` adapters (unchanged from v5.0).
- The toolkit (already ships `verify_for_conductor` — no PR needed).
- Config field names.
- Any new CLI flag.

## Adapter contract

`src/dsar_orchestrator/adapters/verify_spec.py`:

- Lazy-imports `dsar_pipeline.verify_spec`; calls `mod.verify_for_conductor(cfg.case_path)`.
- Receives a 4-field `Verdict`. Direct attribute access on `verdict.audit_log_path` (no `getattr` fallback — per v5.0 Phase 1b code-review lesson).
- On `verdict.all_passed == False`: raises `PipelineHalt` with halt message including `verdict.failed_doc_count`, `verdict.failed_verifier_summary`, and `verdict.audit_log_path` (which points at `working/verify_spec_findings.jsonl`).
- Halt message resume hint: `dsar-conductor --case <id> --from redact` (operator likely needs to re-classify or re-redact to fix plan-level issues).
- No `cfg.<flag>_enabled` gate (verify_spec is always-on; the operator's only way to skip it would be `--from bake` or later).

`PRODUCER_VERSION = "dsar_orchestrator.adapters.verify_spec 0.3.0"`.

## Code touch points

### `src/dsar_orchestrator/pipeline.py`

- `STAGE_ORDER`: insert `"verify_spec"` between `"redact"` and `"bake"`.
- `SUB_STAGES_BY_STAGE`: add `"verify_spec": ("verify_spec",)`.
- Add `_run_verify_spec` function (mirror of `_run_verify_pdf`).
- Add StageBanner block in `run()` between Stage 6 redact and Stage 8 bake; renumber subsequent banner comments (Stage 8 bake, Stage 9 verify_pdf, Stage 10 export).
- Module docstring + run() docstring: 9 stages → 10 stages; v5 → v5.5 spec ref.

### `src/dsar_orchestrator/stages.py`

- Add `_hash_verify_spec_inputs` helper: sha256 over `redaction_input.jsonl` content + `pii_findings.jsonl` content (separator `\x1f`). Match what verify_spec reads.
- Add `STAGE_ARTEFACTS["verify_spec"]` between `"redact"` and `"bake"`:

  ```python
  "verify_spec": StageArtefact(
      "verify_spec",
      "verify_spec",
      "working/verify_spec_findings.jsonl",
      _hash_verify_spec_inputs,
  ),
  ```

### `src/dsar_orchestrator/module_agents.py`

- Add `check_verify_spec` (mirror of `check_verify_pdf`); points at `cfg.case_path / "working" / "verify_spec_findings.jsonl"`, counts `severity == "high"` rows for failure.
- Insert `"verify_spec": check_verify_spec,` in CHECKERS dict between `"redact"` and `"bake"`.

### `src/dsar_orchestrator/log_analyser/collectors.py`

- Add `"verify_spec_findings.jsonl"` to `WORKING_KNOWN_LOGS` tuple (alongside `"post_bake_findings.jsonl"`).
- `basic_stats.verify_failed_count` — extend to count `severity == "high"` rows from **both** `verify_spec_findings.jsonl` and `post_bake_findings.jsonl` combined. Single count; downstream consumers don't need to disambiguate spec vs PDF failures (both are "verifier failed → operator must investigate"). If a future use case needs the split, add a separate `verify_spec_failed_count` field then.

### `src/dsar_orchestrator/log_analyser/prompts.py`

- System prompt category mentioning `post_bake_findings.jsonl` rows with severity=='high' — extend to mention `verify_spec_findings.jsonl` too. One sentence change.

### `src/dsar_orchestrator/__init__.py`, `pyproject.toml`

- Bump `__version__` and `version` to `0.3.0`.

### `CHANGELOG.md`

- `[Unreleased]` entries: new `verify_spec` stage; stage numbering shift (bake 7→8, verify_pdf 8→9, export 9→10); flag both breakings under pre-1.0 waiver.

## Test changes

### `tests/_toolkit_stubs/stubs.py`

Add `make_verify_spec_stub()` mirroring `make_post_bake_verify_stub()`:

- `types.ModuleType("dsar_pipeline.verify_spec")`
- Inner function `verify_for_conductor(case_path)` returning a `Verdict` (with `audit_log_path = case_path / "working" / "verify_spec_findings.jsonl"`)
- Writes a stub finding row in real toolkit's shape (severity, ref, check, issue) — same discipline as post_bake_verify stub fix from v5.0 Phase 1b.
- Register in `STUBS` dict (or equivalent) at `"dsar_pipeline.verify_spec"`.

### `tests/test_adapter_verify_spec.py` (new)

Mirror of `tests/test_adapter_verify_pdf.py`:

- `_PassingVerdict` + `_FailingVerdict` with 4 fields each (including `audit_log_path` pointing at `working/verify_spec_findings.jsonl`).
- Tests: `test_completes_silently_when_verdict_passes`, `test_verifier_receives_case_path`, `test_halts_pipeline_when_verdict_fails`, `test_halt_message_includes_audit_log_path` (assert the verify_spec findings path appears), `test_halt_message_includes_resume_hint` (assert `--from redact`).
- No `test_no_op_when_disabled` — verify_spec has no enable flag.

### Integration tests

- `tests/integration/test_full_pipeline_with_stubs.py`:
  - Add `_fake_verify_spec_runner` to the `with_toolkit_stubs` fixture. Pattern mirrors the existing `_fake_bake_runner` / `_fake_verify_pdf_runner` (whichever name is in use). Writes a passing findings file.
  - `stages_run` assertion: add `"verify_spec"` to the expected list.
- `tests/integration/test_synthetic_case_100.py`: same.

### Module-agent test updates

- `tests/test_module_agents.py`: add 3 sibling tests for `check_verify_spec` (skip-when-disabled — actually, no, verify_spec is always-on; just keep happy + critical-when-missing tests).

### Test files that hardcode stage count

- `tests/test_pipeline_smoke.py::test_stage_order_includes_all_nine_stages` → rename to `_all_ten_stages`; update tuple.
- `tests/test_resume_cascade.py::test_skip_fresh_artefacts_false_includes_everything` — add `verify_spec` to expected list.
- Comment drift: `# All 9 coarse stages` → `# All 10 coarse stages` in resume cascade tests; module docstrings.

## Backward compatibility

- **CLI flag**: no new flags; existing `--from <stage>` / `--only <stage>` continue to work but now accept `verify_spec` as a valid value. Per pre-1.0 waiver, the stage-numbering shift is allowed on MINOR.
- **Resume cascade**: a v5.0 case mid-flight cannot resume cleanly under v5.5 (the new stage means `verify_spec` will always run on first v5.5 attempt — bake's resume eligibility doesn't trigger early because `verify_spec` is a new prerequisite). Operators with in-flight v5.0 cases should let them finish under v5.0 (pin to 0.2.x) or restart under v5.5 from `--from redact`.
- **Audit log**: `verify_spec_findings.jsonl` is a new toolkit-owned artefact path. Add to the toolkit-side filename ownership table ([harkers/dsar-toolkit#3](https://github.com/harkers/dsar-toolkit/issues/3)) when the toolkit's `audit_paths` registry next iterates.

## Versioning

Per [`VERSIONING.md`](../../../VERSIONING.md):

- Conductor `0.2.0 → 0.3.0` (MINOR per pre-1.0 waiver).
- `__version__` in `__init__.py` + `pyproject.toml` bumped together.
- `PRODUCER_VERSION` for new `verify_spec` adapter: `"dsar_orchestrator.adapters.verify_spec 0.3.0"`.
- **All other adapter `PRODUCER_VERSION` strings bump to `0.3.0` in lockstep** per VERSIONING.md §3 ("the `<package_version>` portion **always tracks the conductor's `__version__`**"). Touches `verify_pdf.py`, `bake.py`, `export.py`, and every other adapter with a `PRODUCER_VERSION` constant.
- Side note: the "bump every adapter PRODUCER_VERSION on every conductor release" rule may deserve revisiting in a future VERSIONING.md iteration (cost: noise on every release; benefit: artefact-rows pinpoint which conductor version produced them). Not in v5.5 scope; follow the existing rule.

- CHANGELOG `[Unreleased]` → `[0.3.0] - <date>` at release.
- Tag `v0.3.0` at release time.

## Cross-repo coordination

- Toolkit already ships `verify_for_conductor` on main; no toolkit change needed for v5.5.
- Pyproject pin: stay at `dsar-pipeline >= 0.2.0` (toolkit hasn't cut a fresh tag yet). Update note in CHANGELOG if/when toolkit tags.
- Filename ownership (toolkit #3): note that `working/verify_spec_findings.jsonl` is toolkit-owned; the conductor only reads, never writes. The toolkit's `audit_paths` registry should know about it.

## Done when

- [ ] `adapters/verify_spec.py` shipped with `_default_verifier` + `run_for_case` mirroring `verify_pdf.py` shape.
- [ ] `pipeline.py` STAGE_ORDER + SUB_STAGES_BY_STAGE updated; `_run_verify_spec` defined; StageBanner block inserted; comments renumbered.
- [ ] `stages.py` has `_hash_verify_spec_inputs` + `STAGE_ARTEFACTS["verify_spec"]`.
- [ ] `module_agents.py` has `check_verify_spec` + CHECKERS entry.
- [ ] `log_analyser/collectors.py` collects `verify_spec_findings.jsonl`; `verify_failed_count` counts both files combined.
- [ ] `log_analyser/prompts.py` references both files.
- [ ] Stub: `make_verify_spec_stub` + `dsar_pipeline.verify_spec` registry entry.
- [ ] `tests/test_adapter_verify_spec.py` shipped (5 tests).
- [ ] Integration tests: new `_fake_verify_spec_runner`; `stages_run` assertions updated.
- [ ] `tests/test_pipeline_smoke.py` and `tests/test_resume_cascade.py` updated for 10-stage shape.
- [ ] All tests pass.
- [ ] Conductor `0.3.0` in `__init__.py` + `pyproject.toml`; all adapter `PRODUCER_VERSION` strings bumped to `0.3.0`.
- [ ] CHANGELOG `[Unreleased]` entries.

## What v5.5 does NOT change

- v4 adapter pattern.
- Resume cascade mechanism.
- Module-agent validation framework.
- `PipelineHalt` semantics.
- The 14-stage toolkit state machine.
- Conductor-owned audit files (pipeline.jsonl, module_checks.jsonl, analysis.jsonl).
- Config / subject-identifier / mode fields.

## Open questions

None blocking. PRODUCER_VERSION discipline question (§Versioning) is noted as a future-VERSIONING.md discussion but not blocking v5.5.
