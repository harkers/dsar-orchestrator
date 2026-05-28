# Operator guide — `dsar-orchestrator`

How to run a DSAR case through the orchestrator on `zen`, recover from
failures, and use the analyser to push back on suspect runs.

For a step-by-step operator loop from ingestion through final export, see [docs/runbooks/dsar-operator-loop.md](docs/runbooks/dsar-operator-loop.md). For the autonomous, agent-driven equivalent (Claude Code looping the pipeline via the Ralph Loop plugin), see [docs/runbooks/ralph-dsar-prompt.md](docs/runbooks/ralph-dsar-prompt.md).

If ingestion fails, the runbook also has a dedicated ingest QA section that explains what failed, what to retry, and which artifacts to inspect.

## The two operator commands

| Command | What it does |
|---|---|
| `dsar-conductor --case <no>` | Run (or resume) the full 10-stage pipeline for a case |
| `dsar-analyse-logs --case <no>` | Ask the local LLM (via mlx-broker) to review the case's audit logs and surface issues |

Both are read-only by default with the right flags (`--check`,
`--no-write`). Both stay on the box — no external API calls.

## Day-to-day flow

### Start a new case

```bash
# 1. Create the case directory + config
mkdir -p ~/dsars/cases/300100/source ~/dsars/cases/300100/working
# (drop source documents into ~/dsars/cases/300100/source/)
cat > ~/dsars/cases/300100/case_config.json <<'EOF'
{
  "case_no": "300100",
  "case_scope": "All personal data about <subject> from <date> to <date> ...",
  "subject_identifier": {
    "primary_name": "Full Name",
    "dob": "YYYY-MM-DD",
    "employee_id": "...",
    "aliases": ["...", "..."],
    "disambiguation_notes": "NOT <other person with similar name>..."
  },
  "rerank_mode": "shadow",
  "pii_classify_mode": "shadow"
}
EOF

# 2. Preview the resume plan
dsar-conductor --case 300100 --check
#   Case 300100 resume plan:
#     ✗ ingest                → will run
#     ✗ stage_2_parallel      → will run
#     ...

# 3. Run it
dsar-conductor --case 300100
#   [...] stage=ingest             start
#   [...] stage=ingest             done in 87s
#   [...] stage=stage_2_parallel   start  (parallel: embed + detect + pii-discovery)
#   ...

# 4. Review the analyser's verdict
dsar-analyse-logs --case 300100
```

### Resume after a failure

The cascade detects what's already done by reading each artefact's
`upstream_hash` field. After a partial run + a fix:

```bash
# Inspect what would re-run
dsar-conductor --case 300100 --check
#     ✓ ingest                (all sub-stage artefacts fresh)
#     ✓ stage_2_parallel      (all sub-stage artefacts fresh)
#     ✗ stage_3_parallel      → will run    (something stale here)
#     ✗ scope_classify        → will run    (downstream-forced)
#     ...

# Just keep going — same command, picks up where it left off
dsar-conductor --case 300100
```

### Re-do one stage

```bash
# Re-embed only; downstream cascade picks up the change automatically
dsar-conductor --case 300100 --only embed

# Or use the module's own CLI when it ships in the toolkit:
dsar-embed --case 300100 --if-exists overwrite
```

### Force a full re-run

```bash
# Disable the cascade; run every in-scope stage regardless of freshness
dsar-conductor --case 300100 --force
```

### Stop after a particular stage

```bash
# Quick smoke: run only up through scope-classify; don't redact yet
dsar-conductor --case 300100 --through scope_classify
```

### Resume from a particular stage

```bash
# Re-do everything from redact onward (e.g., after fixing redact.py)
dsar-conductor --case 300100 --from redact
```

## When the analyser blocks you

`dsar-analyse-logs` reads all the audit logs for a case and asks a
local LLM via mlx-broker to identify issues. If it finds anything
critical, it drops a block flag the next `dsar-conductor` run will
refuse to start through:

```bash
$ dsar-conductor --case 300100
ERROR: case=300100 is under an analyser block.
Inspect ~/.dsar-audit/300100/analysis.md and either:
  - fix the critical findings, then `dsar-analyse-logs --case 300100`
    (clean run removes the block automatically), or
  - `dsar-conductor --case 300100 --acknowledge-issues` to proceed anyway.
```

Three ways to clear it:

```bash
# 1. Inspect, fix, re-analyse — clean run auto-clears
$EDITOR ~/.dsar-audit/300100/analysis.md
# ... fix the issues, re-run the relevant module ...
dsar-analyse-logs --case 300100
# (block flag removed if no critical findings this time)

# 2. Manual clear (operator acknowledgement)
dsar-analyse-logs --case 300100 --clear-block

# 3. Acknowledge inline + proceed with the pipeline
dsar-conductor --case 300100 --acknowledge-issues
```

The analyser never reads document content. It works only from the
structured audit logs (refs, hashes, scores, timestamps, error
messages). Routes through mlx-broker on the local box; no external
API calls.

## Module-agent validation

After every module step, the orchestrator invokes the toolkit's
per-module agent to validate the work. Results go to
`~/.dsar-audit/<case>/module_checks.jsonl`. The contract:

```python
# Lives in the toolkit at dsar_pipeline.module_agents.<sub_stage>
def check_work(case_path: Path) -> ModuleCheckResult:
    """Validate the artefacts the module just wrote. Return:
      .ok: bool
      .severity: "info" | "warning" | "critical"
      .findings: list[str]
      .recommendation: str
    """
```

If a `critical` finding fires, the orchestrator halts immediately
with `PipelineHalt`; the audit row is written before the halt, so
the audit log captures the failure even though the run aborted.

`warning` and `info` severities don't halt — they're recorded for the
log analyser to weigh against other signals.

If a sub-stage has no agent shipped yet, the orchestrator logs an
info-level `no_agent` row and continues. Pipelines that hit
unagented stages still run; the gap is visible in the audit log
for tracking.

## Resume-cascade mental model

```
upstream change → artefact's recorded upstream_hash no longer matches
                → cascade marks the stage stale
                → all downstream stages forced stale (downstream-forced rule)
                → fresh artefacts downstream are ignored; everything re-runs
```

The hash chain prevents "I forgot to re-run the embed step after
swapping source documents". You can't have a partially-fresh case;
once any upstream changes, the downstream re-runs.

Inspect the chain:

```bash
# Confirm where the cascade would resume from
dsar-conductor --case 300100 --check

# See exactly why a stage is marked stale (look in module_checks.jsonl
# + check the upstream_hash field on each artefact)
cat ~/dsars/cases/300100/working/embeddings.jsonl | head -1 | jq .upstream_hash
```

## Where things live

| Path | Owned by |
|---|---|
| `~/dsars/cases/<no>/` | Per-case work; secure-deleted with the case bundle when work is done |
| `~/dsars/cases/<no>/source/` | Raw source documents (operator-provided) |
| `~/dsars/cases/<no>/working/` | All intermediate artefacts (embeddings, tags, rerank verdicts, ...) |
| `~/dsars/cases/<no>/redacted/` | Stage-3 redacted documents |
| `~/dsars/cases/<no>/output/` | Final exported PDFs |
| `~/dsars/cases/<no>/case_config.json` | Per-case config (mode, threshold, subject_identifier) |
| `~/.dsar-audit/<no>/pipeline.jsonl` | Orchestrator audit log (stage transitions, durations, outcomes) |
| `~/.dsar-audit/<no>/module_checks.jsonl` | Per-module agent validation outcomes |
| `~/.dsar-audit/<no>/analysis.jsonl` | Log analyser findings (one row per finding) |
| `~/.dsar-audit/<no>/analysis.md` | Log analyser findings (human-readable) |
| `~/.dsar-audit/<no>/analysis-block.flag` | Present iff analyser found criticals |
| `~/dsars/cases/<no>/working/post_bake_findings.jsonl` | Phase 6 verifier outcomes (per-finding, severity-tagged) |
| `~/.dsar-audit/<no>/llm_calls.jsonl` | Toolkit-side LLM call log |

All under `~/.dsar-audit/` are mode 0700 (operator-only).

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `RERANK_MODE` | `shadow` | Phase 2 mode: off / shadow / enforce |
| `RERANK_THRESHOLD` | `0.01` | Phase 2 starting threshold (see calibration demo for rationale) |
| `PII_CLASSIFY_MODE` | `shadow` | Phase 4 mode |
| `DSAR_PII_BUDGET_USD` | `10.0` | Phase 4 per-case Haiku 4.5 budget cap |
| `DISCOVERY_ENABLED` | `true` | Phase 5 (PII-discovery): enable or skip |
| `REDACT_VERIFY_ENABLED` | `true` | Phase 6 (forensic verify): enable or skip |
| `DSAR_LLM_CONCURRENCY` | `5` | Global semaphore for Claude calls |
| `MLX_BROKER_URL` | `http://127.0.0.1:8090` | Local mlx-broker URL for the log analyser |
| `DSAR_ANALYSER_MODEL` | `tools` | mlx-broker alias the analyser uses |

Override files (any of these → file content wins over env vars):

| File | Purpose |
|---|---|
| `~/.dsar-rerank-mode` | Manually pin reranker mode for all cases (operator override) |
| `~/.dsar-pii-mode` | Same, for the PII classifier |

## Troubleshooting

| Symptom | Likely cause + fix |
|---|---|
| `Required toolkit module '<x>' is not installed` | `pip install -e ~/projects/dsar-toolkit/` |
| `case=<n> is under an analyser block` | See "When the analyser blocks you" above |
| `mlx-broker at http://127.0.0.1:8090 unreachable` | `launchctl print gui/$(id -u)/com.mlx-broker` then `launchctl kickstart` if needed |
| `case=<n> redact-verify failed: ...` | Phase 6 caught leakage; inspect `working/post_bake_findings.jsonl` for high-severity rows, fix redact, re-run from redact |
| `case=<n>: module agent for <stage> flagged a critical issue` | Toolkit agent rejected the stage's output; recommendation is in the error + audit log |
| `Upstream changed since <path> was written` | Hash chain detected upstream drift; re-run the upstream stage or use `--force` |

## What stays off the box

Nothing. Everything in this orchestrator + the toolkit runs locally
on zen. LLM calls go to mlx-broker (port 8090) or LiteLLM
(port 4000); TEI calls go to ports 8084/8085; embeddings + reranker
weights live under `~/models/mlx/`. The only external traffic is
the toolkit-side Anthropic calls for `scope_classify` and
`pii_classify` — and even those route through the existing
`llm_router` semaphore, so the operator has a single throttle point.

The log analyser specifically routes through mlx-broker, never the
external Anthropic endpoint. Its prompts contain only structured
audit metadata (refs, hashes, scores, error messages) — never
document content.
