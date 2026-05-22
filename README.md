# dsar-orchestrator

The pipeline orchestrator for the [dsar-toolkit](https://github.com/harkers/dsar-toolkit)
modular DSAR processing stack. Owns the cross-module sequencing,
parallelism, resume semantics, and rate-limit governance that
chain the toolkit's modules into a working case run.

## Why a separate repo

`dsar-toolkit` is a library of DSAR processing capabilities —
ingest, embed, rerank, search, detect, redact, export, plus the
new modular phases (`dsar_embed`, `dsar_rerank`, `dsar_search`,
`dsar_pii_classifier`, `dsar_pii_discovery`, `dsar_redact_verify`).
Each module is independently runnable via its own CLI.

`dsar-orchestrator` is the *conductor*. It depends on the toolkit
as a Python package + chains its modules per the orchestration
spec (data dependencies, parallel branches, the
synchronisation barrier at the LLM scope-classify gate, the
shadow/enforce mode handling, the upstream_hash chain for
surgical re-runs, the halt-and-flag handling for disputed PII
verdicts).

Split because:

- **Surgical replaceability.** The orchestrator's policy choices
  (parallelism strategy, semaphore budgets, halt thresholds) are
  separable from the modules' implementation. Either side can
  iterate without touching the other.
- **Independent versioning.** A new orchestrator release doesn't
  bump every toolkit module's version.
- **Per-deployment customisation.** Different operators or
  different engagements can run different orchestrator policies
  against the same toolkit version.
- **Architectural consistency.** Mirrors the same extraction
  pattern as [`harkers/zen-tei`](https://github.com/harkers/zen-tei)
  (TEI deployment) and [`harkers/mlx-broker`](https://github.com/harkers/mlx-broker)
  (MLX LLM gateway) — each is a thin runtime that depends on
  upstream-managed components.

## What lives here

| Path | Purpose |
|---|---|
| `docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v<N>.md` | Authoritative orchestration design. Versioned per the iterated-spec convention. |
| `src/dsar_orchestrator/pipeline.py` | The actual `pipeline.run(case, …)` function. |
| `src/dsar_orchestrator/hash_chain.py` | `upstream_hash` compute + verify utilities. Shared by all artefact reads/writes the orchestrator drives. |
| `src/dsar_orchestrator/cli.py` | `dsar-pipeline` operator CLI: `--case`, `--from`, `--through`, `--only`, `--check`, `--dry-run`. |
| `src/dsar_orchestrator/audit.py` | Writes `pipeline.jsonl` per-case audit log (stage banners, durations, outcomes). |
| `tests/` | Orchestrator unit + integration tests. |
| `docs/audit_schemas/pipeline.schema.json` | Schema for the per-case pipeline audit log. |

## What does NOT live here

- The toolkit modules themselves (`dsar_embed`, `dsar_rerank`, …)
  — those live in `harkers/dsar-toolkit`.
- The LLM router + semaphore — that's a toolkit concern; modules
  (e.g., `dsar_pii_classifier`) call it directly without the
  orchestrator's involvement.
- The audit-schema validator — toolkit-side; orchestrator just
  emits rows that conform.
- The case-config schema — toolkit-side.

## Dependency direction

Strictly one-way: `dsar-orchestrator` imports from `dsar-toolkit`;
`dsar-toolkit` never imports from `dsar-orchestrator`. The
orchestrator is the consumer.

## Install

(Until the first release lands — projected once the orchestration
spec v2 is implemented:)

```bash
pip install -e ~/projects/dsar-toolkit       # the modules
pip install -e ~/projects/dsar-orchestrator  # the conductor
```

After install, `dsar-pipeline --case <no>` is the operator's entry
point. Module-level CLIs (`dsar-embed`, `dsar-rerank`, …) come from
the toolkit install and remain available for surgical re-runs.

## Status

**Pre-implementation as of 2026-05-22.** The orchestration design
specs (v1 + v2 WIP) are landed; the actual Python code lives in
`src/dsar_orchestrator/` and is the next implementation
workstream after `dsar-toolkit` Phase 1 ships.

## See also

- [`dsar-toolkit`](https://github.com/harkers/dsar-toolkit) — the
  modules being orchestrated. Integration spec:
  `dsar-toolkit/docs/superpowers/specs/2026-05-22-zen-tei-integration-design-v4.md`.
- [`zen-tei`](https://github.com/harkers/zen-tei) — the TEI
  deployment that Phase 1 + 2 + 3 depend on.
