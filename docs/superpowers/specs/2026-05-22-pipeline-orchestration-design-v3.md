# dsar-toolkit pipeline orchestration — design (v3)

**Status:** v3 — 2026-05-22. Reflects the discovery that the toolkit
has been building in parallel + has produced its own
``dsar-orchestrator`` CLI with a different model. Captures the rename
of our CLI to ``dsar-conductor`` and the architectural positioning of
this conductor as a layer ABOVE the toolkit's state machine.

**Relationship to other specs.**
[`2026-05-22-zen-tei-integration-design-v4.md`](2026-05-22-zen-tei-integration-design-v4.md)
in the toolkit repo describes what each module *is*. This document
describes how the conductor (this repo) chains the toolkit's modules
into a working case run. The two specs are siblings, cross-linked,
versioned independently. The toolkit also ships its own internal
state-machine orchestrator (``dsar_pipeline.orchestrator``); this
spec documents how the conductor coexists with it (see "Toolkit
divergence" below).

## Version history

| Version | Date | Commit | Summary |
|---|---|---|---|
| v1 | 2026-05-22 | `0258f76` | Initial draft. Defines the data-dependency DAG; identifies parallel branches; specifies the orchestrator + per-module CLI dual-entry model; locks resume semantics via per-artefact `upstream_hash` fields; describes the LLM-rate-limit semaphore at the scope-classify gate. Covered Phases 1–3 from integration spec v3. |
| v2 | 2026-05-22 | `0b8a2e8` | Extends to cover all seven phases from integration spec v4. New DAG branches: `dsar_pii_discovery` (Ph5) parallel with detect-2.1-2.4; `dsar_pii_classifier` (Ph4) between scope-classify and redact, with shadow/enforce mode handling + disputed-doc halt-and-flag; `dsar_redact_verify` (Ph6) between redact and export as a halt-on-fail gate. Hash chain extended with `pii_collection.jsonl`, `pii_discovery.jsonl`, `redact_verify.jsonl`. Semaphore extended to gate Haiku 4.5 calls alongside Sonnet 4.6. New sections: "Per-phase integration table", "Full pipeline.run() pseudocode", "Disagreement + halting". Phase 7 (regression harness) deliberately excluded — CI infrastructure, not pipeline runtime. |
| v3 | 2026-05-22 | this commit | Toolkit-divergence sync. The toolkit team has shipped 17 phases of work in parallel, including their own 14-stage state-machine orchestrator (``dsar_pipeline.orchestrator``) and a `dsar-orchestrator` CLI. Three reconciliations land here: (1) **CLI rename** — our operator-facing command becomes ``dsar-conductor`` to avoid the name clash. (2) **Architectural positioning** — we sit ABOVE the toolkit's state machine, owning resume cascade + log analyser + module-agent validation as cross-cutting concerns; the toolkit owns intra-pipeline state. (3) **Toolkit-module API drift** — the lazy-import names in our `pipeline.py` ( e.g., `dsar_pipeline.detect.run_2_1_to_2_4`) don't match the toolkit's actual shapes (they ship per-stage scripts: `pii_identification_stage.main`, `scope_check_stage.main`, etc.). Module-agent validation already brought in-process (v3 of integration spec) so we're independent of the toolkit's `module_agents/` framework, which turns out to be a build-and-test agent harness rather than per-pipeline-stage validators. v4 of this spec will lock the adapter shim that maps our conductor's call shape onto the toolkit's actual CLIs/Python functions. |

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

## Toolkit divergence (added in v3)

While this orchestrator was being built, the toolkit team shipped 17
phases of independent work — including their own pipeline orchestrator
+ 12 new `dsar_pii_*` modules. The divergence requires three explicit
reconciliations.

### 1. Naming conflict — locked rename

The toolkit ships a CLI command **`dsar-orchestrator`** (entry point
``dsar_pipeline.orchestrator:main``). Our operator-facing CLI was
provisionally named ``dsar-pipeline``, which ALSO conflicts with the
toolkit's existing ``dsar-pipeline`` script entry.

**Locked:** our operator-facing CLI is **`dsar-conductor`**. The
Python package stays ``dsar_orchestrator`` (technical identity is
fine; the repo name is also ``harkers/dsar-orchestrator``). The
``dsar-pipeline`` and ``dsar-orchestrator`` script names both stay
owned by the toolkit.

```
toolkit ships:                     this repo ships:
  dsar-pipeline                      dsar-conductor
  dsar-orchestrator                  dsar-analyse-logs
  dsar-pii-classify, ...             dsar-synthesize-case
```

### 2. Architectural positioning — conductor above state machine

The toolkit's orchestrator (``dsar_pipeline.orchestrator``) is a
**14-stage state machine** with gates like ``scope_check_running``,
``redaction_qc_a``, ``awaiting_operator_review``. It owns intra-
pipeline state — what stage a case is in, what operator gates are
pending, where the state.json sits.

Our conductor sits **above** this state machine. We own cross-cutting
concerns the toolkit's orchestrator doesn't (and shouldn't):

| Concern | Owner |
|---|---|
| 14-stage intra-pipeline state machine | toolkit `dsar_pipeline.orchestrator` |
| Operator gates / awaiting-review semantics | toolkit |
| Per-agent dispatch within a stage | toolkit |
| Resume cascade via upstream_hash chain | this conductor |
| Module-agent validation per sub-stage | this conductor (in-process; see § Per-module agent validation) |
| Log analyser → critical-finding block flag | this conductor |
| Synthetic-case generator | this conductor |
| Cross-stage parallelism (Stage 2 + 3 fan-outs) | this conductor |
| Audit log: `pipeline.jsonl` | this conductor |
| Audit log: `llm_calls.jsonl`, `state.json` | toolkit |

The conductor's ``pipeline.run(case)`` will invoke the toolkit's
orchestrator (or its per-stage scripts) as the unit of work for each
of our 8 coarse stages; we don't replace it.

### 3. Toolkit-module API shapes

The toolkit's `dsar_pipeline.detect` exposes a single ``detect()``
function with a different signature than our `pipeline._run_<X>`
helpers expected. The toolkit's per-stage work happens in per-stage
scripts:

| What our spec assumed | What the toolkit actually ships |
|---|---|
| `dsar_pipeline.detect.run_2_1_to_2_4(case_path)` | `dsar_pipeline.pii_identification_stage:main` (a CLI entry; or via the state-machine orchestrator) |
| `dsar_pipeline.detect.run_scope_prefilter(case_path)` | `dsar_pipeline.scope_check_stage:main` |
| `dsar_pipeline.detect.run_scope_classify(case_path)` | `dsar_pipeline.context_classify.*` |
| `dsar_pipeline.detect.run_people_register(case_path)` | not yet a standalone; lives inside the detect/classifier flow |
| `dsar_pipeline.redact.run(case_path, …)` | `dsar_pipeline.redact_stage:main` |
| `dsar_pipeline.export.run(case_path)` | `dsar_pipeline.bake_stage:main` + `dsar_pipeline.post_bake_verify_stage:main` |
| `dsar_embed.core.embed_corpus(case_path)` | NOT YET SHIPPED — toolkit's `embed.py` still uses Ollama |
| `dsar_rerank.core.rerank_case(case_path, …)` | NOT a standalone module; rerank lives inside `dsar_pii_classifier` + via `dsar_clients.tei_rerank_client` |

**Implication for the conductor:** every `_run_<X>` helper in
`pipeline.py` needs an adapter that translates our coarse call into
the toolkit's actual interface — typically a subprocess call to the
toolkit's per-stage CLI or a Python call into the toolkit's state
machine.

That adapter is **v4 work**. v3 locks the divergence + the rename.
Until v4 lands, the conductor's `pipeline.run()` against the real
toolkit will fail at the first `_lazy_import`; the toolkit-stub
tests pass because the stubs implement our expected API.

### What we keep from the original design

Everything below the integration-shim layer survives:

- The 8 coarse stages + ThreadPoolExecutor parallelism on Stages 2 + 3
- The resume cascade via `upstream_hash`
- The PipelineHalt semantics for Phase 6 verifier failures
- The disputed-doc halt-and-flag behaviour for Phase 4
- The module-agent validation contract (already in-process)
- The log analyser block-flag gate
- The synthetic-case generator

These are conductor-side concerns; they don't depend on the toolkit's
shape, just on the toolkit producing valid artefacts when its stages
finish.

### What the toolkit's `module_agents/` is NOT

Our v2 spec assumed `dsar_pipeline.module_agents.<sub_stage>.check_work(case_path)`
would be where per-stage validators lived. The toolkit ships a top-
level `module_agents/` package, but it's a different framework: an
autonomous-build harness with `builder.py`, `judge.py`, `runner.py`
for building dashboard modules (cases-lifecycle, security-middleware,
…). Not pipeline-stage validators.

We brought the per-stage validators in-process to our own
`dsar_orchestrator.module_agents` (covered in integration spec v3
+ this repo's `module_agents.py`). That decision is now load-bearing:
without it, the conductor would be waiting on a toolkit contract that
isn't going to exist in the form we anticipated.

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

For a single case, per the dependency graph (all 7 toolkit phases):

```
Stage 1.  ingest                                                    (serial)
                ↓
Stage 2.  parallel: { dsar-embed (Ph1)
                    ; detect-2.1-2.4
                    ; dsar-pii-discovery (Ph5/A) }                  (3-way parallel; join)
                ↓
Stage 3.  parallel: { people_register
                    ; scope_prefilter → dsar-rerank (Ph2) }         (parallel; join)
                ↓
Stage 4.  detect-2.5 LLM scope-classify (Sonnet 4.6)
          (gated by global LLM concurrency semaphore)               (serial; rate-gated)
                ↓
Stage 5.  dsar-pii-classify (Ph4 — Haiku 4.5)
          (gated by same semaphore; shadow/enforce mode applies)    (serial; rate-gated)
                ↓
Stage 6.  redact
          (entity-source preference: spaCy in shadow,
           LLM-extracted in enforce)                                (serial)
                ↓
Stage 7.  dsar-redact-verify (Ph6/B)
          (halts pipeline on any verifier failure;
           case stays in working/, not promoted)                    (serial; halt-on-fail)
                ↓
Stage 8.  export                                                    (serial)
```

"Optimal" here means: stages 2 and 3 are the only legitimate
parallelism wins. Inside Stage 2, three branches run concurrently:
- **dsar-embed** (Phase 1) — minutes, GPU-bound via TEI :8085
- **detect-2.1-2.4** — minutes, CPU-bound (spaCy + regex)
- **dsar-pii-discovery** (Phase 5/A) — minutes, mostly CPU-bound
  with the GLiNER engine taking the largest share

Three-way parallelism on Stage 2 cuts the pre-LLM wall-clock to
roughly the longest single branch instead of their sum.

Inside Stage 3, people_register's numpy-vectorised cosine matrix
is GPU-light and finishes in seconds; scope_prefilter + rerank
also seconds; making them parallel is cheap insurance against
either growing.

Stages 1, 4, 5, 6, 7, 8 must be serial:
- **Stage 4** is the sync barrier for the Stage 2 + 3 fans.
- **Stage 5** depends on Stage 4's in-scope verdict.
- **Stages 6-8** are inherently sequential file operations on
  the case bundle (redact reads tags, verify reads redacted,
  export reads verified).

### Across cases (batch processing)

Each case is independent at the filesystem level. Multiple cases
can run their full pipelines concurrently. The only shared
resource is the **Anthropic Claude API** hit at Stage 4 + 5 (LLM
scope-classify and LLM PII-classify). Both flow through the same
`dsar_pipeline.llm_router` semaphore — see "Rate-limit handling"
below.

### Phase enablement

Phases 4, 5, 6 can be disabled per-case via env vars or case
config — but defaults to **all-enabled** (in `shadow` mode for
Ph2 + Ph4 until promoted):

| Env var | Default | Effect when off |
|---|---|---|
| `RERANK_MODE` | `shadow` | `off` = skip Stage 3's rerank branch |
| `PII_CLASSIFY_MODE` | `shadow` | `off` = skip Stage 5 entirely |
| `DISCOVERY_ENABLED` | `true` | `false` = drop Stage 2's pii-discovery branch |
| `REDACT_VERIFY_ENABLED` | `true` | `false` = skip Stage 7 (NOT recommended; only for debug) |

Disabling Stage 7 in production is operationally discouraged —
it's the last line of defence before client-visible output. The
env var exists for debugging when verify is broken; it should be
back to `true` before any case ships.

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

## Full `pipeline.run()` pseudocode

The authoritative reference for what
`dsar_orchestrator.pipeline.run()` does, end-to-end, with all 7
toolkit phases wired in. This is design pseudocode, not the
actual code — but the structure is what implementation should
match.

```python
# src/dsar_orchestrator/pipeline.py
from concurrent.futures import ThreadPoolExecutor, FIRST_EXCEPTION, wait
from dsar_pipeline import ingest, detect, redact, export, llm_router
from dsar_pipeline.audit import PipelineAuditor
from dsar_embed import core as embed_core
from dsar_rerank import core as rerank_core
from dsar_pii_classifier import core as pii_classify_core
from dsar_pii_discovery import core as pii_discovery_core
from dsar_redact_verify import core as redact_verify_core
from dsar_orchestrator.hash_chain import verify_upstream, compute_upstream
from dsar_orchestrator.audit import StageBanner
from dsar_orchestrator.config import load_case_config, validate_phase_4_prereqs


class PipelineHalt(Exception):
    """Raised when redact-verify (Ph6) flags any failure. Case stays
    in working/, never reaches output/."""


def run(
    case: CaseRef,
    *,
    from_stage: str | None = None,
    through_stage: str | None = None,
    only_stage: str | None = None,
    dry_run: bool = False,
    check: bool = False,
) -> RunReport:
    """Orchestrate a full DSAR case run.

    Stages 1-8 per "The optimal flow" section.
    Resume semantics: each stage checks artefact presence + upstream_hash
    before running; skips when fresh.
    Failure semantics: typed exceptions surface to the operator with
    the case-no + stage + recovery instruction. PipelineHalt is the one
    intentional "stop here, manual review" outcome (Ph6 verifier fail).
    """
    cfg = load_case_config(case)
    if cfg.pii_classify_mode != "off":
        validate_phase_4_prereqs(cfg)  # asserts subject_identifier present + well-formed

    plan = build_stage_plan(case, cfg, from_stage, through_stage, only_stage)
    if check or dry_run:
        return plan.print_and_exit()

    audit = PipelineAuditor(case)

    # =========================================================
    # Stage 1 — ingest (serial)
    # =========================================================
    if plan.includes("ingest"):
        with StageBanner(audit, "ingest"):
            ingest.run(case)
            # Output: working/register.json + raw text per ref
            # Hash: SHA-256 of source/ directory tree

    # =========================================================
    # Stage 2 — parallel: embed (Ph1) ∥ detect-2.1-2.4 ∥ pii-discovery (Ph5)
    # =========================================================
    if plan.includes_any("embed", "detect_2_1_to_2_4", "pii_discovery"):
        with StageBanner(audit, "stage_2_parallel"):
            with ThreadPoolExecutor(max_workers=3) as ex:
                futures = {}
                if plan.includes("embed"):
                    verify_upstream(case, "embeddings.jsonl", upstream="register.json")
                    futures["embed"] = ex.submit(embed_core.embed_corpus, case)
                if plan.includes("detect_2_1_to_2_4"):
                    futures["detect_2_1_to_2_4"] = ex.submit(
                        detect.run_2_1_to_2_4, case
                    )
                if plan.includes("pii_discovery") and cfg.discovery_enabled:
                    futures["pii_discovery"] = ex.submit(
                        pii_discovery_core.discover_entities, case
                    )

                done, _ = wait(futures.values(), return_when=FIRST_EXCEPTION)
                # Propagate any exception immediately. If embed fails,
                # detect + discovery futures are cancelled by the
                # exiting context manager.
                for f in done:
                    f.result()  # re-raises if it had an exception

    # =========================================================
    # Stage 3 — parallel: people_register ∥ (scope_prefilter → rerank Ph2)
    # =========================================================
    if plan.includes_any("people_register", "scope_filter_chain"):
        with StageBanner(audit, "stage_3_parallel"):
            with ThreadPoolExecutor(max_workers=2) as ex:
                futures = {}
                if plan.includes("people_register"):
                    futures["people_register"] = ex.submit(
                        detect.run_people_register, case
                    )
                if plan.includes("scope_filter_chain"):
                    futures["scope_filter_chain"] = ex.submit(
                        _scope_filter_chain, case, cfg
                    )

                done, _ = wait(futures.values(), return_when=FIRST_EXCEPTION)
                for f in done:
                    f.result()

    # =========================================================
    # Stage 4 — LLM scope-classify (Sonnet 4.6, semaphore-gated)
    # =========================================================
    if plan.includes("scope_classify"):
        with StageBanner(audit, "scope_classify"):
            detect.run_scope_classify(case)
            # Internally:
            # - Iterates docs in cosine-passing set (shadow) or
            #   reranker-kept set (enforce, post-Ph2 promotion).
            # - For each: llm_router.dispatch(role="scope_classify", ...)
            #   which acquires the DSAR_LLM_CONCURRENCY semaphore.
            # - Writes tags per doc + appends llm_calls.jsonl.

    # =========================================================
    # Stage 5 — LLM PII classifier (Ph4 — Haiku 4.5, semaphore-gated)
    # =========================================================
    if plan.includes("pii_classify") and cfg.pii_classify_mode != "off":
        with StageBanner(audit, "pii_classify"):
            try:
                pii_classify_core.classify_case(
                    case,
                    mode=cfg.pii_classify_mode,  # shadow | enforce
                    subject_identifier=cfg.subject_identifier,
                    budget_usd=cfg.pii_budget_usd,  # default $10
                )
                # Internally per in-scope doc:
                # - llm_router.dispatch(role="pii_classify", ...)
                #   acquires the SAME global semaphore as scope_classify.
                # - Emits pii_collection.jsonl row.
                # - If in_scope_recheck == "disputed":
                #     emits scope_recheck.jsonl row + sets a "halt" flag
                #     for this ref that redact.py will respect.
                # - Always appends to ~/.dsar-audit/training_corpus/
                #   (cross-case; survives bundle deletion).
            except PIIBudgetExceeded as e:
                # Fail loud per CLI contract; operator decides to raise
                # the budget cap or abandon the case.
                audit.note("pii_budget_exceeded", str(e))
                raise

    # =========================================================
    # Stage 6 — redact (entity-source preference per Ph4 mode)
    # =========================================================
    if plan.includes("redact"):
        with StageBanner(audit, "redact"):
            redact.run(
                case,
                prefer_llm_entities=(cfg.pii_classify_mode == "enforce"),
                respect_dispute_halts=True,  # disputed docs from Ph4 skipped here
            )
            # Outputs: redacted/<ref>.* per doc.

    # =========================================================
    # Stage 7 — redact-verify (Ph6/B — halt-on-fail)
    # =========================================================
    if plan.includes("redact_verify") and cfg.redact_verify_enabled:
        with StageBanner(audit, "redact_verify"):
            verdict = redact_verify_core.verify_case(case)
            audit.write_redact_verify_summary(verdict)
            if not verdict.all_passed:
                # Pipeline halts. Case stays in working/, NOT exported.
                # Operator must inspect ~/.dsar-audit/<case>/redact_verify.jsonl
                # and re-run redact (or fix the verifier flag) before export
                # can proceed.
                raise PipelineHalt(
                    f"case={case.no} redact-verify failed: "
                    f"{verdict.failed_doc_count} doc(s) flagged "
                    f"({verdict.failed_verifier_summary}). "
                    f"See ~/.dsar-audit/{case.no}/redact_verify.jsonl. "
                    f"Re-run after fixing: "
                    f"dsar-pipeline --case {case.no} --from redact"
                )

    # =========================================================
    # Stage 8 — export
    # =========================================================
    if plan.includes("export"):
        with StageBanner(audit, "export"):
            export.run(case)
            # Outputs: output/<ref>.pdf per doc + cover sheet.

    return audit.finalise()


def _scope_filter_chain(case, cfg):
    """Stage 3's chained scope_prefilter → dsar_rerank branch.
    Bundled into one function so it runs as a single ThreadPoolExecutor
    future."""
    detect.run_scope_prefilter(case)  # cosine ≥ 0.30 → cosine_prefilter.jsonl
    if cfg.rerank_mode != "off":
        rerank_core.rerank_case(
            case,
            mode=cfg.rerank_mode,       # shadow | enforce
            threshold=cfg.rerank_threshold,  # default 0.01 per integration spec v4
        )
        # Emits scope_rerank.jsonl. In shadow: pass-through (LLM still
        # sees everything cosine kept). In enforce: filters cosine set
        # to top-k or score-cutoff.
```

### Invariants the pseudocode encodes

- **Two-caller invariant.** Every stage calls `<module>.core.<fn>(case)`
  directly. Never subprocess. The module's own CLI (`dsar-embed`,
  `dsar-rerank`, ...) is a wrapper around the same `core.<fn>()`;
  drift between orchestrator + CLI is impossible.
- **Hash-chain invariant.** `verify_upstream` before each downstream
  stage; raises if upstream changed (the operator sees a clear
  re-run instruction, no silent staleness).
- **Audit-emission invariant.** Every stage opens a `StageBanner`
  context manager that writes `pipeline.jsonl` start/end rows with
  duration + outcome. No stage is silent in the audit log.
- **Halt-not-degrade invariant.** Failures raise typed exceptions
  with case-no + stage + recovery instruction. The pipeline never
  silently produces a wrong output. `PipelineHalt` is the only
  intentional "stop here, manual review" exit.
- **Mode-respects-config invariant.** Ph2 + Ph4 modes
  (off/shadow/enforce) come from case config, env var, or
  operator-override file (`~/.dsar-rerank-mode`,
  `~/.dsar-pii-mode`) in that priority order. The orchestrator
  reads the mode once at start and passes it explicitly to each
  module's `core.<fn>()` — never re-reads mid-run.

### What the pseudocode does NOT show

- The actual hash-chain verify implementation (lives in
  `dsar_orchestrator.hash_chain`).
- The PipelineAuditor's atomic-write contract for
  `pipeline.jsonl` (per Cross-cutting § Operational semantics).
- The semaphore implementation inside `llm_router.dispatch()`
  (toolkit-side; sees both Sonnet 4.6 and Haiku 4.5 traffic).
- The case-config schema validation (toolkit-side; defines what
  fields `cfg` carries).

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

### Concrete hash chain (all 7 phases)

| Artefact | `upstream_hash` covers |
|---|---|
| `working/embeddings.jsonl` | sorted hash of `(ref, sha256(raw_text))` for every ref in `register.json` |
| `working/pii_discovery.jsonl` (Ph5) | hash of `register.json` + raw text per ref + GLiNER model_revision |
| `working/cosine_prefilter.jsonl` | hash of `embeddings.jsonl` + the case-context vector + the threshold |
| `working/scope_rerank.jsonl` (Ph2) | hash of `cosine_prefilter.jsonl` + the case scope statement + the reranker model_revision + threshold + mode |
| `working/<ref>_tags.json` | hash of the cosine-passing set (or reranker-kept set, in enforce mode) + pii_discovery output for ref + raw text per ref |
| `working/pii_collection.jsonl` (Ph4) | hash of `<ref>_tags.json` set for in-scope refs + subject_identifier + Haiku 4.5 model_revision + mode |
| `working/scope_recheck.jsonl` (Ph4) | hash of `pii_collection.jsonl` (one entry per disputed ref) |
| `redacted/<ref>.*` | hash of `<ref>_tags.json` + (in enforce mode) `pii_collection.jsonl` entry for ref + per-doc raw bytes |
| `~/.dsar-audit/<case>/redact_verify.jsonl` (Ph6) | hash of `redacted/*` (one entry per verified doc) |
| `output/<ref>.pdf` | hash of `redacted/<ref>.*` + (redact_verify_pass=true required) |

The Ph6 entry uses an absolute path because the verify-log lives
under the audit tree, not in case `working/`. It still participates
in the chain: `output/` cannot be written until `redact_verify.jsonl`
shows pass=true for every redacted ref. This is the enforcement
mechanism for the halt-on-fail invariant from "The optimal flow".

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
pipeline touches. **Two stages** now call Claude — Stage 4
scope-classify (Sonnet 4.6) and Stage 5 pii-classify (Haiku 4.5,
Phase 4). For a typical case that's hundreds-thousands of
scope-classify calls plus (in shadow + enforce) Haiku calls
against the in-scope subset.

For multiple cases running concurrently, naive parallelism
multiplies the call rate and trips the per-minute or per-day
token limits.

**Locked decision:** a **single global semaphore** gates concurrent
Anthropic calls across both stages. Lives in
`dsar_pipeline.llm_router` (toolkit-side, not orchestrator).
Default concurrency 5; env var `DSAR_LLM_CONCURRENCY` overrides.
The semaphore is process-local (per-`dsar-pipeline` invocation);
operators running multiple parallel `dsar-pipeline` shells on the
same machine are responsible for their own arithmetic.

Both `llm_router.dispatch(role="scope_classify", ...)` (Sonnet 4.6)
and `llm_router.dispatch(role="pii_classify", ...)` (Haiku 4.5)
acquire the SAME semaphore. This is intentional: the limit is
total concurrent Anthropic calls, regardless of model. A case
running in PII enforce mode can saturate the semaphore with
Haiku calls just as easily as with Sonnet calls; the semaphore
keeps total budget under control.

Per-role token-bucket separation (e.g., separate budgets for
Sonnet and Haiku) is YAGNI today — single operator, single
machine. If two stages start contending for the semaphore at
scale, revisit with a weighted-token-bucket strategy.

Why not a true distributed rate-limiter (e.g., Redis-backed)?
Single operator, single workstation. YAGNI.

The reranker (Phase 2 enforce mode) reduces the number of
scope-classify calls that hit the semaphore but doesn't replace
it. Phase 4 PII-classify adds calls; Phase 2's reduction and
Phase 4's addition roughly cancel for typical cases.

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
2. **LLM rate-limit hit** (Anthropic 429) at Stage 4 or Stage 5
   → retry with backoff inside `llm_router.py` (existing
   behaviour); surface only after retries are exhausted.
3. **Validation failure** (`upstream_hash` mismatch) → exit
   non-zero with the exact instruction to repair (e.g.,
   "re-run `dsar-embed --case X --if-exists overwrite`").
4. **Phase 4 budget exceeded** (`DSAR_PII_BUDGET_USD` cap reached
   mid-case) → emit a clear summary of work-done + work-remaining
   + the per-doc spend rate; exit non-zero. Operator decides to
   raise the cap (`DSAR_PII_BUDGET_USD=20 dsar-pipeline --case X
   --from pii_classify`) or abandon the case.
5. **Phase 6 verifier failure** (`PipelineHalt`) → case stays in
   `working/`, NOT promoted to `output/`. Error message names the
   failed-doc count + per-verifier summary + the recovery path
   (re-run after fixing the redact step).
6. **Phase 4 disputed-doc halt** → not a pipeline failure; case
   continues. Disputed docs are skipped by `redact.py` and logged
   to `scope_recheck.jsonl`. The case completes with operator
   review queued; the operator inspects the dispute log, decides
   which docs to redact (manually or by re-running pii-classify
   with adjusted parameters), then runs `dsar-pipeline --case X
   --from redact` to finish.

The orchestrator never silently degrades. DSAR has legal
deadlines; a silent slowdown could push a case past the 30-day
statutory window without operator notice (per zen-tei-integration
spec § Cross-cutting → Failover).

---

## Disagreement + halting (Phase 4 + Phase 6 specific)

Two phases introduce new "this case is paused, operator review
needed" outcomes. Both are intentional design choices, not
failures.

### Phase 4 disputed docs (per-doc halt, case continues)

The PII classifier's `in_scope_recheck` returning `disputed`
flags ONE document for operator review. The case keeps running:
non-disputed docs continue through redact + verify + export. The
disputed docs are written to `scope_recheck.jsonl` with full
reasoning and skipped by `redact.py`.

Per-case operator flow on completion:

```bash
dsar-pipeline --case 301770              # runs to completion;
                                          # disputed docs unredacted
# operator inspects:
cat ~/.dsar-audit/301770/scope_recheck.jsonl
# operator decides per doc:
#   - "override: redact anyway" → tag-edit + re-run from redact
#   - "drop from case: out of scope" → mark as excluded + re-run from redact
#   - "expand scope: confirm in" → update case scope + re-run from pii_classify
dsar-pipeline --case 301770 --from <decision-stage>
```

This is by design: in a regulated domain, the LLM should never
silently auto-override the scope-classify verdict in either
direction. Disputes get human eyes.

### Phase 6 verifier failure (whole-pipeline halt)

The redact-verify gate is binary: every redacted doc must pass
all three verifiers (pikepdf + pytesseract + difflib). Any
failure halts the entire pipeline. The case stays in `working/`
indefinitely until the operator inspects + fixes.

Per-case operator flow on `PipelineHalt`:

```bash
dsar-pipeline --case 301770
# → PipelineHalt: case=301770 redact-verify failed: 3 doc(s) flagged ...
cat ~/.dsar-audit/301770/redact_verify.jsonl
# operator finds e.g. "ref 0042: pikepdf detected unredacted
# 'James Carter' in text layer at position 1457"
# operator inspects redact.py output for that ref, fixes the bug
# (e.g., redact.py was using mtime instead of byte-offset), then:
dsar-pipeline --case 301770 --from redact
```

This is the strictest gate in the whole pipeline. There is no
"override and proceed" path; verifier failure means PII leakage
risk, which is the failure mode the whole pipeline exists to
prevent.

### Both: audit trail invariant

Both halt paths emit explicit audit entries to
`pipeline.jsonl`. Resume from a halt is a NEW audit entry, not
a continuation — the trail shows exactly when the halt
occurred, what the operator did, when the resume happened, and
what changed.

---

## Per-phase integration table

How each of the 7 dsar-toolkit phases hooks into the orchestrator:

| Phase | Module | Where in `pipeline.run()` | Mode handling | Halt path |
|---|---|---|---|---|
| 1 | `dsar_embed` | Stage 2 parallel branch | n/a (always-on) | TEI unreachable → typed exception |
| 2 | `dsar_rerank` | Stage 3 parallel branch (chained after `scope_prefilter`) | `RERANK_MODE={off,shadow,enforce}`; orchestrator reads at start, passes to `core.rerank_case()` | TEI unreachable → typed exception. Promotion-gate violation → reverts to shadow on next run (auto-revert via `~/.dsar-rerank-mode`) |
| 3 | `dsar_search` | NOT in DAG (OOB tool) | n/a | Stale `embeddings.jsonl` → refuses with hash-mismatch error |
| 4 | `dsar_pii_classifier` | Stage 5 (new) | `PII_CLASSIFY_MODE={off,shadow,enforce}`; subject_identifier required; `DSAR_PII_BUDGET_USD` cap | Budget exceeded → typed exception. Disputed docs → per-doc halt, case continues |
| 5 | `dsar_pii_discovery` | Stage 2 parallel branch (third) | `DISCOVERY_ENABLED={true,false}`; toggled at case level | GLiNER model missing → install-time error, not runtime |
| 6 | `dsar_redact_verify` | Stage 7 (new; halt-on-fail gate before export) | `REDACT_VERIFY_ENABLED={true,false}`; false discouraged | ANY verifier failure → `PipelineHalt`; case stays in `working/` |
| 7 | regression harness | NOT in DAG (CI infra) | n/a | n/a |

The orchestrator reads ALL of these env vars / config fields
once at start, validates them, and passes them explicitly into
each module's `core.<fn>(case, mode=..., ...)`. The orchestrator
itself does not branch on mode mid-stage; each module owns its
mode-respect behaviour.

This is the same "two-caller invariant, one implementation per
module" rule from the Orchestration model section, applied to
mode-handling specifically: the CLI of each module accepts the
same mode flags; the orchestrator passes them through; both
paths use the same `core.<fn>()` underneath; drift is
impossible.

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
plans output. But for orientation — and noting that orchestrator
code now lives in `harkers/dsar-orchestrator`, not
`harkers/dsar-toolkit`:

### In dsar-orchestrator (this repo)

```
NEW   src/dsar_orchestrator/__init__.py
NEW   src/dsar_orchestrator/pipeline.py
    - the run(case, …) function from the pseudocode section above
    - all 8 stages wired with ThreadPoolExecutor for Stages 2 + 3
    - reads case config (mode, budget, subject_identifier);
      passes values into each module's core.<fn>()
    - integrates the upstream_hash verification chain
    - opens StageBanner context managers for pipeline.jsonl audit
NEW   src/dsar_orchestrator/hash_chain.py
    - `compute_upstream_hash(artefact_kind, case) -> str`
    - `verify_upstream(case, artefact_path, upstream: str) -> None`
    - shared by all modules' core.<fn>() functions
NEW   src/dsar_orchestrator/cli.py
    - `dsar-pipeline` entry point (--case, --from, --through,
      --only, --check, --dry-run, --mode)
NEW   src/dsar_orchestrator/audit.py
    - PipelineAuditor: emits pipeline.jsonl rows to
      ~/.dsar-audit/<case>/
    - StageBanner context manager
NEW   src/dsar_orchestrator/config.py
    - load_case_config(case) -> CaseConfig
    - validate_phase_4_prereqs(cfg)  (subject_identifier required)
NEW   docs/audit_schemas/pipeline.schema.json
    - shape of pipeline.jsonl rows
NEW   pyproject.toml
    - `dsar-pipeline` entry-point registration
    - depends on `dsar-toolkit>={pinned-version}`
NEW   .importlinter
    - orchestrator may import dsar_pipeline.* + dsar_embed.* +
      dsar_rerank.* + dsar_search.* + dsar_pii_classifier.* +
      dsar_pii_discovery.* + dsar_redact_verify.* + dsar_clients.*
    - toolkit may NOT import dsar_orchestrator.* (one-way rule)
NEW   tests/test_pipeline_run.py + fixtures
NEW   tests/test_hash_chain.py
NEW   tests/test_cli.py
```

### In dsar-toolkit (companion changes)

```
MOD   src/dsar_pipeline/llm_router.py
    - adds the DSAR_LLM_CONCURRENCY semaphore (gates both
      scope_classify and pii_classify roles)
    - role `pii_classify` → claude-haiku-4-5-20251001
    - already-existing role `scope_classify` → claude-sonnet-4-6
MOD   src/dsar_pipeline/audit.py
    - registers pipeline.jsonl schema (cross-repo schema)
MOD   src/dsar_pipeline/pipeline.py
    - DEPRECATED. Operator gets a one-line stub that exits with
      "use dsar-pipeline from dsar-orchestrator instead". May be
      deleted entirely once dsar-orchestrator has shipped and the
      operator has confirmed the orchestrator is the only entry
      point used.
MOD   <case-config schema>
    - subject_identifier becomes required for cases that run
      Phase 4 (PII_CLASSIFY_MODE != off)
MOD   <each module's core.py>
    - hash_chain integration: every core.<fn>() that writes an
      artefact calls compute_upstream_hash(); every one that
      reads an artefact calls verify_upstream() (or has the
      caller orchestrator do so, depending on plan-time choice)
```

The toolkit-side changes are minimal: the orchestrator was always
an extracted concern; the toolkit's existing pipeline.py was the
"legacy" chain that the orchestrator supersedes. The toolkit's
modules are unchanged.

---

## Cross-cutting consistency with the integration spec

This spec composes with
[`2026-05-22-zen-tei-integration-design-v4.md`](https://github.com/harkers/dsar-toolkit/blob/main/docs/superpowers/specs/2026-05-22-zen-tei-integration-design-v4.md)
(in the dsar-toolkit repo):

| Concern | Where it lives |
|---|---|
| Module shape (`__init__`, `cli`, `core`, …) | integration spec v4 (dsar-toolkit) |
| Dependency rules (CI-enforced) | integration spec v4 + Appendix B; this spec's `.importlinter` covers the cross-repo direction |
| HTTP robustness (timeouts/retries/deadlines) | integration spec v4 § Operational semantics |
| Idempotency primitive (`--if-exists`, atomic writes) | integration spec v4 § CLI contract |
| Schema/producer versioning | integration spec v4 § Schema and artifact versioning |
| Phase 2 shadow/enforce promotion gates | integration spec v4 § Phase 2 + Appendix A |
| Phase 4 shadow/enforce promotion gates | integration spec v4 § Phase 4 |
| **Stage ordering + parallelism** | **this spec** |
| **Resume / upstream_hash chain** | **this spec** |
| **Orchestrator vs module-CLI contract** | **this spec** |
| **LLM-call concurrency semaphore** | **this spec** (toolkit-side llm_router.py implements; orchestrator drives) |
| **Disagreement + halting** (Ph4 dispute, Ph6 verify-fail) | **this spec** |
| **Per-phase integration table** | **this spec** |

Together they answer "how is Phase N built?" (integration spec) +
"how do all phases run together?" (this spec). All 7 phases of
the integration spec v4 are covered by the DAG, pseudocode, and
per-phase integration table here.

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
- **Cross-repo dependency: pip-install or git-submodule for
  dsar-toolkit?** `pip install -e ~/projects/dsar-toolkit` (editable
  install). Single operator, single workstation; pip's editable
  mode is the simplest dev story. Git submodule rejected — adds
  fetch/update overhead and complicates the cross-repo dependency
  graph for no benefit at this scale.
- **dsar-toolkit version pin?** Loose at first
  (`dsar-toolkit>=0.1.0`). Tighten to a specific minor version
  (`dsar-toolkit>=0.4,<0.5`) once both repos stabilise. The
  pin lives in `dsar-orchestrator/pyproject.toml`.
- **Where does the cross-repo `pipeline.jsonl` schema live?**
  In dsar-toolkit, alongside the other audit schemas, because the
  toolkit's `audit.py` shared validator is what enforces it. The
  orchestrator emits rows; the toolkit defines + validates the
  shape.
- **Phase 7 (regression harness) — which repo?** Lives in
  dsar-toolkit (because it tests the toolkit's modules), but its
  end-to-end test cases invoke `dsar_orchestrator.pipeline.run()`.
  This is the one place the dependency is bidirectional at test
  time only — dsar-toolkit's test suite depends on
  dsar-orchestrator as a dev-dep. Production code dependency
  stays one-way.

---

## Companion artefacts

- Implementation prompt for the orchestration extensions: filed
  after Phase 1 + Phase 2 have shipped and the integration points
  exist (premature otherwise).
- The integration spec's `phase-1-plan.md` and `phase-2-plan.md`
  (when written) reference this spec for their orchestration
  hook-up details. Each plan implements the orchestrator-side
  changes for the module it's introducing.
