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
| `src/dsar_orchestrator/cli.py` | `dsar-conductor` operator CLI: `--case`, `--from`, `--through`, `--only`, `--check`, `--dry-run`. |
| `src/dsar_orchestrator/audit.py` | Writes `pipeline.jsonl` per-case audit log (stage banners, durations, outcomes). |
| `src/dsar_orchestrator/operator_console.py` | Localhost-only operator console (`http.server`): per-case decision pages plus the `/live-log` live-event feed. |
| `src/dsar_orchestrator/local_broker/live_log_stream.py` | Live-log SSE streaming: tails the L1/L2/L3 JSONL sources through a single merged iterator with composite-cursor resume. |
| `src/dsar_orchestrator/local_broker/live_log_projection.py` | Fail-closed per-event-type field allowlist + bounded-enum scrubber — the PII boundary for the live feed. |
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

## Operator console & live-log feed

`operator_console.py` serves a localhost-only, single-case UI bound to
`127.0.0.1`. Alongside the decision pages (blockers, leak review, flag
review, people register, …) it exposes a Plex-style **live-log feed** at
`/live-log`:

- Tails three layers — `working/audit_events.jsonl` (L1), the per-stage
  decision/finding jsonls behind a verbose toggle (L2), and the
  `~/.dsar-audit/<case_no>/pipeline.jsonl` that `PipelineAuditor` already
  writes (L3). No new producer code — the existing audit log *is* the L3
  source.
- Streams to the browser over Server-Sent Events (`/live-log/stream`).
  Every frame passes a **fail-closed** per-event-type field allowlist +
  bounded-enum value scrubber, so no raw PII reaches the browser; the one
  free-text L3 surface (`note().message`) is dropped at the projection
  boundary.
- A composite `Last-Event-ID` cursor (byte offsets across all sources)
  drives auto-reconnect with identity-validate-before-seek (rotation /
  truncation detected and surfaced as gap markers), a 16 MiB resume
  backlog cap, and a 15 s heartbeat that itself carries the cursor.

Design: `docs/superpowers/specs/2026-05-29-operator-console-live-log-design-v2.md`.

## Install

(Until the first release lands — projected once the orchestration
spec v2 is implemented:)

```bash
pip install -e ~/projects/dsar-toolkit       # the modules
pip install -e ~/projects/dsar-orchestrator  # the conductor
```

After install, `dsar-conductor --case <no>` is the operator's entry
point. Module-level CLIs (`dsar-embed`, `dsar-rerank`, …) come from
the toolkit install and remain available for surgical re-runs.

## Status

**Implemented.** The orchestration pipeline, the `dsar-conductor`
CLI, and the operator console (including the `/live-log` feed) are
landed in `src/dsar_orchestrator/` with a green unit + integration
test suite. The versioned design specs under `docs/superpowers/specs/`
remain the authoritative reference and continue to iterate ahead of
the code.

## See also

- [`dsar-toolkit`](https://github.com/harkers/dsar-toolkit) — the
  modules being orchestrated. Integration spec:
  `dsar-toolkit/docs/superpowers/specs/2026-05-22-zen-tei-integration-design-v4.md`.
- [`zen-tei`](https://github.com/harkers/zen-tei) — the TEI
  deployment that Phase 1 + 2 + 3 depend on.
