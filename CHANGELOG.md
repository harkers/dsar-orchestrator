# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: see [`VERSIONING.md`](VERSIONING.md).

## [Unreleased]

## [0.9.9] - 2026-05-27

### Added — two-panel redaction viewer (#109, v3-console Phase 2)

- New `local_broker/redaction_viewer.py` projects redaction overlays from the existing `<case>/working/<ref>_tags.json` files (produced by `pii_tagger_mini` / toolkit `detect.py`) at render time. No new persisted artefacts, no toolkit changes — decision broker-locked as Option C earlier in v3 design.
- Code badges pin to the v3 jury synthesis taxonomy: `DS` (data subject), `TP` (third party), `SC` (special category), `CH` (child), `AE` (adverse event), `LC` (legal counsel), `CC` (organisation / client confidential), `NR` (needs review), `SEC` (security).
  - Default mapping in `REDACTION_CODE_MAP`: `data_subject→DS`, `third_party→TP`, `organisation→CC`, `client_confidential→CC`, `special_category→SC`, `child→CH`, `adverse_event→AE`, `legal_counsel→LC`, `security→SEC`.
  - `redact == "flag"` overrides classification → always `NR` so ambiguous entities stand out in the redacted pane.
  - Unmapped classification → `NR` (operator should triage rather than miss).
- `build_overlay(case_dir, doc_ref) -> dict` returns `{doc_ref, filename, exists, entities: [{start, end, text, classification, redact, code}, ...]}` sorted by `start` asc. Missing/corrupt tag file → `exists=False, entities=[]` (no exception).
- `render_redacted_html(text, overlay)` emits `<span data-code="X" data-start="N" data-end="M">[X]</span>` overlays only for entities where `redact in (True, "flag")`. Entities with `redact == False` (data-subject preserve) appear verbatim — matches the toolkit's actual redaction output. Outside-overlay text is `html.escape`d so the pane is XSS-safe.
- New `/redaction-viewer/<ref>` GET route in `operator_console`. Two-pane layout: left pane original text (verbatim), right pane redacted view with spans + colour-coded codes. Graceful empty-pane fallback when the text or tag file is missing.
- New `ROUTE_PREFIX_REQUIRED_PHASE: dict[str, str]` registers phase gating for dynamic-suffix routes; `is_route_accessible` consults it after exact-match fails. `/redaction-viewer/` requires min phase `redact`.
- 14 new tests in `tests/test_redaction_viewer.py` covering: `classify_code` mapping + flag-override + unknown→NR; `build_overlay` sort order, missing file, missing offsets, corrupt JSON; `render_redacted_html` redact-true spans, redact-false verbatim, flag→NR, XSS escaping; route end-to-end (both panes + missing-text fallback + phase gate). Suite: 555 passing (was 541; +14).
- Version: pre-1.0 PATCH.

## [0.9.8] - 2026-05-27

### Added — compensating `FAILURE_RECORDED` chain event closes reverse-drift gap (#114, v3-console Phase 2)

- Chain-first ordering (PR #104) prevents one direction of audit drift (no JSONL row without a chained `REVIEWER_DECISION_MADE` event). The reverse — chain emit succeeds then the JSONL/state append fails (disk full, EACCES mid-call) — leaves an orphan event in the hash chain that downstream `audit_verify` cannot correlate.
- Fix: each of the three operator decision sites now wraps the post-chain user-visible write in `try/except OSError`. On failure it emits a compensating `FAILURE_RECORDED` event referencing the orphan event's `original_event_hash`, then re-raises.
  - `leak_review.record_decision` → wraps `<case>/audit/leak_review_decisions.jsonl` append
  - `unextractable.record_decision` → wraps `<case>/audit/unextractable_decisions.jsonl` append
  - `operator_console.toggle_blocker_resolved` → wraps `<case>/audit/operator_console_state.json` save
- Reuses the existing `AuditEventType.FAILURE_RECORDED` enum in the toolkit — no toolkit PR needed.
- `audit_chain.py` factored: new private `_emit_typed(event_type, …)` helper backs the existing `emit_decision_event` plus new `emit_failure_event` / `emit_failure_for_case_dir` siblings. Behaviour of existing callers unchanged.
- Compensating event payload (top-level after toolkit `append_event` merge): `phase=post-chain-jsonl-write` (or `-state-write`), `original_event_hash`, `error_type`, `error`, `target_path`, plus the relevant `doc_ref` / `source_path` / `blocker_id`. `stage` matches the decision kind for filtering. `prev_hash` of the compensating event equals `original_event_hash` (both events land sequentially under the same `_EMIT_LOCK` + `fcntl.flock`).
- 4 new tests in `tests/test_compensating_failure_event.py` covering all three call sites' failure paths (monkeypatch `Path.open` / `Path.write_text` to raise `OSError`) plus a happy-path sanity. Suite: 541 passing (was 537; +4).
- Version: pre-1.0 PATCH (small additive change, matches 0.9.6/0.9.7 cadence).

## [0.9.7] - 2026-05-26

### Fixed — unextractable.retry_extract crashed with TypeError after #105

- `unextractable.retry_extract()` internally calls `record_decision()`, which started requiring a `reason_code` keyword after PR #32 / v0.7.0 (#105). The retry path was not updated and crashed with `TypeError: record_decision() missing 1 required keyword-only argument: 'reason_code'` on every invocation — both happy-path (retried_ok) and error-path (retried_fail).
- Fix: all 3 `record_decision()` calls inside `retry_extract` now pass `reason_code="R009"` (Technical extraction issue), matching the semantic intent of a re-ingest after the original failure.
- Discovered during live operator retry of 57 case-301770 files agent01 had silently skipped (toolkit issue harkers/dsar-toolkit#153). All 57 retries failed with this regression; once the fix lands and the venv is re-pinned, the retry can proceed.
- 3 new regression tests in `tests/test_unextractable_retry.py` covering: happy path appends ingested-item + reason-coded decision row; ingest_v3 raises → retried_fail with reason_code; missing source path → retried_fail with reason_code. (No coverage existed before — that's what let the regression slip through #105 review.)
- Hermetic count: 479 passing (was 476; +3).
- Version: pre-1.0 PATCH.

## [0.9.6] - 2026-05-26

### Added — dsar-durant-calibration CLI promoted to conductor (#111 sub-6, completes #111)

- New `dsar_orchestrator.local_broker.durant_calibration` + `dsar-durant-calibration` CLI. Promoted from `audit/calibration_portal.py`.
- Operator-calibration web portal validating the Durant verdict quality (distinct from `qa_sample` which validates redaction quality). Stratified 30-doc sample: 10 disputed + 10 agreed-exclude + 5 recheck-ambiguous + 5 originally-biographical.
- Subject name auto-resolved from `data_subject.json::full_name` (was hardcoded in the case-side script). Falls back to case dir name.
- Decisions are append-only with latest-wins read semantics; full click history preserved at `<case-root>/working/operator_calibration_30.jsonl`.
- Agreement report computes operator vs original-Durant + operator vs recheck rates per stratum and overall.
- Same case-root parameterisation as the other promoted scripts. ThreadingHTTPServer + `make_handler(case_root, sample)` closes over per-case state cleanly (was a class variable in the case-side script).
- 12 broker-free tests in `tests/test_durant_calibration.py` covering module surface (sums to 30), case-root resolution, subject-name resolution + fallback, normalisation helpers (op/durant/recheck → include/exclude/uncertain), stratum picking (proper counts when pool exceeds want, under-samples when pool < want, deterministic with default seed), append-only decision persistence + latest-wins, agreement report (no-sample / has-sample / counts match).
- Hermetic count: 476 passing (was 464; +12).
- Version: pre-1.0 PATCH.

### #111 status

All 6 promotions complete. Next: #112 deletes the originals from `/Volumes/propharma/cases/<case>/audit/`.

## [0.9.5] - 2026-05-26

### Added — dsar-pii-tagger-mini CLI promoted to conductor (#111 sub-5)

- New `dsar_orchestrator.local_broker.pii_tagger_mini` + `dsar-pii-tagger-mini` CLI. Promoted from `audit/agent06-tagger.py`.
- Local-broker PII tagger (model alias `mini`) producing toolkit-compatible `<case-root>/working/<ref>_tags.json` files (schema matches `dsar_pipeline.detect.py`), then consumed unchanged by the toolkit's `agent06_redaction`.
- LLM entity detection + deterministic regex layer (email / UK phone / NI number). Per-doc pipeline merges both layers, dedupes by `(start, end, text)`.
- Classification precedence (highest-priority first):
  1. `subject_protected_phrases` match → `data_subject`, never redact
  2. subject identifier match (name tokens > 2 chars + aliases + emails) → `data_subject`, never redact
  3. LLM said `data_subject` → never redact
  4. LLM said `third_party` → redact
  5. LLM said `organisation` → `flag`
  6. anything else → `unknown` / `flag`
- Regex-detected emails/phones/NINOs default to `third_party` redact=True unless preserved by the rules above.
- Same case-root parameterisation + retry-with-backoff + canonical-only dedupe-filter handling as the other promoted scripts.
- Filter source: `scope_verdicts.jsonl` (Durant-canonical) preferred; falls back to `responsiveness_decisions.jsonl::disposition=included` with a warning if absent. Logs the `present∧excluded` discrepancy set so the operator can audit which Durant-positive items the responsiveness layer would have dropped.
- 19 broker-free tests in `tests/test_pii_tagger_mini.py` covering `_subject_identifier_set` (name token expansion, short-token guard), `_protected_phrases_set` lowercasing, `_find_all_spans` (case-insensitive, non-overlapping, empty needle), `_regex_layer` (email/phone/NINO detection), `_classify_entity` precedence (protected wins over LLM, subject ID wins over LLM, all six paths), `build_tags_for_doc` (LLM+regex merge with broker monkeypatched, short-entity drop, multi-span dedupe), `run()` end-to-end (processes included, skips already-done, returns 1 on missing inputs).
- Hermetic count: 464 passing (was 445; +19).
- Version: pre-1.0 PATCH.

### Followups

- 1 promotion remaining under #111: calibration_portal.

## [0.9.4] - 2026-05-26

### Added — dsar-context-classify-mini CLI promoted to conductor (#111 sub-4)

- New `dsar_orchestrator.local_broker.context_classify_mini` + `dsar-context-classify-mini` CLI. Promoted from `audit/agent04-mini.py`.
- Local-broker context classifier (model alias `mini`) bypassing the toolkit's `QwenContextClient` (hardcoded remote 30B endpoint, ~120s/call on local hardware). Output is agent05-compatible: `durant_verdict` + `primary_classification` + `is_about_requester` + `confidence` + `requester_role` + `evidence_snippet` + `recommended_action` + `rationale`.
- Best-effort `_coerce_parsed` clamps confidence to [0, 1], maps unknown enums to safe defaults (durant→ambiguous, primary→other, is_about→unclear), truncates over-long string fields (rationale 500 / durant_rationale 600 / evidence_snippet 300 / role+action 32). Bad rows never raise — they land as `error_state` rows in agent05-compatible shape so downstream consumers don't have to special-case errors.
- Output lands at `<case-root>/working/context_classifications.jsonl`; per-50-doc progress at `<case-root>/audit/context-classify-mini-progress.jsonl`.
- Same case-root parameterisation + resume + canonical-only dedupe-filter handling as the other promoted scripts.
- 12 broker-free tests in `tests/test_context_classify_mini.py` covering module surface, case-root resolution, `_coerce_parsed` (confidence clamping, unknown-enum fallbacks for each enum field, field truncation), `classify_one` (happy / empty-content / network-error paths), `run()` end-to-end (processes all + skips resumed; returns 1 on missing inputs).
- Hermetic count: 445 passing (was 433; +12).
- Version: pre-1.0 PATCH.

### Followups

- 2 promotions remaining under #111: agent06-tagger, calibration_portal.

## [0.9.3] - 2026-05-26

### Added — dsar-durant-recheck CLI promoted to conductor (#111 sub-3)

- New `dsar_orchestrator.local_broker.durant_recheck` + `dsar-durant-recheck` CLI. Promoted from `audit/agent-durant-recheck.py`.
- Under-disclosure safety net: for every doc the Durant pass marked `work_context_only`, asks the broker the inverse question. Three recheck verdicts: `confirmed_work_context_only` / `reclassify_to_biographical` / `reclassify_to_ambiguous`.
- **Confirmation-bias guard:** the original Durant rationale is kept in the audit record but NOT shown to the model — the recheck assesses each doc on its own merits.
- **Under-disclosure-conservative default:** unknown / malformed verdicts default to `reclassify_to_ambiguous` (escalation), never to `confirmed_work_context_only`. "I'm not sure" must escalate, never silently agree.
- Same case-root parameterisation, resume-safe atomic cleanup, retry-with-backoff, and canonical-only dedupe-filter handling as `durant_pass`.
- 10 broker-free tests in `tests/test_durant_recheck.py` covering module surface, case-root resolution, `recheck_one` happy / unknown-verdict-defaults-to-ambiguous / confirmation-bias-guard / network-error paths, excluded-refs filter (only `work_context_only` rows), `run()` end-to-end (processes excluded only, skips biographical-orig docs, skips already-rechecked, returns 1 on missing inputs).
- Hermetic count: 433 passing (was 423; +10).
- Version: pre-1.0 PATCH.

### Followups

- 3 promotions remaining under #111: agent04-mini, agent06-tagger, calibration_portal.

## [0.9.2] - 2026-05-26

### Added — dsar-durant-pass CLI promoted to conductor (#111 sub-2)

- New `dsar_orchestrator.local_broker.durant_pass` module + `dsar-durant-pass` CLI script entry. Promoted from the per-case `audit/agent-durant.py`.
- Applies the Durant v FSA biographical-focus test to every ingested doc via mlx-broker (model alias `mini`). Output lands at `<case-root>/working/durant_verdicts.jsonl`; per-50-doc progress at `<case-root>/audit/durant-progress.jsonl`.
- Case-root parameterised via `--case-root` / `DSAR_CASE_ROOT` / cwd. `INCLUDE_DUPLICATES=1` opts out of the canonical-only dedupe filter.
- Resume-safe: drops errored rows (`error_state` present) atomically before rerun; skips already-classified refs.
- Retry-with-backoff (2/8/30/60s) on HTTP 500 / connection errors / timeouts; JSON-parse and verdict-enum failures do NOT retry — they're recorded as `error_state` rows and re-processed on next run.
- 13 broker-free tests in `tests/test_durant_pass.py` covering module surface, case-root resolution, `_strip_fences`, `classify_one` happy + invalid-verdict + empty-content + bad-JSON + network-error paths, resume cleanup (drops errored rows), and `run()` end-to-end with broker monkeypatched (processes all, skips already-classified, returns 1 on missing inputs).
- Hermetic count: 423 passing (was 410; +13).
- Version: pre-1.0 PATCH (additive subsystem + CLI script).

### Followups

- 4 promotions remaining under #111: agent-durant-recheck, agent04-mini, agent06-tagger, calibration_portal.

## [0.9.1] - 2026-05-26

### Added — dsar-approver CLI promoted to conductor (#111 sub-1)

- New `dsar_orchestrator.local_broker.dsar_approver` module + `dsar-approver` CLI script entry. Promoted from the per-case `audit/dsar-approver.py` so the script no longer lives in the encrypted sparse bundle alongside subject data (per the "nothing not related to the data subject in that volume" rule).
- Case-root parameterised via `--case-root` flag, `DSAR_CASE_ROOT` env var, or cwd (in that priority). Audit log appends to `<case-root>/audit/approver-decisions.jsonl`.
- Module exports `review(case_id, package, *, case_root=None)` for programmatic use; `_VALIDATOR` exposes the published JSON schema.
- 13 broker-free tests in `tests/test_dsar_approver.py` covering schema validation (acceptance + rejection of bad enums), case-root resolution priority, audit-log path resolution, broker monkeypatch happy + failure paths (invalid JSON, schema failure, empty content), and CLI plumbing (`--help`, missing-case-id error).
- Hermetic count: 410 passing (was 397; +13).
- Version: pre-1.0 PATCH (additive subsystem + CLI script).

### Followups

- Promote remaining 5 case-side scripts under #111 (one PR each): agent-durant, agent-durant-recheck, agent04-mini, agent06-tagger, calibration_portal.
- After all 6 promotions land, #112 deletes the originals from `/Volumes/propharma/cases/<case>/audit/`.

## [0.9.0] - 2026-05-26

### Added — 30-doc QA stratified sample + completion gate

- New `dsar_orchestrator.local_broker.qa_sample` module:
  - `sample_for_qa(case_dir, *, size=30, seed=42, force=False)` — picks `size` redacted docs stratified into three buckets per the jury synthesis: 10 high entity_count + 10 medium + 10 random. Persists to `audit/qa_sample.jsonl` on first call; subsequent calls return the persisted sample. `force=True` discards and re-picks against the current corpus.
  - `list_qa_sample(case_dir)` — sample rows enriched with the latest decision per `doc_ref`.
  - `record_qa_decision(case_dir, *, doc_ref, decision, reason_code, note)` — chain-first (same pattern as leak_review / unextractable). Decisions: `approve` / `request_reredaction` / `mark_false_positive` / `mark_missed_redaction` / `escalate`. `reason_code` validated against the R001-R010 taxonomy from #105.
  - `qa_sample_complete(case_dir)` — `True` iff every sampled doc has a non-pending decision.
  - `summary_counts(case_dir)` — page-header headline numbers.
- New `/qa-sample` GET + `/api/qa-sample/decide` POST. Page shows bucket badge (HIGH/MED/RAND), entity + redaction counts, decision form per doc, and a stage-complete banner once every doc has a final decision.
- `/qa-sample` added to `ROUTE_REQUIRED_PHASE` (min phase = redact).
- 14 new tests in `tests/test_qa_sample.py` covering sample size (default 30, smaller corpus), stratification (10/10/10), high bucket has higher avg entity count than medium, persistence + idempotent re-read, `force=True` reset, status filter (only `redacted`), decision validation (reason_code required, unknown decision rejected), chain emission on decide, completion check (no-sample / partial / complete), and decision status enrichment for the UI.
- Hermetic count: 397 passing (was 383; +14).
- Version: pre-1.0 MINOR (additive subsystem + new route).

## [0.8.0] - 2026-05-26

### Added — multi-factor action queue + Next Best Review

- New `dsar_orchestrator.local_broker.action_queue` module:
  - `collect_pending_actions(case_dir, state)` — pulls unresolved blockers, failed leak redactions, and pending unextractable items from existing JSONL sources. Filters out future-phase items (stage-rail enforcement is the source of truth).
  - `score_action(item, state, case_dir, *, recent_decisions)` — five-factor score per the jury synthesis: `0.40·risk + 0.30·sla + 0.15·stage_position − 0.10·fatigue + 0.05·diversity`. Recent decisions read from the hash-chained `audit_events.jsonl` (REVIEWER_DECISION_MADE events).
  - `scored_queue(case_dir, state)` — collect + score + sort descending.
  - `next_best_review(case_dir, state)` — top-scored item, or `None`.
- SLA proximity uses `working/data_subject.json::request_received_date` against the UK GDPR Art 15 30-day window; neutral 0.5 when no SLA data.
- Landing page gains an "Action queue" card showing the top 5 items + score breakdown (`risk 8/10 · 12d to deadline · stage-pos 1 · fatigue −0.33 · div +1.0`) and a "Next Best Review →" deep-link to the top item's screen.
- 12 new tests in `tests/test_action_queue.py` covering collection per source, resolved-item filtering, phase filtering, score ordering by risk + SLA, fatigue penalty after streak, diversity bonus after off-kind streak, next-best pick, empty-queue case, breakdown shape.
- Hermetic count: 383 passing (was 371; +12).
- Version: pre-1.0 MINOR (additive subsystem).

### Followups

- Cache scored_queue between renders if performance degrades at scale (current per-render cost is tiny; revisit when item count > 1k).

## [0.7.1] - 2026-05-26

### Added — stage-rail enforcement on operator-console GET routes

- Forward routes (`/unextractable`, `/leak-review`, `/blockers`, `/release-check`, `/closure-letter`) are gated by the current pipeline phase. Deep-link attempts past the current phase 303 back to `/` with a banner naming both phases (`'Release' isn't reachable yet — case is in 'Discovery'.`). Read-only drilldowns (`/pipeline`, `/audit`, `/file`) stay unguarded.
- New `ROUTE_REQUIRED_PHASE` mapping declares min-phase per route.
- New `current_phase_key(state)` helper resolves stage → phase; logs warning on unknown stage (state-file corruption shouldn't fail silently into a fully-gated console).
- New `is_route_accessible(state, path)` returns `(allowed, message)` so the redirect banner can be self-explanatory.
- 19 new tests in `tests/test_stage_enforcement.py` cover the contract per route × phase.
- Hermetic count: 371 passing (was 352; +19).
- Version: pre-1.0 PATCH (additive route guard; no API changes).

## [0.7.0] - 2026-05-26

### Added — R001-R010 reason-code taxonomy on operator decisions

- New `dsar_orchestrator.local_broker.reason_codes` module defines the 11 codes operators choose from when recording any decision:
  - `R001` Correct DS match
  - `R002` Not DS personal data
  - `R003` Work-context only
  - `R004` Duplicate of reviewed item
  - `R005` Third-party redaction required
  - `R006` Special category — escalate (note required)
  - `R007` Redaction confirmed accurate
  - `R008` Redaction incomplete
  - `R009` Technical extraction issue
  - `R010` Withhold pending legal review (note required)
  - `R-PENDING` Pending classification (note required + auto-escalates after 24 h)
- `validate_reason_code(code, note)` raises on missing, unknown, or note-required-but-empty.
- `is_r_pending_stale(ts_iso, *, now=None)` flags R-PENDING decisions past the 24-h escalation window; malformed timestamps treated as stale (safer to escalate than swallow).
- Wired into `leak_review.record_decision`, `unextractable.record_decision`, `operator_console.toggle_blocker_resolved` — all now take a required `reason_code` keyword. Validation happens before chain emit; `reason_code` is carried into both the user-visible JSONL row and the hash-chained REVIEWER_DECISION_MADE event.
- 6 UI forms (3 blocker-toggle, 1 each in unextractable accept/reject, 3 in leak-review accept/include/manual-fix) gained a `<select name='reason_code' required>` with the 11 options.
- 3 route handlers (`/api/blocker/toggle`, `/api/unextractable/decide`, `/api/leak-review/decide`) extract `reason_code` from form and surface validation errors via `_LAST_ACTION_RESULT`.
- Backwards compat: historical decisions (pre-v0.7.0) lacking `reason_code` render their existing note + timestamp; new decisions show an R-code badge alongside.
- Hermetic count: 352 passing (was 334; +18).
- Version: pre-1.0 MINOR (additive but breaking API on three internal functions — callers in the same package were updated in this PR; no external API).

### Followups (task #114 + open)

- Compensating REVIEWER_DECISION_FAILED chain event for the reverse-drift case (task #114, deferred from v0.6.1 review).
- Auto-escalation of stale R-PENDING decisions to DPO (notification infra not yet present; `is_r_pending_stale` is ready to feed it).

## [0.6.1] - 2026-05-26

### Added — hash-chained audit emission on operator-console decisions

- New `dsar_orchestrator.local_broker.audit_chain` module wrapping `dsar_pipeline.audit.FileAuditStore.append_event`. Three call sites — `leak_review.record_decision`, `unextractable.record_decision`, `operator_console.toggle_blocker_resolved` — now emit a hash-chained `REVIEWER_DECISION_MADE` event under `<case>/working/audit_events.jsonl` *before* appending to the user-visible decision JSONL.
- **Chain-first ordering:** if the chain emit raises (schema or IO failure), the JSONL row is NOT written. Prevents JSONL/chain drift in the no-emit direction. Reverse-direction drift (chain succeeds, JSONL append then fails) is documented in `audit_chain.py` and left for a follow-up compensating event.
- `resolve_case_id(case_dir)` reads `working/data_subject.json::case_no` (or `case_id`); logs a warning and falls back to `case_dir.name` if missing, malformed, or empty.
- `_EMIT_LOCK` (in-process) + `fcntl.flock` (cross-process advisory lock on `audit_events.jsonl`) guard the read-prev/write-event sequence. Toolkit-side `append_event` callers (e.g. redaction agents) do not yet flock — see audit_chain.py docstring.
- `/api/blocker/toggle` route handler now wraps `toggle_blocker_resolved` in try/except so chain emit failures surface via `_LAST_ACTION_RESULT` (parity with leak-review and unextractable routes).
- 14 new tests in `tests/test_audit_chain.py` cover: first-event prev_hash null, multi-event chain integrity, returned canonical hash, case_id resolution from data_subject.json, fallback to dir name, malformed JSON, null case_no, item_id propagation, chain emission per call site, 20-thread concurrent emit, chain-first invariant (emit failure blocks JSONL write), and end-to-end blocker-toggle ordering.
- Hermetic count: 334 passing (was 320; +14).
- Version: pre-1.0 PATCH (additive; no API breakage).

## [0.6.0] - 2026-05-26

### Added — operator console v2 (web UI) + local_broker helpers

- New `dsar-operator-console` CLI script (entry point in `pyproject.toml`). Starts a stdlib `ThreadingHTTPServer` against a case directory and serves the operator workflow: stage rail, action queue, pipeline drilldown with auto-generated writer-model RAG summaries, dedupe findings, approver verdict view, blockers checklist, unextractable review, leak review, closure-letter auto-draft, audit log viewer.
- New package `dsar_orchestrator.local_broker/` with five helpers:
  - `stage_summariser.py` — writer-model RAG summary per pipeline stage with PII redaction (`_redact_pii`, `_redact_path`), broker eviction risk check, sha256-based cache invalidation.
  - `dedupe_filter.py` — `canonical_refs(case_dir)` returns the canonical doc-ref set (None = no dedupe yet; empty = warn; non-empty = filter). `INCLUDE_DUPLICATES=1` env override.
  - `closure_letter.py` — `compute_funnel()`, `readiness_state()`, `draft_letter()`. Auto-generates the DSAR closure letter from case state with ICO-careful wording (e.g. "incidentally mentioned but not substantively relating" rather than "work context").
  - `unextractable.py` — diffs `agent01_input.jsonl` vs `ingested_items.jsonl`; per-row decisions `accept` / `reject` / `retry` (re-invoking `dsar_pipeline.ingest_v3.ingest`). Writes to `audit/unextractable_decisions.jsonl`.
  - `leak_review.py` — lists `status=failed` redactions; per-row decisions `accept_exclude` / `include_with_note` / `retried_ok|fail` / `manual_fix_done`. `retry_redaction()` re-invokes `redact_document` (cwd-sensitive — chdirs into case_dir first). Writes to `audit/leak_review_decisions.jsonl`.
- Console reads JSONL artefacts directly from `<case>/working/` and `<case>/audit/`; no DB. State is per-case via `CaseContext`.
- Hermetic count: still 320 passing — console code is currently uncovered by tests (known gap; v3 PRs add coverage per surface).
- Version: pre-1.0 MINOR (purely additive subsystem; no existing CLI / module changes).
- Foundation for the v3 console build (stage-rail enforcement, reason codes, audit hash chain, QA flow).

## [0.5.0] - 2026-05-25

### Added — operator opt-in for flag resolution on real cases (closes #26)

- New `--resolve-flags-as <true|false>` CLI flag (also `DSAR_RESOLVE_FLAGS_AS` env var). When set, conductor auto-resolves all detect-stage `redact:"flag"` entries to the given value before invoking bake. Operator explicit opt-in for non-interactive runs (different from `cfg.synthetic` which is implicit).
- New `resolve_flags_as: str | None` field on `CaseConfig` (loaded from env / case_config.json).
- New pre-bake gate `_halt_on_pending_flags` in `adapters/bake.py`: when `cfg.synthetic=False` AND `resolve_flags_as=None` AND any flags pending → halt with actionable `PipelineHalt` message listing entity flag count + doc note count and pointing the operator at the two options (manual edit or `--resolve-flags-as`). Closes the operator UX gap left over from #18.
- New `_count_pending_flags(case_path) -> (entity_count, notes_count)` helper for tests + future UI integrations.
- New `_resolve_all_flags_to(case_path, target: bool)` helper — non-synth variant of the synthetic auto-resolve. Reuses the register.json::notes clear logic.
- `PRODUCER_VERSION` on `adapters/bake.py` bumped to 0.5.0.
- 4 new tests: opt-in-resolves-to-false, opt-in-resolves-to-true, no-flags-no-halt, _count_pending_flags counts both sources.
- 2 existing "non-synth preserves" tests updated: now assert the halt + intact-state behaviour.
- Hermetic count: 320 passing (was 316; +4).
- Version: pre-1.0 MINOR (additive CLI flag + additive CaseConfig field + new gate behaviour gated behind opt-in defaults).

## [0.4.9] - 2026-05-25

### Fixed — subprocess PATH-robustness (closes #15)

- Conductor adapters that shell out to toolkit CLIs (`dsar-redact`, `dsar-bake`, `dsar-scope-check`, `python -m dsar_pipeline.{ingest,detect,export}`) previously inherited PATH from `os.environ`. On hosts where the toolkit is ALSO installed via homebrew/system pip, the system-installed shims in `/opt/homebrew/bin/` shadow the venv copy via PATH ordering — bug fixes in the venv toolkit get silently shadowed.
- New `src/dsar_orchestrator/subprocess_env.py::build_subprocess_env()` returns a copy of `os.environ` with `sys.executable`'s bin dir prepended to PATH. Idempotent; stdlib-only leaf module.
- 6 adapters updated to use it: `ingest`, `detect_2_1_to_2_4`, `scope_classify`, `redact`, `bake`, `export`. (verify_pdf + verify_spec use lazy Python imports, not subprocess; unaffected.)
- All 13 adapter `PRODUCER_VERSION` strings bumped to `0.4.9` in lockstep per VERSIONING.md §3.
- 5 new tests in `test_subprocess_env.py` covering: prepends-venv-bin, idempotent-when-first, empty-PATH, copy-not-reference, preserves-other-vars.
- Hermetic count: 316 passing (was 311; +5).
- Operator impact: `dsar-conductor` now works without `PATH=...venv/bin:$PATH` prefix even when the homebrew toolkit shim is present.

## [0.4.8] - 2026-05-24

### Fixed — check_verify_pdf warning on synthetic cases

- Paired with v0.4.7's `verify_pdf` adapter synth-tolerance. When the adapter prints a warning and continues on `cfg.synthetic` (because gate_audit_completeness + gate_structural legitimately can't pass without operator workflow), the conductor's `check_verify_pdf` module agent must also downgrade from critical to warning — otherwise it re-flags the same findings immediately.
- Both halves needed to land for the cross-test to actually clear verify_pdf on synth cases. (Caught during cross-test iteration; should've been one PR.)

## [0.4.7] - 2026-05-24

### Fixed — verify_pdf adapter tolerates failures on synthetic cases

- Synthetic cases legitimately fail `gate_audit_completeness` (no operator decisions log) and `gate_structural` (no `draft/` disclosure pack) because there's no operator workflow. Adapter now prints a warning and returns instead of raising `PipelineHalt` when `cfg.synthetic` is True. Real operator cases halt as before — verify_pdf is the safety net we don't bypass.

## [0.4.6] - 2026-05-24

### Fixed — bake adapter sets DSAR_AUTO_SIGNOFF=1 on synthetic cases

- Paired with toolkit v0.3.2 which added the `DSAR_AUTO_SIGNOFF=1` env var that auto-writes a synthetic signoff after redact. Conductor's bake adapter sets it when `cfg.synthetic` is True.
- Real operator cases (`cfg.synthetic=False`) unchanged — still need explicit `dsar-pipeline --signoff '<reviewer>'`.
- `PRODUCER_VERSION` on `adapters/bake.py` bumped to 0.4.6.

## [0.4.5] - 2026-05-24

### Fixed — bake adapter skips toolkit MRA post-stage hooks

- After v0.4.4 cleared synthetic flags, bake's legacy `redact_all` invoked the toolkit's MRA post-stage hook which raises `ModuleNotFoundError: No module named 'module_agents'` (a toolkit-internal package not part of the conductor's runtime contract). Toolkit provides `DSAR_PIPELINE_SKIP_MRA=1` as a documented opt-out; conductor's bake adapter now sets it.
- MRA hooks are toolkit-internal dashboard health checks. The conductor's own `check_<stage>` module agents cover the validation we actually need.
- `PRODUCER_VERSION` on `adapters/bake.py` bumped to 0.4.5.
- No new tests (env-var addition; verified via cross-test).

## [0.4.4] - 2026-05-24

### Fixed — synthetic-flag helper also clears register.json::notes (#18 round 2)

- v0.4.3 cleared `redact: "flag"` entries in `*_tags.json` but legacy `redact_all` also checks `register.json::notes` for the string `"flagged for review"` (set by the toolkit's detect stage when entities are flagged). Cross-test still halted: "Items flagged for review remain unresolved".
- Extended `_auto_resolve_synthetic_flags` to clear register.json `notes` fields containing "flagged for review" when `cfg.synthetic` is True. Contract A invariant respected (register.json mutation scoped to synth only; conductor metadata stays in the sidecar).
- New helper `_clear_synthetic_register_notes` (atomic write).
- 2 new tests: synth-clears-notes, non-synth-preserves-notes.
- `PRODUCER_VERSION` on `adapters/bake.py` bumped to 0.4.4.
- Hermetic count: 311 passing (was 309).

## [0.4.3] - 2026-05-24

### Fixed — auto-resolve detect flags on synthetic cases (closes #18)

- The toolkit's bake stage delegates to legacy `redact_all` which refuses to ship while any `*_tags.json` entity has `redact: "flag"` (third-party items the detect stage couldn't auto-classify). Real operator workflow expects a human to inspect each and set `redact: true|false`. Synthetic cases have no operator, halting the cross-test indefinitely.
- New `synthetic: bool` field on `CaseConfig` (loaded from `case_config.json::synthetic`, set by `dsar-synthesize-case`).
- `adapters/bake.py::_auto_resolve_synthetic_flags` walks `working/*_tags.json` and rewrites every `redact: "flag"` to `redact: false` when `cfg.synthetic` is True. Atomic per-file writes. Called by `run_for_case` before invoking `dsar-bake`.
- Real operator cases (`cfg.synthetic=False`) are unaffected — flag resolution remains their explicit call.
- `PRODUCER_VERSION` on `adapters/bake.py` bumped to 0.4.3.
- 3 new tests in `test_adapter_bake.py`: synthetic-rewrites-flag, non-synthetic-leaves-intact, synthetic-no-tags-no-op.
- Hermetic count: 309 passing (was 306).
- Decision rationale: Option 1 from #18 (conductor-side, scoped to synthetic). Operator-facing flag-resolution UX is a separate concern; this fix is surgical and unblocks Contract B cross-test bake stage.

## [0.4.2] - 2026-05-24

### Fixed — ingest adapter writes data_subject.json from subject_identifier

- The toolkit's bake (and redact) stages read `working/data_subject.json` with a `full_name` field. The conductor's `case_config.json` instead carries `subject_identifier.primary_name`. Synthetic cases and operator-created cases generally don't write `data_subject.json`; the toolkit's bake exits 3 with "data_subject.json missing or no full_name field".
- `adapters/ingest.py` now writes `working/data_subject.json` from `cfg.subject_identifier` on every run: `{full_name, aliases, dob?, employee_id?}`. Atomic write; idempotent. `PRODUCER_VERSION` bumped to 0.4.2.
- 3 new tests in `test_adapter_ingest.py`: full payload, optional-fields-omitted, subject-identifier-missing-skips-write.
- Hermetic count: 306 passing (was 303).
- Unblocks Contract B cross-test bake stage.

## [0.4.1] - 2026-05-24

### Fixed — check_verify_spec distinguishes missing from empty

- `module_agents.check_verify_spec` previously treated both "audit file missing" and "audit file empty" as critical halts. The toolkit's `verify_for_conductor` always writes the audit log even when there are 0 failures (so empty file = "ran cleanly"). Conductor now: MISSING file → critical (toolkit didn't run); EMPTY file → ok (toolkit ran with 0 failures); HIGH severity rows → critical (unhandled findings); non-HIGH rows only → ok. Mirror of Contract B #12's smart-empty pii_classify pattern.
- 4 new tests in `tests/test_module_agents.py`: missing-critical, empty-ok, non-high-ok, high-critical.
- Hermetic count: 303 passing (was 299).
- Coordinates with: harkers/dsar-toolkit#125 (v0.3.1) which paired this side of the fence.

## [0.4.0] - 2026-05-24

### Changed — Contract B (issues #10/#11/#12)

- **BREAKING (pre-1.0 waiver):** Removed `pii_discovery` stage from `stage_2_parallel` (closes #10). The toolkit doesn't ship `dsar_pii_discovery.core`; the discovery functionality is folded into `dsar_pii_classifier.core.discover_case()` which the pii_classify stage already calls. `pii_discovery` no longer a valid `--only` target. `discovery_enabled` config field kept as deprecated no-op for one release; removal target = v0.5.0.
- Rewired `_run_scope_filter_chain` rerank branch to use new `adapters/rerank.py` (closes #11). The conductor was lazy-importing the non-existent `dsar_rerank.core`. New adapter calls `dsar_clients.tei_rerank_client.rerank_pairs(query=case_scope, docs=[texts])` directly — mirror of the embed adapter's existing tei-client rewire.
- `check_pii_classify` now tolerates empty `pii_collection.jsonl` when scope_classify produced zero `"present"` verdicts (closes #12, interim). Halts critical only when ≥1 docs are in-scope and PII findings missing. Filed harkers/dsar-toolkit#120 for the long-term aggregation fix; conductor follow-up issue dsar-orchestrator#13 tracks the pivot when toolkit lands aggregation.

### Added — Contract B principle (durable)

- `VERSIONING.md §4` *Toolkit-coupling contract*: every conductor lazy-import target must exist in the toolkit; every adapter writes what consumers + agents expect; new adapters must be exercised by the real-toolkit smoke test.
- `tests/test_contract_b_no_fictional_modules.py` — AST-walk enforcement under `@pytest.mark.needs_toolkit` plus a non-gated walker-sanity test.
- `tests/integration/test_real_toolkit_smoke.py` now exports `EXPECTED_TOOLKIT_MODULES` documenting the intended toolkit-module set.
- Contract B pointer added to `src/dsar_orchestrator/__init__.py` module docstring.

### Added — new adapter

- `src/dsar_orchestrator/adapters/rerank.py`. Mirror of the embed adapter pattern: injectable client protocol, `working/cosine_prefilter.jsonl` → `working/scope_rerank.jsonl` with cascade-correct upstream_hash. Retires when toolkit ships `dsar_pipeline.rerank.run_for_case`.

### Tests

- 5 new tests in `tests/test_adapter_rerank.py` covering happy path, threshold edge, empty input, client error, missing prerequisite.
- 3 new tests in `tests/test_module_agent_pii_classify.py` covering smart-empty tolerance.
- 2 new tests in `tests/test_contract_b_no_fictional_modules.py` (AST walker sanity + the gated `needs_toolkit` enforcement).
- Removed: 4 pii_discovery-specific tests across `test_stages.py`, `test_module_agents.py`, `test_config.py` plus assertions in `test_synthetic_case_100.py`, `test_full_pipeline_with_stubs.py`, `test_real_toolkit_smoke.py`.
- Hermetic baseline: 297 passing (was 293).

### Coordination

- Toolkit-side issue filed: harkers/dsar-toolkit#120 (`pii_classifier_stage: write working/pii_collection.jsonl aggregating per-stage findings`). Conductor v0.4.0 ships interim smart-empty tolerance; conductor's `adapters/pii_classify.py` pivots to consume the toolkit's aggregated file in a follow-up release (tracked as dsar-orchestrator#13).

### Fixed — 0.1.1 (issue #8: register.json shape)
- Closes #8 — conductor's `register.json` consumers were assuming a dict envelope `{refs: [...], upstream_hash, schema_version, producer_version}` but the real toolkit writes a **flat list** of file-record dicts. End-to-end runs crashed at ingest with `AttributeError: 'list' object has no attribute 'get'`. Hermetic tests passed because the in-test stubs synthesised dict-shape registers.
- **Contract A**: conductor adapts to the toolkit's flat-list shape. Conductor-owned metadata (`upstream_hash`, `schema_version`, `producer_version`) moves to a sibling file `working/register_meta.json` written by the conductor's ingest adapter.
- 9 source sites updated across `hash_chain.py`, `adapters/ingest.py`, `adapters/embed.py`, `module_agents.py`, `stages.py`. New leaf module `register.py` houses the shape helpers (`read_register`, `text_path_for_ref`, `read_register_meta`, `write_register_meta`) so module_agents and hash_chain can share them without violating import-linter contract 7.
- `STAGE_ARTEFACTS["ingest"]` cascade anchor moves from `working/register.json` (toolkit-owned) to `working/register_meta.json` (conductor-owned).
- Hermetic test fixtures across 10 test files updated to produce the toolkit's flat-list shape — prevents drift from re-introducing the bug.
- New `tests/integration/test_real_toolkit_smoke.py` (gated behind `@pytest.mark.needs_toolkit`) exercises the conductor's ingest adapter against the real toolkit; would have caught the bug on first run. Self-skips when toolkit / TEI / spaCy model not available.

### Added — v5.5 (rollout B phase 2)
- New `verify_spec` coarse stage (Stage 7 in the new 10-stage numbering) — pre-bake plan-level verifier. New `adapters/verify_spec.py` lazy-imports `dsar_pipeline.verify_spec.verify_for_conductor` (toolkit-shipped 2026-05-24); halt message includes the toolkit's `audit_log_path` field. Always-on (no enable flag — operators skip via `--from bake` or later).
- New `check_verify_spec` module-agent validator + registry entry. Mirror of `check_verify_pdf` at the new pre-bake stage.
- New `make_verify_spec_stub` in `tests/_toolkit_stubs/stubs.py`. Writes audit rows in the real toolkit's verify_spec shape (check/ref/severity/issue/…); `upstream_hash` at top level so resume cascade reads it.
- `log_analyser/collectors.py` extended: `WORKING_KNOWN_LOGS` now includes `verify_spec_findings.jsonl`; `basic_stats.verify_failed_count` counts severity-high rows across both files (spec + post-bake).
- 6 new tests in `tests/test_adapter_verify_spec.py` covering happy path, failure halt, audit_log_path in message, resume hint, missing-optional-fields tolerance.

### Changed — v5.5
- **BREAKING (pre-1.0 waiver applies):** Stage numbering shifts again — bake is now Stage 8 (was 7), verify_pdf is Stage 9 (was 8), export is Stage 10 (was 9). Resume cascade for in-flight v5.0 cases is not preserved; restart from `--from redact`.
- All adapter `PRODUCER_VERSION` strings bumped to `0.3.0` in lockstep per VERSIONING.md §3 (the `<package_version>` portion tracks the conductor's `__version__`).
- Conductor version: `0.2.0` → `0.3.0` (MINOR per pre-1.0 waiver: additive new stage + breaking on stage numbering shift).

### Added — v5.0 (rollout B phase 1)
- `VERSIONING.md` documenting the package/schema/producer version policy.
- `CHANGELOG.md` (this file).
- `docs/superpowers/brainstorms/2026-05-24-v5-paused-notes.md` capturing the in-flight v5 pipeline-orchestration brainstorm.
- New `bake` coarse stage (Stage 7 in v5.0; Stage 8 after v5.5) — extracted from the export adapter. New `adapters/bake.py` subprocess wrapper around `dsar-bake --case <id>`. Writes cascade-anchor manifest at `working/redact_v4/bake_manifest.json`.
- New `adapters/verify_pdf.py` (renamed from `adapters/redact_verify.py`) — rewired to the real `dsar_pipeline.post_bake_verify.verify_for_conductor` toolkit entry. Halt message now includes the toolkit's `audit_log_path` field.
- New `check_bake` module-agent validator + registry entry.
- New `check_verify_pdf` module-agent validator (renamed from `check_redact_verify`).

### Changed
- **BREAKING (pre-1.0 waiver applies):** Stage `redact_verify` renamed to `verify_pdf`. `--from redact_verify` / `--only redact_verify` no longer accepted by `dsar-conductor`; use `--from verify_pdf` instead.
- **BREAKING (pre-1.0 waiver applies):** Stage numbering shifts — `export` is now Stage 9 (was Stage 8); `verify_pdf` is Stage 8 (was redact_verify at Stage 7); `bake` is the new Stage 7 (was inside Stage 8 export). Resume cascade for in-flight v4 cases is not preserved; restart from `--from redact`.
- Verify stage now runs **after** bake (was before), so `dsar_pipeline.post_bake_verify.verify_for_conductor` can actually see `<case>/redacted/`. Closes #1.
- `STAGE_ARTEFACTS["verify_pdf"].artefact_relpath` updates to `working/post_bake_findings.jsonl` (toolkit-owned write target), replacing the v4 `~/.dsar-audit/<case>/redact_verify.jsonl` path.
- `adapters/export.py` slimmed — no longer invokes `dsar-bake`; only runs `python -m dsar_pipeline.export`. Manifest at `output/manifest.json` unchanged.
- `PRODUCER_VERSION` strings in `verify_pdf`, `bake`, and `export` adapters bumped to `0.2.0`.

### Fixed
- #1 — `adapters/redact_verify.py` no longer imports the fictional `dsar_redact_verify.core` module. Toolkit ships `dsar_pipeline.post_bake_verify.verify_for_conductor` as of 2026-05-24; adapter rewired (closed by the rename to verify_pdf).

### Coordination
- Requires dsar-toolkit at HEAD or a release tag including the merged `dsar_pipeline.post_bake_verify.verify_for_conductor` + `dsar_pipeline.verifier_verdict.Verdict` (4-field) work. If toolkit hasn't cut such a tag at conductor PR merge time, the `pyproject.toml` pin stays at `dsar-pipeline >= 0.2.0` and an editable install at toolkit HEAD is the operator's responsibility.

## [0.1.0] - 2026-05-23

Initial tagged release. State as of commit `cd1594f` (immediately after
the v4 adapter sprint).

### Added
- 8-coarse-stage DAG (`ingest → embed → detect → people_register →
  scope_prefilter → rerank → scope_classify → pii_classify → redact →
  redact_verify → export`).
- `dsar-conductor` CLI with `--check`, `--force`, `--from`, `--only`,
  `--acknowledge-issues`.
- v4 adapter layer: 10 adapters under `src/dsar_orchestrator/adapters/`
  with single-injectable-dependency contract and per-adapter retirement
  triggers. See `docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v4.md`.
- Resume cascade via `upstream_hash` chain on every artefact row.
- Module-agent validation framework (`src/dsar_orchestrator/module_agents.py`).
- Log analyser with critical-finding block flag (`src/dsar_orchestrator/log_analyser/`).
- Synthetic-case generator (`dsar-synthesize-case` CLI).
- Local LLM audit-log reviewer (`dsar-analyse-logs` CLI; mlx-broker-backed).
- 282 passing tests; 9 import-linter contracts.
- Schema and producer-version stamping on every artefact row
  (`SCHEMA_VERSION = "1.0"`, per-module `PRODUCER_VERSION`).

[Unreleased]: https://github.com/harkers/dsar-orchestrator/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/harkers/dsar-orchestrator/compare/v0.3.0...v0.4.0
[0.1.0]: https://github.com/harkers/dsar-orchestrator/releases/tag/v0.1.0
