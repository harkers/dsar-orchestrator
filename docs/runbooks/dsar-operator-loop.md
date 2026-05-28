# DSAR operator loop runbook

How to walk one case through the orchestrator from ingestion to
exported disclosure pack, with the inspection points and retry
recipes you need at each checkpoint.

For the full conceptual model (10-stage pipeline, resume cascade,
analyser block flag, environment variables), see
[operator-guide.md](../operator-guide.md). For the autonomous
agent-driven equivalent of this walk, see
[ralph-dsar-prompt.md](./ralph-dsar-prompt.md).

## Prerequisites

- A case directory at `~/dsars/cases/<case-no>/` with `source/` and `working/` present.
- A valid `case_config.json` with at least `case_no`, `case_scope`, `subject_identifier`, and the phase flags you intend to use.
- Toolkit and orchestrator installed in the same environment.
- A fitness report (or `--auto-fitness`) if `fitness_check_enabled` is on for the case.

## First step, always: `--check`

```bash
dsar-conductor --case <case-no> --check
```

Print the resume plan before each forward step. It is read-only,
costs nothing, and tells you which checkpoints will run and why.

## Stage ladder

The ladder shows operator checkpoints. The conductor advances
through internal sub-stages (`stage_2_parallel`, `stage_3_parallel`,
…) between checkpoints — see
[operator-guide.md](../operator-guide.md) for the full 10-stage map.

After running each command, inspect the listed outputs before
moving on to the next row.

| Checkpoint | Command | Primary outputs to inspect |
|---|---|---|
| Ingestion | `dsar-conductor --case <case-no> --through ingest` | `working/register.json`, `working/register_meta.json`, extracted text files |
| Scope classification (Durant test) | `dsar-conductor --case <case-no> --through scope_classify` | `working/scope_verdicts.jsonl`, `working/scope_classify_complete.jsonl` |
| Redaction plan | `dsar-conductor --case <case-no> --through redact` | `working/pii_collection.jsonl`, `working/redaction_input.jsonl`, `working/redact_complete.json` |
| Pre-bake verification | `dsar-conductor --case <case-no> --through verify_spec` | `working/verify_spec_findings.jsonl` |
| Bake redacted files | `dsar-conductor --case <case-no> --through bake` | `redacted/` tree |
| Post-bake PDF verification | `dsar-conductor --case <case-no> --through verify_pdf` | `working/post_bake_findings.jsonl` |
| Export | `dsar-conductor --case <case-no> --through export` | `output/`, `output/manifest.json` |

## Retry policy

When a checkpoint fails:

1. Identify the failing stage, module, or adapter from stderr, the
   audit row in `~/.dsar-audit/<case-no>/pipeline.jsonl`, or the
   module-agent finding in
   `~/.dsar-audit/<case-no>/module_checks.jsonl`.
2. Inspect the stage's output artefacts and fix the smallest local
   cause that explains the failure.
3. Rerun with one of:
    - `--only <stage> --force` — upstream is fine, just redo that
      one stage.
    - `--from <stage>` — you fixed something upstream of the
      failure; let the cascade advance from there.
4. Re-check the resulting artefacts.
5. Escalate to the operator only if the rerun still fails or the
   case needs a human decision.

### Stage-specific addenda

Most stages follow the generic recipe above. These have wrinkles:

- **Ingest** — see *Ingestion QA* below. Failures come from the QA
  pass that runs immediately after the adapter, not from the
  adapter itself unless stderr says otherwise.
- **`verify_spec` finding** — fix the redaction plan inputs and
  rerun `--from redact`, not `--from verify_spec`. The plan
  inputs are what `verify_spec` is checking, so its own rerun
  would just re-emit the same finding.
- **`export` failure** — if the failure is in export itself
  (manifest writer, output tree permissions), rerun
  `--from export`. Only use `--from bake` when the redacted tree
  itself is corrupt and needs rebuilding.

## Ingestion QA

The ingest checkpoint runs an adapter and then an ingest-QA check
in the same conductor invocation. If the QA fails, the ingest
checkpoint stops before any downstream stage runs.

### What the QA checks

- `working/register.json` exists and contains at least one ref.
- Every ref in `working/register.json` has a matching extracted text file.
- `working/register_meta.json` exists and contains `upstream_hash`.
- The ingest adapter wrote `working/data_subject.json` when the case has a subject identifier.

### What each failure means

- `register.json missing or empty` — ingest did not produce the
  corpus index, or the source tree was empty.
- `extracted text missing for ref=...` — the source file was not
  extracted successfully, or the register points at a file that is
  no longer there.
- `register_meta.json missing or has no upstream_hash` — the
  conductor metadata sidecar was not written, so the resume
  cascade cannot trust ingest freshness.
- Adapter exited non-zero before QA ran — toolkit ingest failed
  upstream of QA. Start with the stderr tail.

### What to retry

- Source tree changed or was incomplete → fix `source/`, rerun
  `dsar-conductor --case <case-no> --through ingest`.
- Only the extracted text or register is stale →
  `dsar-conductor --case <case-no> --only ingest --force`.
- `register_meta.json` missing → rerun ingest; the adapter writes
  that sidecar immediately after ingest completes.
- `data_subject.json` missing → correct `case_config.json`, rerun
  ingest.

### Where to look next

- `~/.dsar-audit/<case-no>/pipeline.jsonl` for the stage row.
- `~/.dsar-audit/<case-no>/module_checks.jsonl` for the QA findings.
- `working/register.json`, `working/register_meta.json`,
  `working/data_subject.json` for the artefacts themselves.

## Analyser block recovery

`dsar-analyse-logs` can drop a block flag that the next
`dsar-conductor` invocation refuses to start through. If you see:

```
ERROR: case=<case-no> is under an analyser block.
```

…follow
[operator-guide.md § When the analyser blocks you](../operator-guide.md#when-the-analyser-blocks-you).
In short, one of:

- Inspect `~/.dsar-audit/<case-no>/analysis.md`, fix the
  criticals, rerun `dsar-analyse-logs --case <case-no>`
  (a clean run auto-clears the flag).
- `dsar-analyse-logs --case <case-no> --clear-block` (explicit
  operator acknowledgement).
- `dsar-conductor --case <case-no> --acknowledge-issues`
  (acknowledge inline and proceed with the pipeline).

## Fitness preflight

If `fitness_check_enabled` is on and the conductor refuses to
start with a fitness-preflight error, run:

```bash
dsar-fitness-canary --deployment-id <id>
```

The `<id>` comes from the fitness report header for this case, or
from the most recent fitness row in
`~/.dsar-audit/<case-no>/pipeline.jsonl`. To bypass preflight for
a single run, use `--auto-fitness` instead.

## Fast path

If you don't need stage-by-stage checkpointing:

```bash
# Full case, all stages, no preview
dsar-conductor --case <case-no>

# Full case with the resume plan printed first
dsar-conductor --case <case-no> --check
```

The cascade still picks up where it left off if you previously
ran partial stages.

## Exit criteria

The case is ready for final disclosure review when:

- `output/manifest.json` exists, and
- the expected PDFs are present in `output/`.

(`verify_pdf` cleanliness is implicit — `export` won't succeed
otherwise.)
