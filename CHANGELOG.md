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

### Added
- `VERSIONING.md` documenting the package/schema/producer version policy.
- `CHANGELOG.md` (this file).
- `docs/superpowers/brainstorms/2026-05-24-v5-paused-notes.md` capturing the in-flight v5 pipeline-orchestration brainstorm.

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
