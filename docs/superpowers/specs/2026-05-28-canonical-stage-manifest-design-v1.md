# Canonical stage manifest — design v1

**PR-1 of a 3-PR series** unifying the stage vocabulary across
`dsar-toolkit` and `dsar-orchestrator`. PR-1 ships the canonical
manifest and cross-repo drift checks. PR-2 rewrites the toolkit's
operator-runbook against canonical. PR-3 adds the toolkit Claude
Code hook mirroring the orchestrator's.

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-05-28 | Initial design. Locked after 6 jury rounds on Section 1, 3 rounds on Section 2. |

## Summary

`dsar-orchestrator` defines a 10-stage automated conductor pipeline
(`STAGE_ORDER`). `dsar-toolkit` defines a separate 14-stage legacy
state machine (`STAGES`) plus its own 14-stage operator-runbook
describing manual `run-agent` workflows. The two vocabularies
overlap but diverge — a real bug already manifested in
`operator-guide.md` claiming "9-stage pipeline" while the code had
10, caught by the recently added `tools/check_runbook_drift.py`.

PR-1 introduces a canonical 22-stage manifest as the single source
of truth, lives in `dsar-toolkit`, and is consumed by both repos'
drift checks via snapshot tests. Legacy constants stay hand-coded
at their import sites; snapshot tests assert they match canonical
projections. Drift triggers CI failure; `--fix` (env-guarded)
auto-rewrites legacy tuples to match.

PR-1 does NOT rewrite either runbook (PR-2), does NOT add the
toolkit-side Claude Code hook (PR-3), and does NOT migrate the
legacy constants to derived expressions (deferred to opportunistic
follow-up, tracked as no-SLA issues).

## Motivation

The orchestrator-side runbook automation that just landed
(`.claude/settings.json` hook + `tools/check_runbook_drift.py` +
`runbook-drift.yml` workflow) catches drift WITHIN `dsar-orchestrator`
but is blind to changes in `dsar-toolkit`. Adding a parallel check
in `dsar-toolkit` requires resolving the vocabulary divergence
first — naïve cross-repo manifest comparison false-fires forever
because the two repos describe overlapping-but-distinct concepts:

- `dsar-orchestrator.STAGE_ORDER`: 10 entries
  (`ingest, stage_2_parallel, stage_3_parallel, scope_classify,
   pii_classify, redact, verify_spec, bake, verify_pdf, export`).
- `dsar-toolkit.STAGES`: 14 entries
  (`intake_created, ingestion_running, ingestion_qc_running,
   dedupe_running, context_running, scope_check_running,
   responsiveness_running, redaction_running, redaction_qc_a_running,
   redaction_qc_b_running, improvement_loop_running,
   human_review_pending, release_gate_running, disclosure_pack_ready`).
- `dsar-toolkit.STAGE_TO_MODULES`: 4 keys — unrelated; it's a
  dashboard health-check trigger map, not a stage list.

Toolkit's `operator-runbook.md` opening line confirms the
divergence: *"the current orchestrator tracks state but does NOT
dispatch agents — the operator runs each agent manually."* The
toolkit's 14-stage view is the legacy manual workflow; the
orchestrator's 10-stage view is the new automated conductor.
Per the brainstorming Q&A, the user's intent is to **unify** the
two views (not deprecate one), starting with a canonical vocabulary.

## Goals (PR-1)

1. Single source of truth for stage vocabulary, in
   `dsar-toolkit/src/dsar_pipeline/stages_canonical.py`.
2. Both repos' legacy constants validated against canonical
   projections via snapshot tests in CI.
3. Toolkit and orchestrator drift scripts both fail CI on any
   divergence between hand-coded legacy constants and canonical.
4. `--fix` (env-guarded) auto-rewrites legacy tuples to match
   canonical projections.
5. No behavior change at any orchestrator or toolkit consumer site
   — constants keep their existing names and import paths.

## Non-goals (PR-1)

- Rewriting `dsar-toolkit/docs/operator-runbook.md` against the
  canonical (PR-2).
- Adding a Claude Code hook in `dsar-toolkit` (PR-3).
- Migrating legacy constants to derived expressions
  (`STAGES = manual_stages(use_legacy_names=True)`) — deferred to
  opportunistic follow-up; tracked but no SLA.
- `scope_check_running` semantic confirmation against
  `scope_classify` (PR-2 will revisit; PR-1 ships them distinct).
- ParallelGroup fan-out (`exit_downstream: str` is single-target;
  extends only when needed).

## Design

### 1. Data model + canonical manifest

`dsar-toolkit/src/dsar_pipeline/stages_canonical.py`:

```python
from dataclasses import dataclass
from typing import Literal

StageKind = Literal["operator_checkpoint", "sub_stage", "operator_gate"]
Workflow = Literal["automated", "manual", "both"]

@dataclass(frozen=True)
class StageMeta:
    name: str
    kind: StageKind
    applies_to: Workflow
    parallel_group: str | None           # references PARALLEL_GROUPS key
    downstream: tuple[str, ...]          # canonical stage names OR group names
    legacy_aliases: tuple[str, ...] = ()

@dataclass(frozen=True)
class ParallelGroup:
    name: str
    members: tuple[str, ...]             # ordered, contiguous in CANONICAL_STAGES
    exit_downstream: str                 # single canonical name (fan-out is future work)
    wait_strategy: Literal["wait_all"] = "wait_all"

CANONICAL_STAGES: tuple[StageMeta, ...]   # 22 entries (see vocabulary below)
PARALLEL_GROUPS: dict[str, ParallelGroup]  # 2 entries
LEGACY_ALIAS_MAP: dict[str, str]           # alias -> canonical; derived
```

#### Vocabulary — 22 canonical stages

`name | kind | applies_to | parallel_group | aliases`

```
 1. intake_created              | op_gate | manual    | -                 | -
 2. ingest                       | op_chk  | both      | -                 | ingestion_running
 3. ingestion_qc_running         | sub     | manual    | -                 | -
 4. dedupe_running               | sub     | manual    | -                 | -
 5. embed                        | sub     | automated | stage_2_parallel  | -
 6. detect                       | sub     | automated | stage_2_parallel  | -
 7. context_running              | sub     | manual    | -                 | -
 8. scope_check_running          | sub     | manual    | -                 | -  (TODO PR-2)
 9. people_register              | sub     | automated | stage_3_parallel  | -
10. scope_prefilter              | sub     | automated | stage_3_parallel  | -
11. rerank                       | sub     | automated | stage_3_parallel  | -
12. scope_classify               | op_chk  | both      | -                 | responsiveness_running
13. pii_classify                 | op_chk  | automated | -                 | -
14. redact                       | op_chk  | both      | -                 | redaction_running
15. verify_spec                  | op_chk  | both      | -                 | redaction_qc_a_running
16. bake                         | op_chk  | automated | -                 | -
17. verify_pdf                   | op_chk  | both      | -                 | redaction_qc_b_running
18. improvement_loop_running     | op_gate | manual    | -                 | -
19. human_review_pending         | op_gate | manual    | -                 | -
20. release_gate_running         | op_gate | manual    | -                 | -
21. export                       | op_chk  | automated | -                 | -
22. disclosure_pack_ready        | op_chk  | manual    | -                 | -  (terminal)
```

Contiguity: `stage_2_parallel` members at indices 5-6;
`stage_3_parallel` members at 9-11. Both groups satisfy the
contiguity invariant (validator Item 12).

**Downstream graph note (PR-1 implementation)**: the `downstream`
tuple for each `StageMeta` is omitted from this table. The values
are PR-1 implementation detail constrained by `validate_canonical()`
(Items 1, 5, 8, 9, 13). The snapshot tests + validator together
guarantee any choice of `downstream` tuples is correct iff
`automated_stages() == STAGE_ORDER`, `manual_stages(...) == STAGES`,
and the projection-orphan walks succeed. Implementation fills these
in such that both conditions hold; PR review verifies.

#### Public API

```python
def automated_stages() -> tuple[str, ...]:
    """Orchestrator's STAGE_ORDER projection.
    Filter applies_to ∈ {automated, both}; replace parallel_group
    members with their group name; dedup preserving first occurrence.
    Result: 10 entries matching dsar-orchestrator's STAGE_ORDER."""

def manual_stages(use_legacy_names: bool = False) -> tuple[str, ...]:
    """Toolkit's STAGES projection.
    Filter applies_to ∈ {manual, both}; emit canonical names in
    canonical order. When use_legacy_names is True, rewrite each
    name to legacy_aliases[0] if non-empty, else the canonical name.
    Result: 14 entries matching dsar-toolkit's STAGES (in legacy-names mode)."""

def operator_checkpoints() -> tuple[str, ...]:
    """Runbook-visible subset; filter kind ∈ {operator_checkpoint, operator_gate}.
    Result: 13 entries — the universal operator-facing surface for
    both workflows. PR-2 will use this to drive the runbook table."""

def canonicalize(name_or_alias: str) -> str:
    """Returns the canonical name for any canonical name or legacy alias.
    Raises KeyError on unknown input."""

def derive_orchestrator_sub_stages_by_stage() -> dict[str, tuple[str, ...]]:
    """For each operator_checkpoint with no parallel_group: maps to (name,).
    For each parallel_group: maps to its members in canonical order.
    Mirrors orchestrator.pipeline.SUB_STAGES_BY_STAGE shape."""

def validate_canonical() -> None:
    """Asserts the 13-item contract below; raises ValueError with the
    full list of violations on any inconsistency. NOT called at import."""
```

#### validate_canonical() contract

1. Every `StageMeta.downstream` entry resolves to either a
   `CANONICAL_STAGES` name OR a `PARALLEL_GROUPS` name.
2. Every `StageMeta.parallel_group` is None or in `PARALLEL_GROUPS`.
3. For every group G:
   `tuple(s.name for s in CANONICAL_STAGES if s.parallel_group == G.name)
    == G.members` (exact equality, including order).
4. All members of a `ParallelGroup` share the same `applies_to`
   value.
5. Every group member's `downstream` entries are either (a) other
   members of the same group, or (b) the group's `exit_downstream`.
6. `ParallelGroup.exit_downstream` exists in `CANONICAL_STAGES`.
7. No `legacy_aliases` collision (within a stage, across stages, or
   against any canonical name).
8. `CANONICAL_STAGES` is a valid topological ordering of the full
   downstream graph (no cycles, including within groups).
9. For each workflow w ∈ {automated, manual}: starting from the
   workflow's entry stage (manual: `intake_created`; automated:
   `ingest`), forward traversal via downstream — implemented
   standalone in the validator, NOT delegated to `automated_stages()` —
   reaches every stage in the workflow's projection. No orphans.
10. Namespace disjointness: `{s.name for s in CANONICAL_STAGES}`,
    `set(LEGACY_ALIAS_MAP.keys())`, and `set(PARALLEL_GROUPS.keys())`
    are pairwise disjoint.
11. Member kind: every group member has `kind == sub_stage`.
12. Contiguity: for every group G, the indices in `CANONICAL_STAGES`
    of `G.members` form a contiguous block.
13. Topological ordering: every `downstream` target has a higher
    index than its source (group-internal edges resolved via the
    contiguity block).

#### Snapshot tests (PR-1 deliverable in BOTH repos)

```python
# dsar-toolkit/tests/test_canonical_projection.py
from dsar_pipeline.stages_canonical import (
    manual_stages, validate_canonical,
)
from dsar_pipeline.orchestrator_state import STAGES

def test_manual_projection_matches_toolkit_stages():
    assert tuple(manual_stages(use_legacy_names=True)) == tuple(STAGES)

def test_canonical_validates():
    validate_canonical()


# dsar-orchestrator/tests/test_canonical_projection.py
from dsar_pipeline.stages_canonical import (
    automated_stages, derive_orchestrator_sub_stages_by_stage,
    validate_canonical,
)
from dsar_orchestrator.pipeline import STAGE_ORDER, SUB_STAGES_BY_STAGE

def test_automated_projection_matches_orchestrator_stage_order():
    assert tuple(automated_stages()) == tuple(STAGE_ORDER)

def test_sub_stages_match_derived():
    assert SUB_STAGES_BY_STAGE == derive_orchestrator_sub_stages_by_stage()

def test_canonical_validates():
    validate_canonical()
```

### 2. Migration approach + drift-check shape

#### Migration: snapshot-only enforcement

Legacy constants stay hand-coded at their existing sites. Snapshot
tests assert match against canonical projections. Drift triggers
CI failure; dev runs `DSAR_DRIFT_FIX_ALLOW=1 python tools/check_<...>.py --fix`
to auto-rewrite the legacy tuple via libcst. The diff appears in
the PR for review.

Considered and rejected: wrapper shims
(`STAGES = manual_stages(use_legacy_names=True)`). Rationale:
snapshot-only minimizes PR-1's blast radius — `pipeline.py`,
`orchestrator_state.py`, `audit_verify.py` are not modified at all
in PR-1. Migration to derived expressions happens opportunistically
in follow-up issues (no SLA, tracked).

#### Drift dataclass

```python
from dataclasses import dataclass, field
from typing import Literal

DriftType = Literal[
    "automated_projection_mismatch",
    "manual_projection_mismatch",
    "sub_stages_mismatch",
    "canonical_validation",
    "runbook_unknown_token",
    "runbook_token_not_op_checkpoint",
    "runbook_missing_op_checkpoint",
    "runbook_malformed_token",
    "stage_count_claim_mismatch",
    "idempotency_violation",
]

@dataclass(frozen=True)
class Drift:
    type: DriftType
    fixable: bool                          # True if --fix can resolve it
    file: str | None = None                # path to rewrite (fixable only)
    target: str | None = None              # assignment target name (fixable only)
    expected: object = None
    actual: object = None
    extra: tuple[tuple[str, str], ...] = ()  # frozen-safe key/value pairs
```

#### Toolkit-side drift check (NEW)

`dsar-toolkit/tools/check_stage_consistency.py`:

```python
def run_checks() -> list[Drift]:
    drifts = []
    try:
        validate_canonical()
    except ValueError as e:
        drifts.extend(parse_validation_errors(e))
    if tuple(manual_stages(use_legacy_names=True)) != tuple(STAGES):
        drifts.append(Drift(
            type="manual_projection_mismatch",
            fixable=True,
            file="src/dsar_pipeline/orchestrator_state.py",
            target="STAGES",
            expected=tuple(manual_stages(use_legacy_names=True)),
            actual=tuple(STAGES),
        ))
    return drifts

# NOTE: dsar-toolkit's audit_verify._STAGE_ORDER is intentionally NOT
# checked in PR-1. Its semantics relative to STAGES are not yet
# documented; comparing it to a canonical projection risks false
# positives. Tracked as a PR-1 follow-up issue:
# "Investigate audit_verify._STAGE_ORDER semantics and add to
# stage-consistency drift check." If it turns out to equal
# manual_stages() ordering, the projection is the same; if it's a
# subset/filter, a separate derive_audit_stage_order() helper is
# added in a follow-up PR.

def apply_fixes(drifts: list[Drift]) -> tuple[int, int]:
    """Batches drifts by file; parses each file with libcst exactly once;
    applies all rewrites in a single tree pass; verifies the resulting
    tree compiles; writes atomically. Returns (fixed_count, failed_count).
    On a per-drift idempotency failure (current file value != drift.actual),
    emits a new Drift(type='idempotency_violation', fixable=False) rather
    than raising — caller sees it in the re-run."""

def main() -> int:
    args = parser.parse_args()  # --json, --fix
    if args.fix and os.environ.get("DSAR_DRIFT_FIX_ALLOW") != "1":
        print("ERROR: --fix requires DSAR_DRIFT_FIX_ALLOW=1", file=sys.stderr)
        return 2
    drifts = run_checks()
    if args.fix:
        fixable = [d for d in drifts if d.fixable]
        if fixable:
            fixed, failed = apply_fixes(fixable)
            drifts = run_checks()  # post-fix re-run; idempotency violations show up here
    return emit_and_exit(drifts, json=args.json)
```

`.github/workflows/stage-consistency.yml`:

```yaml
on:
  pull_request:
    paths:
      - "src/dsar_pipeline/stages_canonical.py"
      - "src/dsar_pipeline/orchestrator_state.py"
      - "src/dsar_pipeline/audit_verify.py"
      - "tools/check_stage_consistency.py"
      - ".github/workflows/stage-consistency.yml"
  push:
    branches: [main]
    paths: [<same>]
jobs:
  consistency:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: pip install -e .
      - run: python tools/check_stage_consistency.py --json
```

#### Orchestrator-side drift check (EXTEND existing)

`dsar-orchestrator/tools/check_runbook_drift.py` adds two
functions and refactors one:

```python
from dsar_pipeline.stages_canonical import (
    automated_stages, operator_checkpoints, canonicalize,
    validate_canonical, derive_orchestrator_sub_stages_by_stage,
)

def _check_canonical_alignment() -> list[Drift]:
    drifts = []
    if tuple(automated_stages()) != tuple(STAGE_ORDER):
        drifts.append(Drift(
            type="automated_projection_mismatch", fixable=True,
            file="src/dsar_orchestrator/pipeline.py", target="STAGE_ORDER",
            expected=tuple(automated_stages()), actual=tuple(STAGE_ORDER),
        ))
    expected_subs = derive_orchestrator_sub_stages_by_stage()
    if SUB_STAGES_BY_STAGE != expected_subs:
        drifts.append(Drift(
            type="sub_stages_mismatch", fixable=True,
            file="src/dsar_orchestrator/pipeline.py", target="SUB_STAGES_BY_STAGE",
            expected=expected_subs, actual=SUB_STAGES_BY_STAGE,
        ))
    try:
        validate_canonical()
    except ValueError as e:
        drifts.extend(parse_validation_errors(e))
    return drifts

def _check_runbook_ladder() -> list[Drift]:
    drifts: list[Drift] = []
    expected: set[str] = set(operator_checkpoints())
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    raw_tokens = set(re.findall(r"--through\s+(\S+)", text)) - {"<stage>"}
    canonical_seen: set[str] = set()
    for token in raw_tokens:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", token):
            drifts.append(Drift(
                type="runbook_malformed_token", fixable=False,
                extra=(("token", token),),
            ))
            continue
        try:
            canonical = canonicalize(token)
        except KeyError:
            drifts.append(Drift(
                type="runbook_unknown_token", fixable=False,
                extra=(("token", token),),
            ))
            continue
        canonical_seen.add(canonical)
        if canonical not in expected:
            drifts.append(Drift(
                type="runbook_token_not_op_checkpoint", fixable=False,
                extra=(("token", token), ("canonical", canonical)),
            ))
    for stage in expected - canonical_seen:
        drifts.append(Drift(
            type="runbook_missing_op_checkpoint", fixable=False,
            extra=(("stage", stage),),
        ))
    return drifts
```

`_check_stage_count_claims()` unchanged.

`apply_fixes()`, `main()`, env-guard, `--json` flag follow the same
contracts as the toolkit-side script.

`.github/workflows/runbook-drift.yml` already triggers on
`pipeline.py`; trigger paths extended to include new test files.

### 3. PR-1 scope + risks + follow-ups

#### PR-1 deliverables

In `dsar-toolkit` (new files unless noted):
1. `src/dsar_pipeline/stages_canonical.py` — manifest + API.
2. `tests/test_canonical_projection.py` — snapshot + validation tests.
3. `tools/check_stage_consistency.py` — drift script with `--fix`
   (env-guarded) and `--json`.
4. `.github/workflows/stage-consistency.yml`.
5. `pyproject.toml` — version bump signaling canonical's introduction.

In `dsar-orchestrator`:
6. `tests/test_canonical_projection.py` — mirrors toolkit-side shape.
7. `tools/check_runbook_drift.py` — extended (canonical alignment +
   refactored ladder check using `operator_checkpoints()` and
   `canonicalize()` + `--fix`/`--json`).
8. `.github/workflows/runbook-drift.yml` — paths expanded.
9. `pyproject.toml` — bump pinned dsar-toolkit version to match #5.

#### Cross-repo coordination

Breaking canonical changes require a coordinated 2-PR cycle:
1. Toolkit PR ships the canonical change + version bump.
2. Orchestrator PR bumps the pinned toolkit version in
   `pyproject.toml`.

Snapshot tests on both sides provide back-pressure: toolkit-side
goes green after #1; orchestrator-side fails until #2 lands.

**Emergency hotfix escape hatch**: if a canonical change is needed
urgently and the 2-PR window is too slow, the toolkit can ship
without version bump (or with a pre-release version) and the
orchestrator can pin to the unreleased commit SHA. Both repos are
local development environments per project conventions; no
public-registry friction. Documented as an escape hatch, not a
recommended path.

#### Risks + mitigations

| Risk | Mitigation |
|---|---|
| Canonical change breaks toolkit-side or orchestrator-side CI in the wrong order | 2-PR coordinated cycle; snapshot tests fail-fast both sides; SHA-pin escape hatch |
| Legacy constants drift between toolkit releases | Snapshot tests on every PR touching the legacy files (in trigger paths) |
| `--fix` corrupts files | `Drift.fixable: bool` + post-fix re-run + libcst batching by file + idempotency violation reported as Drift not exception |
| `--fix` accidentally invoked in CI | `DSAR_DRIFT_FIX_ALLOW=1` env-guard |
| `scope_check_running` semantics turn out to be wrong | Marked TODO; PR-1 ships as distinct stage; PR-2 reassesses |
| Future ParallelGroup fan-out needs | `exit_downstream: str` extends to tuple at that point; consumer code updates with it |

#### Follow-up issues opened with PR-1

- `dsar-toolkit#N`: Migrate `STAGES`, `_STAGE_ORDER` to derived
  expressions from canonical. Label `tech-debt: opportunistic`. No
  SLA — happens when the affected files are next touched for any
  reason. Drift checks + `--fix` are the safety net in the
  meantime.
- `dsar-orchestrator#M`: Migrate `STAGE_ORDER`, `SUB_STAGES_BY_STAGE`
  to derived expressions. Same labelling.
- `dsar-toolkit#N+1`: Investigate `audit_verify._STAGE_ORDER`
  semantics and add to stage-consistency drift check (PR-1
  follow-up; not blocking).
- `dsar-toolkit#N+2`: PR-2 — rewrite `operator-runbook.md` against
  `operator_checkpoints()`; confirm `scope_check_running` semantics.
- `dsar-toolkit#N+3`: PR-3 — Claude Code hook mirroring the
  orchestrator's `.claude/settings.json` + reminder script.

#### Items addressable in PR review (from round-3 jury, non-blocking)

- libcst rewrites batched per-file (Kimi): `apply_fixes()`
  implementation detail; contract specifies it; implementation
  proves it.
- `Drift.extra` frozen-safety: use `tuple[tuple[str, str], ...]`
  (encoded as key-value pairs) per Kimi's hashability concern.
  Updated in the dataclass above.
- `Drift.type` as `Literal` (Qwen): typed in the dataclass above.
- Idempotency-violation reporting (Qwen): `apply_fixes()` emits
  `Drift(type="idempotency_violation", fixable=False)` rather than
  raising; surfaces in the post-fix re-run.
- Atomic multi-file rollback (Kimi): `--fix` is local-only via
  env-guard; `git checkout` is the recovery path. Formal
  transaction system not required for this scope.

## Brainstorming history

Section 1 (data model + canonical manifest): 6 jury rounds. Key
convergent findings driven through:
- Round 1: parallel-group modeling too weak; downstream validation
  missing; toolkit `*_running` stages need explicit accommodation.
- Round 2: snapshot tests required against actual repo constants;
  `detect.applies_to="both"` wrong; vocabulary remained speculative.
- Round 3: 1/3 approve (Gemini). Snapshot tests should call
  `manual_stages()` directly; `exit_downstream` fan-out
  underspecified.
- Round 4: 0/3 (Gemini withdrew approval). Hand-rolled
  `validate_canonical()` orphan-check had real bugs.
- Round 5: 0/3. New design holes surfaced — namespace collision,
  fan-in integrity, contiguity, `SUB_STAGES_BY_STAGE` derivation
  ambiguity.
- Round 6: 0/3 but incremental. Allow `downstream` to target
  `ParallelGroup.name`; strict DAG check; `sub_stage`-only group
  membership.
- **Locked at round 6.** Per CLAUDE.md jury rule, lock-in on user
  decision.

Section 2 (migration + drift-check shape): 3 jury rounds.
- Round 1: 1/3 (Gemini). Runbook alias bug; `--fix` flag suggested;
  wrapper-shim rationale weak.
- Round 2: 0/3. Convergent issues fixed but new ones surfaced
  (`--fix` safety, CI triggers).
- Round 3: 1/3 (Gemini approved). Remaining critiques implementation-
  level (libcst batching, frozen-safe Drift, structured idempotency
  reporting). **Locked at round 3** on user decision.

Section 3 (PR-1 scope + risks): no jury — administrative content,
not design risk.

## References

- Orchestrator runbook automation that motivated this work:
  - `.claude/settings.json` + `.claude/hooks/stage-edit-reminder.sh`
  - `tools/check_runbook_drift.py`
  - `.github/workflows/runbook-drift.yml`
  - Spec: orchestration design v2
    (`docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v2.md`)
- Toolkit's existing operator-runbook (PR-2 will rewrite):
  `~/projects/dsar-toolkit/docs/operator-runbook.md`
- Toolkit's stage constants (PR-1 snapshot tests assert against
  these):
  - `~/projects/dsar-toolkit/src/dsar_pipeline/orchestrator_state.py`
    (`STAGES`)
  - `~/projects/dsar-toolkit/src/dsar_pipeline/audit_verify.py`
    (`_STAGE_ORDER`)
- Orchestrator's stage constants (PR-1 snapshot tests assert
  against these):
  - `src/dsar_orchestrator/pipeline.py` (`STAGE_ORDER`,
    `SUB_STAGES_BY_STAGE`)
