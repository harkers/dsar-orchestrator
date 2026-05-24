# `dsar_orchestrator.log_analyser`

Local-LLM-driven analyser for case audit logs. Stays on the box —
routes through `mlx-broker` on `http://127.0.0.1:8090` by default.
No external API calls.

## What it does

1. Reads every audit log under `~/.dsar-audit/<case>/`
   (`pipeline.jsonl`, `llm_calls.jsonl`, `scope_rerank.jsonl`,
   `pii_collection.jsonl`, `scope_recheck.jsonl`) plus
   `working/post_bake_findings.jsonl` from the case directory.
2. Computes deterministic stats (stage durations, halt reasons, LLM
   call counts, disputed-doc rates, verify failures).
3. Asks a local LLM via mlx-broker to surface issues — categories
   include `stage_failed`, `stage_duration_outlier`, `dispute_rate`,
   `verify_failure`, `schema_violation`, `llm_cost`, `rerun_pattern`,
   `threshold_edge`, `completeness`.
4. Writes structured findings to
   `~/.dsar-audit/<case>/analysis.jsonl` + `analysis.md`.
5. If any **critical** finding is present, drops
   `~/.dsar-audit/<case>/analysis-block.flag`. The orchestrator's
   next `dsar-conductor --case <no>` refuses to start until the flag
   is cleared (`--acknowledge-issues` or
   `dsar-analyse-logs --case <no> --clear-block`).

## What it never sees

Document text. The collector reads only the structured audit
metadata: refs, hashes, scores, timestamps, error messages, schema
fields. The LLM's context never contains client document content.

## Usage

```bash
# Run analyser; print report; drop block flag if critical findings
dsar-analyse-logs --case 300100

# Same, but print only — don't persist or flag
dsar-analyse-logs --case 300100 --no-write

# Override model (default: `tools` alias, Hermes-4-70B-4bit)
DSAR_ANALYSER_MODEL=code dsar-analyse-logs --case 300100

# Check whether a case is currently under an analyser block
dsar-analyse-logs --case 300100 --check-block

# Clear the block (operator acknowledgement)
dsar-analyse-logs --case 300100 --clear-block
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MLX_BROKER_URL` | `http://127.0.0.1:8090` | Base URL of mlx-broker |
| `DSAR_ANALYSER_MODEL` | `tools` | Model alias to use for analysis |

## Outputs

| Artefact | Shape |
|---|---|
| `~/.dsar-audit/<case>/analysis.jsonl` | Header row + one row per finding; every row carries `schema_version` + `producer_version` |
| `~/.dsar-audit/<case>/analysis.md` | Human-readable summary; safe to share with the operator |
| `~/.dsar-audit/<case>/analysis-block.flag` | Present iff any critical finding; gates the next pipeline run |
