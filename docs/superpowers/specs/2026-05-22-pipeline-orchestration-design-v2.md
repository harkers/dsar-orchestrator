# dsar-toolkit pipeline orchestration — design (v2)

**Status:** v2 — 2026-05-22, extended to cover all seven phases from
[`2026-05-22-zen-tei-integration-design-v4.md`](2026-05-22-zen-tei-integration-design-v4.md).
Adds Phase 4 (LLM PII classifier) and Phase 5/6 (Paths A + B) to the
DAG, the optimal flow, the hash chain, and the failure-handling
table. Includes the full `pipeline.run()` pseudocode as the
authoritative reference for the orchestrator extension.

**Relationship to other specs.**
[`2026-05-22-zen-tei-integration-design-v4.md`](2026-05-22-zen-tei-integration-design-v4.md)
describes what each new module *is* (7 phases). This document
describes how the orchestrator chains them — and the existing
4-stage pipeline — into a working case run. The two specs are
siblings, cross-linked, versioned independently.

## Version history

| Version | Date | Commit | Summary |
|---|---|---|---|
| v1 | 2026-05-22 | `0258f76` | Initial draft. Defines the data-dependency DAG; identifies parallel branches; specifies the orchestrator + per-module CLI dual-entry model; locks resume semantics via per-artefact `upstream_hash` fields; describes the LLM-rate-limit semaphore at the scope-classify gate. Covered Phases 1–3 from integration spec v3. |
| v2 | 2026-05-22 | this commit | Extends to cover all seven phases from integration spec v4. New DAG branches: `dsar_pii_discovery` (Ph5) parallel with detect-2.1-2.4; `dsar_pii_classifier` (Ph4) between scope-classify and redact, with shadow/enforce mode handling + disputed-doc halt-and-flag; `dsar_redact_verify` (Ph6) between redact and export as a halt-on-fail gate. Hash chain extended with `pii_collection.jsonl`, `pii_discovery.jsonl`, `redact_verify.jsonl`. Semaphore extended to gate Haiku 4.5 calls alongside Sonnet 4.6. New sections: "Per-phase integration table", "Full pipeline.run() pseudocode", "Disagreement + halting". Phase 7 (regression harness) deliberately excluded — CI infrastructure, not pipeline runtime. |

---

## Goal

Define the canonical execution order for a single DSAR case
through the dsar-toolkit pipeline, including:

- Where the new modules (`dsar_embed`, `dsar_rerank`, `dsar_search`)
  slot into the existing 4-stage flow (ingest → detect → redact →
  export).
- Which steps can run in parallel and where the synchronisation
  barriers are.
- How surgical CLI re-runs (`dsar-embed --case X`) interact with
  full-pipeline runs (`dsar-pipeline --case X`).
- How resume-from-partial-failure works without explicit operator
  intervention.
- How rate-limited external APIs (Anthropic Claude at the LLM
  scope-classify gate) are governed across concurrent cases.

## Non-goals

- Re-architect the existing 4-stage `dsar_pipeline.pipeline`
  module. The orchestrator gets extended; it doesn't get rewritten.
- Specify what each module does internally. That's the module's
  own README + the zen-tei-integration design.
- Make orchestration decisions that are really per-module choices
  (e.g., embedding batch size lives in `dsar_embed/core.py`, not
  here).

---

## Current state, briefly

Today (pre-Phase-1) the pipeline is a hand-rolled sequential chain
in `src/dsar_pipeline/pipeline.py`. Stages are called in order:

```
ingest → detect (2.1-2.4 + 2.5 LLM) → redact → export
```

Embedding happens inside ingest as a side-effect (a call into
`embed_corpus()` which today hits Ollama on `:11434` and fails).
Scope pre-filter runs inside detect as a first step (cosine ≥ 0.30
gate before the LLM scope-classify).

The existing chain is serial. There's no parallelism. There's no
resume-from-partial-failure: a crash halfway through forces a
re-run from ingest unless the operator manually inspects the
`working/` state and edits the script.

---

## The data dependencies (the constraints)

Each artefact depends on the upstream artefacts that produced it.
The dependency graph is not a free choice — it's determined by
what each step actually reads:

```
                                source/ + register.json
                                            │
                ┌───────────────────────────┼───────────────────────────┐
                │                            │                            │
                ▼                            ▼                            ▼
        embeddings.jsonl          raw entity tags (spaCy/        pii_discovery.jsonl
        (Phase 1)                  regex/CloakLLM —              (Phase 5 / Path A —
                                   detect-2.1-2.4)               scrubadub + GLiNER
                                                                  + rapidfuzz)
                │                            │                            │
        ┌───────┼───────┐                    │                            │
        ▼       ▼                            │                            │
   person_  cosine_prefilter                 │                            │
   index    .jsonl                           │                            │
            │                                │                            │
            ▼                                │                            │
        scope_rerank.jsonl                   │                            │
        (Phase 2 — shadow                    │                            │
         pass-through or                     │                            │
         enforce drop set)                   │                            │
                │                            │                            │
                └────────────┬───────────────┴────────────────────────────┘
                             ▼
                  ┌──── SYNC BARRIER ────┐
                  ▼                       │
        detect-2.5 scope-classify        │
        (Sonnet 4.6 — LLM-                │
         semaphore-gated)                 │
                  │                       │
                  ▼                       │
        in-scope verdict per doc          │
                  │                       │
                  ▼                       │
        pii_collection.jsonl              │
        (Phase 4 — Haiku 4.5,             │
         shadow-mode log-only             │
         OR enforce-mode redact          │
         source) + scope_recheck.jsonl    │
         (disputed docs halted)           │
                  │                       │
                  ▼                       │
        redact (existing Stage 3,         │
        with entity-source preference     │
        from Phase 4 mode)                │
                  │                       │
                  ▼                       │
        redact_verify.jsonl               │
        (Phase 6 / Path B —               │
         pikepdf + pytesseract +          │
         difflib; halts pipeline          │
         on any verifier failure)         │
                  │                       │
                  ▼                       │
                output/  (Stage 4)        │
                                          │
        person_index ────────► assembled into final report alongside output/
```

Linear chain through the middle (must be serial):
`source → embed → cosine → rerank → LLM scope-classify → pii-classify →
redact → redact-verify → export`.

Parallel branches at each fan-out point:
- **Stage 2 (post-ingest):** `embed` (Ph1), `detect-2.1-2.4`, and
  `dsar_pii_discovery` (Ph5) all depend only on raw text →
  three-way parallel.
- **Stage 3 (post-embed):** `people_register` and the chain
  `scope_prefilter → dsar_rerank` (Ph2) both depend only on
  `embeddings.jsonl` → parallel.
- `dsar-search` (Ph3) is OOB; can run at any time after
  embeddings exist.

The **synchronisation barrier** is the LLM scope-classify step:
it needs the reranker output (or cosine-passing set in Ph2 shadow
mode), the raw entity tags from detect-2.1-2.4, AND the
pii_discovery output (in Ph5 union mode). Nothing downstream of
that barrier can start until all three arrive.

The **PII classifier (Phase 4)** is the second LLM stage. It runs
*after* scope-classify, only on docs that survived. It's also
gated by the global LLM concurrency semaphore (shared with
scope-classify — see "Rate-limit handling" below). In **enforce**
mode its output is the source of truth for `redact`'s entity set;
in **shadow** mode `redact` ignores it and uses spaCy's entities
as today.

The **redact-verify (Phase 6)** is the only post-redact pre-export
stage. Any verifier failure halts the pipeline — the case stays
in `working/`, never reaches `output/`.

---

## The optimal flow

For a single case, per the dependency graph:

```
Stage 1.  ingest                                            (serial)
                ↓
Stage 2.  parallel: { dsar-embed  ;  detect-2.1-2.4 }       (parallel; join)
                ↓
Stage 3.  parallel: { people_register
                    ; scope_prefilter → dsar-rerank }       (parallel; join)
                ↓
Stage 4.  detect-2.5 LLM scope-classify
          (gated by the global LLM concurrency semaphore)   (serial; rate-gated)
                ↓
Stage 5.  redact                                            (serial)
                ↓
Stage 6.  export                                            (serial)
```

"Optimal" here means: stages 2 and 3 are the only legitimate
parallelism wins. Inside Stage 2, embedding takes minutes (compute-
bound on the M5 Max GPU via TEI) and detect-2.1-2.4 takes minutes
(spaCy + regex CPU-bound + occasional CloakLLM HTTP). Running them
concurrently roughly halves the pre-LLM wall-clock. Inside Stage 3,
people_register's numpy-vectorised cosine matrix is GPU-light and
finishes in seconds; scope_prefilter + rerank also seconds; making
them parallel is cheap insurance.

Stages 1, 4, 5, 6 must be serial — Stage 4 because it's the
sync barrier, Stages 1+5+6 because they're inherently sequential
file ops on the case bundle.

### Across cases (batch processing)

Each case is independent at the filesystem level. Multiple cases
can run their full pipelines concurrently. The only shared
resource is the **Anthropic Claude API** hit at Stage 4 (LLM
scope-classify). See "Rate-limit handling" below.

---

## Orchestration model

Two callers, **one implementation per module**:

| Caller | Pattern | When |
|---|---|---|
| Orchestrator (`dsar_pipeline.pipeline.run(case, …)`) | In-process call: `dsar_embed.core.embed_corpus(case)`, `dsar_rerank.core.rerank_case(case)`, etc. | Full-pipeline runs. No subprocess overhead, shared logger context, in-memory state reuse where useful. |
| Module CLI (`dsar-embed`, `dsar-rerank`, `dsar-search`, …) | Subprocess: thin argparse wrapper, builds context, calls the same `core.<fn>()` underneath | Surgical operator re-runs against existing case state. |

This is the **non-negotiable invariant**: there is exactly one
implementation per capability. The CLI wraps `core.<fn>()`; it
does not parallel-implement anything. Drift between the
orchestrator's behaviour and the CLI's behaviour cannot happen by
construction.

### `dsar-pipeline` orchestrator CLI

The orchestrator gets its own CLI entry point (currently
`dsar-pipeline` exists as the existing module's entry; the v1
spec extends it):

```bash
dsar-pipeline --case 301770                       # full run; resumes from current state
dsar-pipeline --case 301770 --from ingest         # force-restart from ingest
dsar-pipeline --case 301770 --through detect      # stop after Stage 4; skip redact + export
dsar-pipeline --case 301770 --only rerank         # alias for `dsar-rerank --case 301770`
dsar-pipeline --case 301770 --dry-run             # print the planned stages + skipped stages
dsar-pipeline --case 301770 --mode shadow         # passes RERANK_MODE=shadow to dsar_rerank
```

`--only <stage>` is sugar for the matching module CLI; it exists
so operators can use one mental model (`dsar-pipeline --only X`)
rather than remembering each module's CLI name. Both forms work;
both call into the same `core.<fn>(case)`.

### What the orchestrator must NOT do

- Subprocess each module's CLI. The orchestrator imports + calls
  `core.<fn>()` in-process. Subprocess overhead per stage costs
  hundreds of milliseconds; that's noise per stage but adds up
  across cases.
- Re-implement parallelism inside any single module's stage. If
  embedding wants to batch into 32-doc chunks for GPU throughput,
  that's `dsar_embed`'s concern — the orchestrator only sees
  "embed this case" as one atomic operation.
- Skip stages it doesn't recognise. If `dsar-pipeline` is run on
  a repo that has `dsar_rerank` installed but the case predates
  it (no `scope_rerank.jsonl` in `working/`), the orchestrator
  detects this and runs the missing stage — never silently skips.

---

## Resume semantics — `upstream_hash` everywhere

The trickier question. Surgical re-runs only work if downstream
stages can detect upstream changes. The convention:

**Every artefact carries an `upstream_hash` field — the SHA-256
of the upstream inputs that produced it.** On read, the
downstream module verifies the hash matches the current upstream.
Mismatch → fail loudly with a clear instruction:

> Upstream artefact has changed since `<path>` was written.
> Re-run `dsar-<X> --case <Y> --if-exists overwrite` or
> `dsar-pipeline --case <Y> --from <stage>` to refresh.

### Concrete hash chain

| Artefact | `upstream_hash` covers |
|---|---|
| `working/embeddings.jsonl` | sorted hash of `(ref, sha256(raw_text))` for every ref in `register.json` |
| `working/cosine_prefilter.jsonl` | hash of `embeddings.jsonl` + the case-context vector + the threshold |
| `working/scope_rerank.jsonl` | hash of `cosine_prefilter.jsonl` + the case scope statement + the reranker model_revision |
| `working/<ref>_tags.json` | hash of the cosine-passing set (or reranker-kept set, in enforce mode) + raw text per ref |
| `redacted/<ref>.*` | hash of `<ref>_tags.json` + per-doc raw bytes |
| `output/<ref>.pdf` | hash of `redacted/<ref>.*` |

### Why upstream_hash, not mtime

mtime is a footgun: it changes on `touch`, on cp-preserves-time,
on filesystem migration, etc. SHA-256 of the actual upstream
content is robust. Cost: one extra SHA pass per artefact write —
trivial compared to the rest of the work.

### Resume cascade behaviour

`dsar-pipeline --case X` with all artefacts present + all hashes
matching → no-op, exit 0 with "case complete; nothing to do".

`dsar-embed --case X --if-exists overwrite` rewrites
`embeddings.jsonl` with a fresh `upstream_hash`. The next
`dsar-pipeline --case X` run detects that `cosine_prefilter.jsonl`'s
recorded upstream hash no longer matches the current
`embeddings.jsonl`'s hash → re-runs from `cosine_prefilter`
onward. The operator doesn't need to think about which stages
to invalidate; the hash chain does it.

### The `--check` mode

`dsar-pipeline --case X --check` prints the resume plan without
running anything:

```
Case 301770 resume plan:
  ✓ ingest                  (artefacts present, hashes ok)
  ✓ dsar-embed              (artefacts present, hashes ok)
  ✓ detect-2.1-2.4          (artefacts present, hashes ok)
  ✓ people_register         (artefacts present, hashes ok)
  ✗ scope_prefilter         (MISSING)  → will run
  ✗ dsar-rerank             (BLOCKED on scope_prefilter)
  ✗ detect-2.5 LLM          (BLOCKED on dsar-rerank)
  …
```

This makes resume behaviour transparent before the operator
commits to a (potentially long) run.

---

## Rate-limit handling at the LLM gate

Anthropic Claude is the only external rate-limited resource the
pipeline touches. The scope-classify step (detect-2.5) calls
Claude once per cosine-passing (or reranker-kept) document. For a
typical case that's hundreds to thousands of calls.

For multiple cases running concurrently, naive parallelism
multiplies the call rate and trips the per-minute or per-day token
limits.

**Locked decision:** a **single global semaphore** gates concurrent
LLM scope-classify calls. Lives in `src/dsar_pipeline/llm_router.py`
(the existing module). Default concurrency 5; env var
`DSAR_LLM_CONCURRENCY` overrides. The semaphore is process-local
(per-`dsar-pipeline` invocation); operators running multiple
parallel `dsar-pipeline` shells on the same machine are
responsible for their own arithmetic.

Why not a true distributed rate-limiter (e.g., Redis-backed)?
Single operator, single workstation. YAGNI.

The reranker (Phase 2 enforce mode) reduces the number of calls
that hit the semaphore but doesn't replace it. Both safety nets
are independent.

---

## Where `dsar-search` fits

`dsar-search` is not in the pipeline DAG. It's a read-only OOB
tool that consumes `working/embeddings.jsonl` (Phase 1's output)
to answer ad-hoc queries.

Constraint: `dsar-search` must verify the `embeddings.jsonl`
upstream hash against the current `register.json` before serving
results. If a re-ingest has happened mid-flight and embeddings.jsonl
is stale, `dsar-search` refuses rather than returning misleading
hits. Same hash-chain discipline as the orchestrator.

Operator can run `dsar-search` at any time AFTER Stage 2
completes. It doesn't block any subsequent pipeline stage.

---

## Failure handling

The orchestrator catches the typed exceptions defined in
`dsar_clients` (`TEIUnreachable`, `TEIDeadlineExceeded`,
`TEIBadResponse`) plus the existing pipeline exceptions, and:

1. **TEI-flavoured failures** during embed or rerank → emit a
   clear one-liner naming the case, the stage, the unreachable
   endpoint. Exit non-zero. Operator can re-run `dsar-pipeline`
   once TEI is back; the resume cascade picks up where it left
   off.
2. **LLM rate-limit hit** (Anthropic 429) at Stage 4 → retry
   with backoff inside `llm_router.py` (existing behaviour);
   surface only after retries are exhausted.
3. **Validation failure** (`upstream_hash` mismatch) → exit
   non-zero with the exact instruction to repair (e.g.,
   "re-run `dsar-embed --case X --if-exists overwrite`").

The orchestrator never silently degrades. DSAR has legal
deadlines; a silent slowdown could push a case past the 30-day
statutory window without operator notice (per zen-tei-integration
spec § Cross-cutting → Failover).

---

## CLI ergonomic shortcuts

The orchestrator should print a status banner at start + end of
each stage with case + stage + duration + outcome:

```
[2026-06-04T14:23:01+01:00] case=301770 stage=ingest         start
[2026-06-04T14:24:47+01:00] case=301770 stage=ingest         done in 106s (refs: 4218)
[2026-06-04T14:24:47+01:00] case=301770 stage=dsar-embed     start  (parallel with detect-2.1-2.4)
[2026-06-04T14:24:47+01:00] case=301770 stage=detect-2.1-2.4 start  (parallel with dsar-embed)
[2026-06-04T14:31:12+01:00] case=301770 stage=dsar-embed     done in 385s
[2026-06-04T14:33:09+01:00] case=301770 stage=detect-2.1-2.4 done in 502s
…
```

Same lines are appended to `~/.dsar-audit/<case>/pipeline.jsonl`
in structured form (one JSON line per stage transition). This is
the audit-trail companion to `llm_calls.jsonl` and
`scope_rerank.jsonl`.

---

## What this spec touches in the codebase

This is an *orchestration* design, not an implementation spec.
The concrete file-level changes belong in the eventual writing-
plans output. But for orientation:

```
MOD   src/dsar_pipeline/pipeline.py
    - extends the existing chain to import + call
      dsar_embed / dsar_rerank in-process
    - adds parallel branches for Stages 2 + 3 (concurrent.futures
      or asyncio — picked at plan time)
    - adds the `--from / --through / --only / --check` CLI flags
    - adds the upstream_hash verification chain
    - adds the stage-banner logging + pipeline.jsonl audit
NEW   src/dsar_pipeline/hash_chain.py
    - `compute_upstream_hash(stage, case) -> str`
    - `verify_upstream_hash(artefact_path, expected) -> None`
    - shared by all modules' core.<fn>() functions
MOD   src/dsar_pipeline/llm_router.py
    - adds the DSAR_LLM_CONCURRENCY semaphore at scope-classify
NEW   docs/audit_schemas/pipeline.schema.json
    - shape of pipeline.jsonl rows (stage banners)
MOD   src/dsar_pipeline/audit.py
    - registers the new schema
```

No changes to the modules' own `core.py` from this spec — those
were specified in the zen-tei-integration design. The hash_chain
helper is the one new shared utility; every module's core
function calls `hash_chain.compute_upstream_hash()` and
`verify_upstream_hash()` at appropriate boundaries.

---

## Cross-cutting consistency with the integration spec

This spec composes with
[`2026-05-22-zen-tei-integration-design-v3.md`](2026-05-22-zen-tei-integration-design-v3.md):

| Concern | Where it lives |
|---|---|
| Module shape (`__init__`, `cli`, `core`, …) | integration spec v3 |
| Dependency rules (CI-enforced) | integration spec v3 + Appendix B |
| HTTP robustness (timeouts/retries/deadlines) | integration spec v3 § Operational semantics |
| Idempotency primitive (`--if-exists`, atomic writes) | integration spec v3 § CLI contract |
| Schema/producer versioning | integration spec v3 § Schema and artifact versioning |
| **Stage ordering + parallelism** | **this spec** |
| **Resume / upstream_hash chain** | **this spec** |
| **Orchestrator vs module-CLI contract** | **this spec** |
| **LLM-call concurrency semaphore** | **this spec** |

Together they answer "how is Phase N built?" (integration spec) +
"how do all phases run together?" (this spec). Future phases (4+)
should slot into both specs' patterns without further design.

---

## Open questions (already answered with defaults)

- **Parallelism mechanism?** `concurrent.futures.ThreadPoolExecutor`
  (synchronous, easy to reason about). asyncio considered + rejected
  for now — the existing pipeline is sync Python; introducing
  async would force a bigger refactor than this spec wants. If a
  later phase benefits from async (e.g., streaming LLM responses),
  reconsider.
- **Stage failure isolation?** A failure in one parallel branch
  cancels the other branch (no point continuing the people_register
  cluster if rerank just crashed) and surfaces both errors. The
  ThreadPoolExecutor's `wait(return_when=FIRST_EXCEPTION)` shape
  handles this.
- **Resume granularity?** Stage-level. We don't try to resume
  partway through `dsar-embed` (e.g., 50% through the corpus). The
  module itself can checkpoint internally (Phase 1's
  responsibility) but the orchestrator sees the module as atomic.
- **`--check` against a case the operator doesn't have access to?**
  Read-only operation; fails cleanly with "cannot read
  `~/dsars/cases/<no>/`" if the sparse bundle is dismounted.

---

## Companion artefacts

- Implementation prompt for the orchestration extensions: filed
  after Phase 1 + Phase 2 have shipped and the integration points
  exist (premature otherwise).
- The integration spec's `phase-1-plan.md` and `phase-2-plan.md`
  (when written) reference this spec for their orchestration
  hook-up details. Each plan implements the orchestrator-side
  changes for the module it's introducing.
