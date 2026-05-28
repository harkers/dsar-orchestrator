# Ralph Loop prompt for DSAR case execution

Drive one DSAR case from ingestion through exported disclosure pack
autonomously, with Claude Code looping the orchestrator pipeline
until the exit criteria are met (or the iteration cap fires).

This pairs with the Ralph Loop plugin (the `/ralph-loop` slash
command in Claude Code; run `/help` for plugin details). For the
human-driven walkthrough of the same pipeline, see
[dsar-operator-loop.md](./dsar-operator-loop.md) — this prompt
defers to that runbook for stage semantics and retry recipes.

## When to use

**Good fit:**

- The case is well-defined: `case_config.json` is in place,
  source documents are loaded, `case_scope` is unambiguous.
- No operator-judgment decisions are expected mid-flight (no
  borderline subjects, no known scope ambiguities, no redaction
  subtleties needing review).
- You can walk away and inspect the result later.

**Bad fit — use the runbook instead:**

- Case has known scope or subject-identity ambiguities.
- The analyser is likely to block (recent toolkit churn,
  unverified module agents, first run on a new corpus shape).
- You want stage-by-stage human eyes on each artefact.

## Invocation

1. Copy the prompt block below into a per-case file (e.g.
   `~/dsars/cases/<case-no>/ralph-prompt.md`) and replace
   `<CASE-NO>` with the case number throughout.
2. From the orchestrator repo root, with the case set up per the
   runbook, run:

   ```bash
   /ralph-loop "$(cat ~/dsars/cases/<case-no>/ralph-prompt.md)" \
       --completion-promise "CASE_COMPLETE" \
       --max-iterations 40
   ```

`--max-iterations 40` is a starting point. Tighten for small
cases; raise only after you have vetted the prompt on a similar
case before.

## Prompt template

Copy this block verbatim, substitute the case number, then feed
it to `/ralph-loop` as above.

```text
Drive DSAR case <CASE-NO> through the orchestrator pipeline to a
completed disclosure pack.

Reference (read on every iteration): docs/runbooks/dsar-operator-loop.md
— the stage ladder, retry policy, ingestion-QA detail, analyser
block recovery, and exit criteria all live there.

Loop body, every iteration:

1. Run `dsar-conductor --case <CASE-NO> --check`. Read the
   resume plan.
2. If every checkpoint is fresh AND
   `~/dsars/cases/<CASE-NO>/output/manifest.json` exists AND the
   expected PDFs are present under `~/dsars/cases/<CASE-NO>/output/`,
   output `<promise>CASE_COMPLETE</promise>` and stop.
3. Otherwise, identify the next stale checkpoint from the plan
   and run it with `dsar-conductor --case <CASE-NO> --through <stage>`.
4. Inspect that checkpoint's "Primary outputs to inspect" per the
   runbook's stage ladder.
5. On failure, follow the runbook's Retry Policy: fix the smallest
   local cause, then rerun with `--only <stage> --force` or
   `--from <stage>` exactly as the runbook directs (note the
   stage-specific addenda for `verify_spec` and `export`).
6. If the next run is refused with `analyser block`, follow the
   runbook's Analyser Block Recovery section.

Hard rules:

- Never output `<promise>CASE_COMPLETE</promise>` unless the exit
  criteria in step 2 are met. Do not emit the promise to escape
  a stuck iteration; let the iteration cap end the loop instead.
- Never edit source documents under
  `~/dsars/cases/<CASE-NO>/source/`. That tree is operator input,
  not pipeline state.
- Never pass `--force` to the whole case. Use stage-scoped
  `--only <stage> --force` when retrying a single module.
- Never proceed past a `critical` module-agent finding with
  `--acknowledge-issues` without first writing the reasoning to
  `~/.dsar-audit/<CASE-NO>/operator-notes.md`.
- If the same checkpoint fails three times in a row with the same
  finding, append a blocker note to
  `~/.dsar-audit/<CASE-NO>/ralph-blocker.md` (failing stage,
  exact error, what you tried, what you ruled out) and stop
  trying that stage. The iteration cap will terminate the loop.
```

## After the loop exits

Two outcomes:

- **`<promise>CASE_COMPLETE</promise>` was emitted** — verify
  `output/manifest.json` and the PDFs on disk, then run
  `dsar-analyse-logs --case <case-no>` for the final analyser
  verdict before queuing the pack for disclosure review.
- **Iteration cap hit** — read
  `~/.dsar-audit/<case-no>/ralph-blocker.md` for the agent's
  blocker note, then resume the case manually per the runbook
  from the last successful checkpoint shown by
  `dsar-conductor --case <case-no> --check`.

## Why this is separate from the runbook

The runbook is the source of truth for stage semantics, retry
rules, and inspection points — for a human walking the pipeline
with their own eyes on each artefact. This prompt is a thin
controller that hands that walk to an iterating agent and gates
exit on the case's exit criteria. Stage details deliberately do
not duplicate here; if the runbook changes, the agent picks up
the new behaviour on its next iteration's read.
