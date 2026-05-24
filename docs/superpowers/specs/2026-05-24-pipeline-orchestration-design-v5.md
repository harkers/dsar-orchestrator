# dsar-toolkit pipeline orchestration — design (v5)

**Status:** v5 — 2026-05-24. Rollout B (phased). This spec covers
**v5.0** — the structural reshape (bake split + redact_verify
rewire/rename to verify_pdf). **v5.5** (adding `verify_spec` as a
pre-bake stage) is a separate later spec.

**Relationship to other specs.**
[`2026-05-22-pipeline-orchestration-design-v4.md`](2026-05-22-pipeline-orchestration-design-v4.md)
is the immediate predecessor — read it first for the adapter pattern
(v4 introduced the conductor-owned `adapters/` layer). v5.0 changes
the stage table and updates two adapters; the v4 adapter contract,
resume cascade, module-agent validation, and halt semantics are
unchanged.

## Version history

| Version | Date | Summary |
|---|---|---|
| v1–v4 | 2026-05-22 → 2026-05-23 | Adapter layer locked in v4. |
| v5 (rollout B v5.0) | 2026-05-24 | Structural reshape: bake extracted from export into its own stage; `redact_verify` renamed to `verify_pdf` and rewired to the toolkit's `dsar_pipeline.post_bake_verify.verify_for_conductor` Python entry (merged on toolkit main 2026-05-24). |
| v5.5 (rollout B phase 2) | future | Add `verify_spec` as pre-bake stage. Separate spec when scoped. |

---

## Why v5 / rollout B

v4 left an explicit parked tension: `redact_verify` (Stage 7 in v4)
expected `case_path/redacted/` to exist, but `redacted/` was only
produced by `dsar-bake` — which ran inside the `export` adapter at
Stage 8, **after** verify. Integration tests fudged this by having
the redact-fake also write stand-in `redacted/<ref>.txt` files.

The 2026-05-24 brainstorm picked rollout B (phased): land the
structural reshape now, defer `verify_spec` to a later phase. The
toolkit shipped both `verify_for_conductor` entries overnight
(2026-05-24), so v5.0's structural fix is unblocked.

v5.0 also incorporates bug fix
[harkers/dsar-orchestrator#1](https://github.com/harkers/dsar-orchestrator/issues/1)
— the v4 `redact_verify` adapter lazy-imported the fictional
`dsar_redact_verify.core`; v5.0 rewires it to the real toolkit
module.

## Scope

**In v5.0 (this spec):**

- Split `bake` out of `adapters/export.py` into a new
  `adapters/bake.py` coarse stage.
- Rename `redact_verify` stage → `verify_pdf` across all touch points.
- Rewire `verify_pdf` adapter to `dsar_pipeline.post_bake_verify.verify_for_conductor` — fixes #1 as the first step.
- Renumber: stages 6 (redact), 7 (bake), 8 (verify_pdf), 9 (export).
- Update integration tests to remove the `redacted/<ref>.txt` fudge.
- Bump `dsar-pipeline` toolkit pin from `>= 0.2.0` to `>= 0.2.1`
  (gated on the toolkit cutting that tag — see "Cross-repo
  coordination" below).
- Conductor MINOR bump: `0.1.0` → `0.2.0` per `VERSIONING.md`
  pre-1.0 waiver (breaking + additive both allowed at MINOR
  pre-1.0).

**Explicitly NOT in v5.0:**

- `verify_spec` stage — deferred to v5.5.
- Renaming any other adapter or stage.
- Changing the adapter pattern.

## Stage shape

**v4 (today):**

```
... → 6 redact → 7 redact_verify → 8 export
                                    └── internally: dsar-bake → python -m dsar_pipeline.export
```

**v5.0 (this work):**

```
... → 6 redact → 7 bake → 8 verify_pdf → 9 export
                                          └── only: python -m dsar_pipeline.export (bake extracted)
```

The verifier now runs **after** bake — so it can actually see
`case_path/redacted/`. That's the bug fix the whole reshape exists for.

## Adapter changes

### Renamed + rewired: `adapters/redact_verify.py` → `adapters/verify_pdf.py`

- File renamed.
- `_default_verifier()` imports `dsar_pipeline.post_bake_verify`
  (not the fictional `dsar_redact_verify.core`).
- Calls `verify_for_conductor(case_path)` (returns the
  toolkit's 4-field `Verdict` from
  `dsar_pipeline.verifier_verdict`).
- `PipelineHalt` message includes `verdict.audit_log_path` so
  operators get a direct path to the failure log.
- Retirement contract: now partially fulfilled (toolkit ships the
  entry). Adapter stays for halt-formatting + injection + no-op
  gate when `cfg.redact_verify_enabled` is False — these remain
  conductor concerns.
- `PRODUCER_VERSION` bumps to `"dsar_orchestrator.adapters.verify_pdf 0.2.0"`.

### Split: `adapters/export.py` → `adapters/bake.py` + slimmed `adapters/export.py`

- **New** `adapters/bake.py`:
  - Subprocess runner around `dsar-bake --case <id>` (the existing
    toolkit CLI; PR #23 doesn't change it).
  - Writes manifest at `working/redact_v4/bake_manifest.json` with
    `upstream_hash` (sha256 of `redaction_input.jsonl`) +
    `schema_version` + `producer_version`.
  - Retirement trigger: `dsar_pipeline.bake.run_for_case(case_path)` (not yet on toolkit; no toolkit issue filed).
  - `PRODUCER_VERSION = "dsar_orchestrator.adapters.bake 0.1.0"`.
- **Slimmed** `adapters/export.py`:
  - Drops the bake subprocess call (lines that invoke `dsar-bake`).
  - Now only runs `python -m dsar_pipeline.export`.
  - Cascade-anchor manifest (`output/manifest.json`) still written.
  - `PRODUCER_VERSION` bumps to `"dsar_orchestrator.adapters.export 0.2.0"`.

## Code touch points

### `src/dsar_orchestrator/stages.py`

- `STAGE_ORDER`: rename `"redact_verify"` → `"verify_pdf"`; insert `"bake"` between `"redact"` and `"verify_pdf"`.
- `STAGE_DEPS`: bake depends on redact; verify_pdf depends on bake; export depends on verify_pdf.
- `STAGE_PARALLEL_GROUPS`: unchanged (bake/verify_pdf/export remain sequential).
- `STAGE_ARTEFACTS`:
  - New entry `"bake"` — artefact = `working/redact_v4/artefacts_redacted/` (directory) + `working/redact_v4/bake_manifest.json` (cascade anchor).
  - Rename `"redact_verify"` entry to `"verify_pdf"`; artefact path updates to `working/post_bake_findings.jsonl` (toolkit-owned write target — see `dsar_pipeline.post_bake_verify.verify_for_conductor`). Replaces the v4 `~/.dsar-audit/<case>/redact_verify.jsonl` path which is no longer written.
  - Update export entry — no longer manages bake output; manages only `output/`.
- Reuse existing `_hash_redaction_input` (from current export adapter) as bake's upstream.

### `src/dsar_orchestrator/pipeline.py`

- `_run_redact_verify` → `_run_verify_pdf`; imports renamed adapter.
- `_run_export` → split into `_run_bake` + slimmed `_run_export`.
- StageBanner calls: 3 banners (bake/verify_pdf/export) where there were 2 (redact_verify/export).
- StageBanner ordering follows STAGE_ORDER.

### `src/dsar_orchestrator/cli.py`

- `--from` / `--only` validation accepts `bake`, `verify_pdf`; rejects `redact_verify`.
- **No back-compat alias** — pre-1.0 waiver allows breaking on MINOR. Cleaner than a one-version alias.

### `src/dsar_orchestrator/module_agents.py`

- Registry key rename: `"redact_verify"` → `"verify_pdf"`.
- New entry for `"bake"`: trivial validator — every ref in `working/register.json` must exist as a file under `working/redact_v4/artefacts_redacted/`.

### `.importlinter`

- Contract 9 adapter list: add `bake`, `verify_pdf`; remove `redact_verify`.

## Test changes

### `tests/_toolkit_stubs/stubs.py`

- Rename `make_redact_verify_stub()` → `make_post_bake_verify_stub()`.
- Change `sys.modules` key from `"dsar_redact_verify.core"` →
  `"dsar_pipeline.post_bake_verify"`.
- Stub `verify_for_conductor` function (not `verify_case`).
- Stub `Verdict` carries the 4 fields (add `audit_log_path: Path`).
- **New** `make_bake_stub()` providing a `dsar-bake` subprocess
  fake — replaces the redact-fake's stand-in `redacted/<ref>.txt`
  writing.
- **New** `make_post_bake_findings_stub()` — fake
  `working/post_bake_findings.jsonl` writer for the verify_pdf
  path (so the verify-fake can read it and emit the 4-field
  Verdict).

### `tests/integration/`

- `test_full_pipeline_with_stubs.py`:
  - **Remove** the `redacted/<ref>.txt` fudge from the redact-fake
    (the comment block in current code explicitly notes this as a
    stand-in for "the not-yet-built bake step inside the export
    adapter"). The bake-fake now writes them as its real output.
  - `stages_run` assertion: adds `"bake"`, renames
    `"redact_verify"` → `"verify_pdf"`.
- `test_synthetic_case_100.py`: same renames and bake stage addition.

### Unit tests

- **New** `tests/dsar_orchestrator/test_adapters_bake.py` — tests
  the new bake adapter (subprocess runner, manifest writing, error
  cases, missing-tool path).
- **Rename** `test_adapters_redact_verify.py` →
  `test_adapters_verify_pdf.py`; rewired to the new module + the
  4-field Verdict; halt message asserted to include
  `audit_log_path`.
- Update `tests/dsar_orchestrator/test_pipeline.py` for the new
  `_run_*` helpers.
- Update `tests/dsar_orchestrator/test_stages.py` for STAGE_ORDER
  changes.

### Import-linter

- `lint-imports` runs in CI per existing convention; passes after
  contract 9 update.

## Backward compatibility

- **CLI flag rename is breaking.** Per VERSIONING.md pre-1.0 waiver,
  this is allowed on MINOR. No alias for `--from redact_verify`.
- **Per-case audit file rename** (`redact_verify.jsonl` →
  `post_bake_findings.jsonl`): v4 cases keep their old audit dirs;
  new v5.0 runs write the new file under
  `working/post_bake_findings.jsonl` (toolkit-owned). No migration
  script — operators don't query the audit dir by name; the
  cascade discovers it via STAGE_ARTEFACTS.
- **Resume cascade compatibility:** a v4 case mid-flight before
  v5.0 cannot resume cleanly under v5.0 — the stage table has
  changed. Operators with in-flight cases should either:
  - Finish under v4 (pin to conductor 0.1.x), or
  - Restart from `--from redact` under v5.0 (the redact artefact
    is stable across the rename).

## Versioning

Per [`VERSIONING.md`](../../../VERSIONING.md):

- **Conductor: `0.1.0` → `0.2.0`** (MINOR bump). Pre-1.0 waiver
  allows breaking + additive at MINOR.
- `__version__` in `src/dsar_orchestrator/__init__.py` and
  `pyproject.toml` bumped together in the same commit.
- `PRODUCER_VERSION` strings in renamed/new adapters bump to
  `0.2.0`. Both verify_pdf and bake adapters track the conductor's
  `__version__` per VERSIONING.md §3 ("the `<package_version>`
  portion **always tracks the conductor's `__version__`**"). New
  bake adapter starts life at the conductor's current version
  (0.2.0), not 0.1.0.
- CHANGELOG.md `[Unreleased]` entry under `Added` and `Changed`
  per Keep-a-Changelog format; moved to `[0.2.0] - <date>` at
  release time.
- Tag `v0.2.0` per release process.

## Cross-repo coordination

This work depends on toolkit-side changes that have already merged
to toolkit main but are **not yet tagged in a toolkit release**:

| Toolkit-side requirement | Status (2026-05-24) |
|---|---|
| `dsar_pipeline.post_bake_verify.verify_for_conductor` | Merged on main |
| `dsar_pipeline.verifier_verdict.Verdict` (4-field) | Merged on main |
| `dsar_pipeline.verify_spec.verify_for_conductor` | Merged on main (used in v5.5 — not v5.0) |

**Coordination steps:**

1. **Prereq:** ask toolkit team to cut a release tag (e.g. `v0.2.1`)
   that includes the above. File on
   [harkers/dsar-toolkit](https://github.com/harkers/dsar-toolkit/issues)
   if not already in flight.
2. Conductor PR bumps `pyproject.toml`:
   `"dsar-pipeline >= 0.2.0"` → `"dsar-pipeline >= 0.2.1"`.
3. Without that tag, conductor v5.0 only runs against an editable
   install of toolkit at HEAD. Document this in the conductor
   `[Unreleased]` CHANGELOG entry until the toolkit tag lands.

## Done when

- [ ] `adapters/redact_verify.py` deleted; `adapters/verify_pdf.py`
      shipped (rewired to real toolkit; halt message uses
      `audit_log_path`).
- [ ] `adapters/bake.py` shipped; `adapters/export.py` slimmed.
- [ ] `stages.py`, `pipeline.py`, `cli.py`, `module_agents.py`,
      `.importlinter` updated.
- [ ] All existing tests pass after each commit in the PR series.
- [ ] New unit tests: `test_adapters_bake.py`,
      `test_adapters_verify_pdf.py` (rename).
- [ ] Integration tests: redact-fake stops writing
      `redacted/<ref>.txt`; bake-fake writes them; new
      verify_pdf-fake reads `post_bake_findings.jsonl`.
- [ ] `__version__` and `pyproject.toml` bumped to `0.2.0`;
      `PRODUCER_VERSION` strings bumped.
- [ ] CHANGELOG `[Unreleased]` entry; `v0.2.0` tag at release.
- [ ] Conductor #1 closed by the rewire commit; conductor #2 v5
      epic moves to "v5.0 shipped; v5.5 deferred"; conductor #4
      retirement tracker updated.
- [ ] Toolkit pin bump deferred to a follow-up if toolkit hasn't
      tagged yet at PR merge time — note in CHANGELOG.

## What v5.0 does NOT change

- v4 adapter pattern (single-injectable-dependency contract).
- Resume cascade via `upstream_hash` chain.
- Module-agent validation framework.
- `PipelineHalt` semantics for verify stages.
- The 14-stage toolkit state machine.
- `~/.dsar-audit/<case>/pipeline.jsonl` / `module_checks.jsonl` /
  `analysis.jsonl` (conductor-owned audit files).
- `case_config.json` / subject-identifier / mode fields.

## v5.5 candidates (separate spec when scoped)

- Add `verify_spec` as pre-bake stage between `redact` and `bake`.
  Toolkit ships `dsar_pipeline.verify_spec.verify_for_conductor`
  already (merged 2026-05-24). New adapter, second stage
  renumbering churn.
- Adapter retirements for other v4 adapters as toolkit ships their
  retirement-trigger entries (see conductor #4).

## Open questions

None blocking. v5.5 timing is operator-paced; no in-flight v4
cases to migrate; toolkit release tag is the only external
dependency and is straightforward.
