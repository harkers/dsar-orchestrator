# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: see [`VERSIONING.md`](VERSIONING.md).

## [Unreleased]

### Fixed — 0.1.1 (issue #8: register.json shape)
- Closes #8 — conductor's `register.json` consumers were assuming a dict envelope `{refs: [...], upstream_hash, schema_version, producer_version}` but the real toolkit writes a **flat list** of file-record dicts. End-to-end runs crashed at ingest with `AttributeError: 'list' object has no attribute 'get'`. Hermetic tests passed because the in-test stubs synthesised dict-shape registers.
- **Contract A**: conductor adapts to the toolkit's flat-list shape. Conductor-owned metadata (`upstream_hash`, `schema_version`, `producer_version`) moves to a sibling file `working/register_meta.json` written by the conductor's ingest adapter.
- 9 source sites updated across `hash_chain.py`, `adapters/ingest.py`, `adapters/embed.py`, `module_agents.py`, `stages.py`. New leaf module `register.py` houses the shape helpers (`read_register`, `text_path_for_ref`, `read_register_meta`, `write_register_meta`) so module_agents and hash_chain can share them without violating import-linter contract 7.
- `STAGE_ARTEFACTS["ingest"]` cascade anchor moves from `working/register.json` (toolkit-owned) to `working/register_meta.json` (conductor-owned).
- Hermetic test fixtures across 10 test files updated to produce the toolkit's flat-list shape — prevents drift from re-introducing the bug.
- New `tests/integration/test_real_toolkit_smoke.py` (gated behind `@pytest.mark.needs_toolkit`) exercises the conductor's ingest adapter against the real toolkit; would have caught the bug on first run. Self-skips when toolkit / TEI / spaCy model not available.

### Added — v5.5 (rollout B phase 2)
- New `verify_spec` coarse stage (Stage 7 in the new 10-stage numbering) — pre-bake plan-level verifier. New `adapters/verify_spec.py` lazy-imports `dsar_pipeline.verify_spec.verify_for_conductor` (toolkit-shipped 2026-05-24); halt message includes the toolkit's `audit_log_path` field. Always-on (no enable flag — operators skip via `--from bake` or later).
- New `check_verify_spec` module-agent validator + registry entry. Mirror of `check_verify_pdf` at the new pre-bake stage.
- New `make_verify_spec_stub` in `tests/_toolkit_stubs/stubs.py`. Writes audit rows in the real toolkit's verify_spec shape (check/ref/severity/issue/…); `upstream_hash` at top level so resume cascade reads it.
- `log_analyser/collectors.py` extended: `WORKING_KNOWN_LOGS` now includes `verify_spec_findings.jsonl`; `basic_stats.verify_failed_count` counts severity-high rows across both files (spec + post-bake).
- 6 new tests in `tests/test_adapter_verify_spec.py` covering happy path, failure halt, audit_log_path in message, resume hint, missing-optional-fields tolerance.

### Changed — v5.5
- **BREAKING (pre-1.0 waiver applies):** Stage numbering shifts again — bake is now Stage 8 (was 7), verify_pdf is Stage 9 (was 8), export is Stage 10 (was 9). Resume cascade for in-flight v5.0 cases is not preserved; restart from `--from redact`.
- All adapter `PRODUCER_VERSION` strings bumped to `0.3.0` in lockstep per VERSIONING.md §3 (the `<package_version>` portion tracks the conductor's `__version__`).
- Conductor version: `0.2.0` → `0.3.0` (MINOR per pre-1.0 waiver: additive new stage + breaking on stage numbering shift).

### Added — v5.0 (rollout B phase 1)
- `VERSIONING.md` documenting the package/schema/producer version policy.
- `CHANGELOG.md` (this file).
- `docs/superpowers/brainstorms/2026-05-24-v5-paused-notes.md` capturing the in-flight v5 pipeline-orchestration brainstorm.
- New `bake` coarse stage (Stage 7 in v5.0; Stage 8 after v5.5) — extracted from the export adapter. New `adapters/bake.py` subprocess wrapper around `dsar-bake --case <id>`. Writes cascade-anchor manifest at `working/redact_v4/bake_manifest.json`.
- New `adapters/verify_pdf.py` (renamed from `adapters/redact_verify.py`) — rewired to the real `dsar_pipeline.post_bake_verify.verify_for_conductor` toolkit entry. Halt message now includes the toolkit's `audit_log_path` field.
- New `check_bake` module-agent validator + registry entry.
- New `check_verify_pdf` module-agent validator (renamed from `check_redact_verify`).

### Changed
- **BREAKING (pre-1.0 waiver applies):** Stage `redact_verify` renamed to `verify_pdf`. `--from redact_verify` / `--only redact_verify` no longer accepted by `dsar-conductor`; use `--from verify_pdf` instead.
- **BREAKING (pre-1.0 waiver applies):** Stage numbering shifts — `export` is now Stage 9 (was Stage 8); `verify_pdf` is Stage 8 (was redact_verify at Stage 7); `bake` is the new Stage 7 (was inside Stage 8 export). Resume cascade for in-flight v4 cases is not preserved; restart from `--from redact`.
- Verify stage now runs **after** bake (was before), so `dsar_pipeline.post_bake_verify.verify_for_conductor` can actually see `<case>/redacted/`. Closes #1.
- `STAGE_ARTEFACTS["verify_pdf"].artefact_relpath` updates to `working/post_bake_findings.jsonl` (toolkit-owned write target), replacing the v4 `~/.dsar-audit/<case>/redact_verify.jsonl` path.
- `adapters/export.py` slimmed — no longer invokes `dsar-bake`; only runs `python -m dsar_pipeline.export`. Manifest at `output/manifest.json` unchanged.
- `PRODUCER_VERSION` strings in `verify_pdf`, `bake`, and `export` adapters bumped to `0.2.0`.

### Fixed
- #1 — `adapters/redact_verify.py` no longer imports the fictional `dsar_redact_verify.core` module. Toolkit ships `dsar_pipeline.post_bake_verify.verify_for_conductor` as of 2026-05-24; adapter rewired (closed by the rename to verify_pdf).

### Coordination
- Requires dsar-toolkit at HEAD or a release tag including the merged `dsar_pipeline.post_bake_verify.verify_for_conductor` + `dsar_pipeline.verifier_verdict.Verdict` (4-field) work. If toolkit hasn't cut such a tag at conductor PR merge time, the `pyproject.toml` pin stays at `dsar-pipeline >= 0.2.0` and an editable install at toolkit HEAD is the operator's responsibility.

## [0.1.0] - 2026-05-23

Initial tagged release. State as of commit `cd1594f` (immediately after
the v4 adapter sprint).

### Added
- 8-coarse-stage DAG (`ingest → embed → detect → people_register →
  scope_prefilter → rerank → scope_classify → pii_classify → redact →
  redact_verify → export`).
- `dsar-conductor` CLI with `--check`, `--force`, `--from`, `--only`,
  `--acknowledge-issues`.
- v4 adapter layer: 10 adapters under `src/dsar_orchestrator/adapters/`
  with single-injectable-dependency contract and per-adapter retirement
  triggers. See `docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v4.md`.
- Resume cascade via `upstream_hash` chain on every artefact row.
- Module-agent validation framework (`src/dsar_orchestrator/module_agents.py`).
- Log analyser with critical-finding block flag (`src/dsar_orchestrator/log_analyser/`).
- Synthetic-case generator (`dsar-synthesize-case` CLI).
- Local LLM audit-log reviewer (`dsar-analyse-logs` CLI; mlx-broker-backed).
- 282 passing tests; 9 import-linter contracts.
- Schema and producer-version stamping on every artefact row
  (`SCHEMA_VERSION = "1.0"`, per-module `PRODUCER_VERSION`).

[Unreleased]: https://github.com/harkers/dsar-orchestrator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/harkers/dsar-orchestrator/releases/tag/v0.1.0
