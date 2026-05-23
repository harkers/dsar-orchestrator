# dsar-toolkit pipeline orchestration — design (v4)

**Status:** v4 — 2026-05-23. Locks the adapter pattern between the
conductor's coarse `_run_<X>` helpers and the toolkit's actual
interfaces. v3 captured the divergence; v4 captures the bridge.

**Relationship to other specs.**
[`2026-05-22-pipeline-orchestration-design-v3.md`](2026-05-22-pipeline-orchestration-design-v3.md)
is the immediate predecessor — read it first for the
architecture-level context (CLI rename to `dsar-conductor`,
positioning above the toolkit's state machine, the toolkit-module
API drift table). v4 changes only the integration shim layer; the
coarse-stage DAG + resume cascade + module-agent validation + halt
semantics are unchanged.

## Version history

| Version | Date | Commit | Summary |
|---|---|---|---|
| v1 | 2026-05-22 | `0258f76` | Initial draft. |
| v2 | 2026-05-22 | `0b8a2e8` | Extends to all seven integration phases. |
| v3 | 2026-05-22 | (committed) | Toolkit-divergence sync. CLI rename. Names the adapter shim layer as v4 work. |
| v4 | 2026-05-23 | this commit | Adapter pattern locked. Eight conductor stages + two parallel sub-stages get one `src/dsar_orchestrator/adapters/<stage>.py` apiece. Each adapter declares a retirement trigger naming the Python entry the toolkit would have to ship for the adapter to retire. Hermetic-test contract: every adapter has one injectable dependency (subprocess runner or Python builder). Import-linter contract 9 enforces adapters' leaf position. |

---

## Why an adapter layer at all

v3 made the case: the conductor's lazy-import shapes
(`dsar_pipeline.detect.run_2_1_to_2_4(case_path)`) don't match the
toolkit's actual shapes (CLI entries like `dsar-redact`,
module-derived `python -m dsar_pipeline.detect <subject>`, or
Python entries like
`dsar_redact_verify.core.verify_case(case_path)`). v3 left the
options open: subprocess the toolkit's CLIs, or wrap its Python
entries. v4 picks per stage and locks it.

Two further drivers settle the design:

1. **Hermetic tests.** Operator constraint: "tests must be hermetic
   — no live TEI, no live mlx-broker, no live Anthropic". The
   adapter layer is the *only* layer that talks to the toolkit;
   making each adapter inject its dependency lets the integration
   test suite stay hermetic without sys.modules contortions for
   every code path.
2. **Retirement contract.** The toolkit will eventually grow Python
   entries that match the conductor's coarse call shape (it has
   said as much in its responses to coordination issues #1–#10).
   The adapter layer is the natural place to declare "when the
   toolkit ships X, this file gets deleted." That makes the
   tech-debt explicit.

## The adapter contract

Every adapter file under `src/dsar_orchestrator/adapters/` follows
the same shape:

```python
"""Conductor-owned <stage> adapter — Stage <N>.

Bridges to the toolkit's <interface>. <One-paragraph summary of
what the adapter does on top of the toolkit's output.>

**Retirement contract.** When the toolkit ships
<expected Python entry>, this adapter retires.
"""

PRODUCER_VERSION = "dsar_orchestrator.adapters.<stage> 0.1.0"
SCHEMA_VERSION = "1.0"

# Injectable dependency: either a subprocess runner
#   (argv, env [, cwd]) -> CompletedProcess
# or a Python callable
#   (case_path | working_dir | ...) -> result-dict | Verdict.

def _default_<thing>() -> <Fn>:
    # Lazy-resolve from the toolkit (importlib.import_module or
    # subprocess.run). Tests never call this — they inject.
    ...

def run_for_case(cfg: CaseConfig, *, <thing>: <Fn> | None = None) -> None:
    if <thing> is None:
        <thing> = _default_<thing>()
    # 1. Call the toolkit
    # 2. Validate the toolkit produced its expected output
    # 3. Compute upstream_hash via stages._hash_<X> (or equivalent)
    # 4. Write the conductor-shaped artefact atomically
    #    (temp + fsync + os.replace)
```

### Required adapter outputs

Every artefact the adapter writes must carry, on each JSONL row (or
in each JSON object):

- `upstream_hash` — the value the resume cascade uses to detect
  invalidation. Must match the hash function from `stages.py` for
  this stage.
- `schema_version` — for forward-compat. `"1.0"` for v4.
- `producer_version` — `"dsar_orchestrator.adapters.<stage> 0.1.0"`.

Artefacts the toolkit writes itself (e.g.
`~/.dsar-audit/<case>/redact_verify.jsonl`) are not in scope for
the adapter's output contract; the conductor consumes them as-is.

### Atomic writes

JSONL and JSON artefacts are always written via `temp + fsync +
os.replace`. This prevents the resume cascade from reading a
partially-written file if the conductor crashes mid-write.

### Error wrapping

- Subprocess non-zero exit → `DSARPipelineError` with case_no +
  return code + stderr tail (last 2000 bytes).
- Toolkit-specific exceptions that have a typed conductor surface
  (e.g. `PIIBudgetExceeded` → `BudgetExceededError`) get wrapped.
- Verifier failures (redact_verify) raise `PipelineHalt` with the
  audit-log path + resume hint embedded.

## The locked adapter table

| Stage | Adapter file | Toolkit interface | Adapter shape | Retirement trigger |
|---|---|---|---|---|
| ingest (1) | `adapters/ingest.py` | `python -m dsar_pipeline.ingest <subject>` (cwd=case_path) | subprocess runner | `dsar_pipeline.ingest.run_for_case(case_path, subject_name)` |
| embed (2/1) | `adapters/embed.py` | `dsar_clients.tei_embed_client.embed(texts)` | injected `embedder` | `dsar_embed.core.embed_corpus(case_path)` (post-Ollama deprecation) |
| detect_2_1_to_2_4 (2/2) | `adapters/detect_2_1_to_2_4.py` | `python -m dsar_pipeline.detect <subject>` (cwd=case_path) + per-ref `<ref>_tags.json` aggregation | subprocess runner | `dsar_pipeline.detect.run_for_case(case_path, subject_name)` |
| pii_discovery (2/3) | `adapters/pii_discovery.py` (placeholder — toolkit ships its own entry) | `dsar_pii_discovery.core.discover_entities(case_path)` | direct lazy-import | None — toolkit's Python entry already matches; kept for symmetry + injection |
| people_register (3/1) | `adapters/people_register.py` | `dsar_pipeline.people_register.build_people_register(working_dir)` | injected `builder_fn` | `dsar_pipeline.people_register.run_for_case(case_path)` |
| scope_prefilter (3/2) | `adapters/scope_prefilter.py` | `dsar_clients.tei_embed_client.embed([case_scope])` + inline cosine math | injected `embedder` | `dsar_pipeline.scope_prefilter.run_for_case(case_path)` — explicit, since the toolkit's `embed.py` still has Ollama |
| rerank (3/3) | `adapters/scope_prefilter.py` (chained) | toolkit `dsar_rerank.core.rerank_case(...)` | direct lazy-import via the existing call | None — toolkit's Python entry already matches |
| scope_classify (4) | `adapters/scope_classify.py` | `dsar-scope-check --case <id>` CLI + cascade anchor on top of `working/scope_verdicts.jsonl` | subprocess runner | `dsar_pipeline.scope_check_stage.run_for_case(case_path)` |
| pii_classify (5) | `adapters/pii_classify.py` | `dsar_pii_classifier.core.discover_case(case_path, mode)` + per-stage finding aggregation | injected `classifier_fn` | `dsar_pii_classifier.core.classify_case_for_conductor(case_path)` |
| redact (6) | `adapters/redact.py` | `dsar-redact --case <id>` CLI + cascade anchor on top of `working/redaction_input.jsonl` | subprocess runner | `dsar_pipeline.redact_stage.run_for_case(case_path)` |
| redact_verify (7) | `adapters/redact_verify.py` | `dsar_redact_verify.core.verify_case(case_path)` | injected `verify_fn` | `dsar_redact_verify.core.verify_case_for_conductor(case_path)` |
| export (8) | `adapters/export.py` | `dsar-bake --case <id>` CLI then `python -m dsar_pipeline.export` (both cwd=case_path) | single subprocess runner handling both calls | `dsar_pipeline.export.run_for_case(case_path)` |

## Integration-test design under the adapter layer

The integration suites (`tests/integration/test_full_pipeline_with_stubs.py`,
`tests/integration/test_synthetic_case_100.py`) now use two
techniques in tandem:

1. **sys.modules stubs** for adapters that resolve their default
   via `importlib.import_module` (embed, pii_classify,
   pii_discovery, redact_verify, people_register, rerank). The
   stub modules in `tests/_toolkit_stubs/stubs.py` get installed
   in `sys.modules` so the adapter's `_default_<fn>()` finds them.
2. **monkeypatch on `_default_<runner>`** for adapters that
   subprocess (ingest, detect_2_1_to_2_4, scope_classify, redact,
   export). The fixture replaces `_default_runner` with a fake
   that writes the toolkit's expected outputs.

Both techniques are reversible per-test via pytest's `monkeypatch`.
There is no live network call, no real toolkit invocation, no
shared global state between tests.

## Import-linter contract

`.importlinter` contract 9 is the structural enforcement:

```
adapters bridge to toolkit; may import config/hash_chain/exceptions only
```

This stops adapters from importing `pipeline`, `cli`, `audit`,
`stages`, `module_agents`, `log_analyser`, or `synthesis`. It
keeps adapters as a leaf layer the toolkit can take a dependency
on if the abstraction ever needs to flow the other way (e.g. for
the toolkit's own integration tests).

## What v4 does NOT change

- The 8 coarse stages + ThreadPoolExecutor parallelism on Stages 2/3.
- The resume cascade via `upstream_hash` on every artefact row.
- The module-agent validation framework
  (`src/dsar_orchestrator/module_agents.py`).
- The PipelineHalt semantics for Phase 6 verifier failures.
- The audit log shape under `~/.dsar-audit/<case>/`.
- The case-config / subject-identifier / mode fields.
- The CLI surface (`dsar-conductor --case <id> [--force | --from |
  --only | --check]`).

## Open questions still flowing through coordination

- Toolkit issue #1 (adapter-pattern confirmation): resolved.
  Toolkit team confirmed the adapter list + ack'd retirement
  triggers.
- Toolkit issues #2 → #10: pending. Will be folded into v5 if any
  change the contract; otherwise v4 is the locked target.

## v5 candidates (not in scope here)

- Move bake out of export, into redact (so redact_verify can run
  after redacted/ exists, matching toolkit reality where the
  verifier inspects redacted PDFs). Requires a stage reordering +
  agent updates.
- Drop adapters whose toolkit retirement triggers ship between
  now and the next spec rev.
- Promote `_default_<runner>` injection points to a single
  conductor-wide dependency-injection registry, if cross-stage
  testing benefits exceed the abstraction cost.
