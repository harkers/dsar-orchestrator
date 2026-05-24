# Pipeline orchestration v5 — brainstorm notes (paused)

**Status:** brainstorming paused 2026-05-24, before the rollout-shape
decision. Five shape decisions are locked. No spec written; no code
changes; no plan.

This file exists so the in-flight state is visible on `git log` /
file browse rather than only in private memory. It is **not** a
versioned spec — when v5 brainstorming resumes and produces a spec,
that lives at `docs/superpowers/specs/<date>-pipeline-orchestration-design-v5.md`.

## What v5 is trying to fix

Spec v4's "parked for v5" tension. Conductor stage order is
`redact (6) → redact_verify (7) → export (8)`, but the toolkit's
verifier inspects redacted PDFs that only exist after `dsar-bake`,
which currently runs *inside* the export adapter (step 1 of 2).
Integration tests fudge it by having the redact-fake also write
stand-in `redacted/<ref>.txt` files. Real cases would fail at
verify_case because `case_path/redacted/` doesn't exist yet.

See [`docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v4.md`](../specs/2026-05-22-pipeline-orchestration-design-v4.md)
section "v5 candidates".

## Significant discovery (filed as separate bug)

**The v4 `redact_verify` adapter is broken against the real
toolkit.** It lazy-imports `dsar_redact_verify.core.verify_case`,
but `dsar_redact_verify` does not exist in
`~/projects/dsar-toolkit/src/`. The integration tests pass purely
because `tests/_toolkit_stubs/stubs.py` installs a fake module in
`sys.modules`.

The real toolkit ships:

- `dsar_pipeline/post_bake_verify_stage.py` — `PostBakeVerifyStage(BaseStage)` with CLI entry. The actual PDF verifier the conductor should be calling.
- `dsar_pipeline/verify.py:verify_redacted_artifact(path, entities_redacted)` — per-artifact helper.
- `dsar_pipeline/bake_stage.py` — `dsar-bake` CLI (currently invoked by `adapters/export.py`).
- **No** spec-level verifier (would be new toolkit work).

The pointer-rewire needs filing as a bug regardless of whether v5
ever happens. Filed as a separate issue alongside the v5 epic.

## Five decisions locked this session

| # | Question | Choice |
|---|---|---|
| 1 | v5 scope | **Toolkit can change too** (coordination issues on the table). |
| 2 | What does verify catch? | **Both, in two passes** — spec-verify before bake (cheap, fail fast on plan errors); PDF-verify after bake (catches bake-introduced failures). |
| 3 | Where does bake live in stage taxonomy? | **Its own coarse stage between two verifies.** New shape: `redact → verify_spec → bake → verify_pdf → export` (renames `redact_verify`, splits bake from export). |
| 4 | Verify ownership | **Toolkit owns both.** Toolkit ships `dsar_pipeline.post_bake_verify.verify_for_conductor(case_path)` (wraps existing `PostBakeVerifyStage`) and a new `dsar_pipeline.verify_spec.verify_for_conductor(case_path)`. Conductor has two thin injection-only adapters (same shape as v4's `pii_discovery`). |
| 5 | Halt semantics | **Both hard-halt.** Both verify stages raise `PipelineHalt` on any failure. |

## Stopped at: rollout shape

Three options were on the table; none picked:

- **A — Full v5 in one shot** (was recommended): split bake, rename
  `redact_verify` → `verify_pdf`, add `verify_spec`, renumber stages
  7-8 → 7,8,9,10, update tests, file toolkit coordination asks.
- **B — Phased.** v5.0 = structural (extract bake, rewire/rename verify
  to point at real `post_bake_verify`). v5.5 = add `verify_spec` once
  toolkit ships it. Smaller first PR; two numbering churns.
- **C — Minimal.** Rewire-only. Contradicts decision #2 ("two passes").

## How to resume

1. Don't re-litigate decisions 1-5 unless they're explicitly reopened.
2. Restart the brainstorm at the rollout question. Once picked, the
   brainstorming skill flow proceeds to: propose design → present
   sections → write spec at
   `docs/superpowers/specs/<resume-date>-pipeline-orchestration-design-v5.md`
   → plan → branch.
3. The broken-adapter fix is independent and can land before any v5
   work — even rollout C requires that pointer-rewire.
