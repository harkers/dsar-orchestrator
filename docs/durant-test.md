# The Durant Test — How It Works

Reference doc for the biographical-focus test the DSAR conductor uses to
filter the corpus before redaction. Covers the legal foundation, the
programmatic shape (input / reasoning / output), both the toolkit canonical
implementation and the local-broker bypass pattern, and the under-disclosure
recheck layered on top.

Companion code: `dsar-toolkit` provides `gate_durant.py` + `scope_check_stage`.
Local deployments that route to an on-prem MLX broker bypass the toolkit's
default cloud-LLM routing with a thin per-engagement script that consumes
the canonical prompt via the `dsar-prompt` CLI (or a vendored zipapp).

---

## 1. What the Durant test is

The Durant biographical-focus test comes from
*Durant v FSA [2003] EWCA Civ 1746* — a UK Court of Appeal decision on
the scope of a data subject's right of access under DPA 1998 (carried
forward into UK GDPR Art 15 / DPA 2018).

The court held that a document is **personal data of the data subject**
— and therefore disclosable under a subject access request — only when:

- The subject is the **focus** of the document, *or*
- The document is **biographical in a significant sense** about the subject.

Documents where the subject is merely **incidentally mentioned** (cc'd on
an email about other people, named in a routine business list, addressee
on a broadcast newsletter) are **NOT** personal data of the subject and
should not be disclosed under Art 15.

The test matters for two reasons:

1. **Over-disclosure risk:** disclosing documents that aren't about the
   subject exposes third-party personal data without legal basis. The
   data subject doesn't have a right to other people's information.
2. **Under-disclosure risk:** excluding documents that genuinely are
   about the subject is a statutory failure. Under-disclosure is the
   *worse* compliance error — the regulator (ICO) is far more likely to
   sanction missed-disclosure than careful exclusion.

The Durant test sits between coarse responsiveness ("does this match a
keyword search for the subject's name?") and final redaction. It filters
the corpus down to the docs that actually need full PII processing.

---

## 2. The decision rule

For each document, the test returns one of three verdicts:

| Verdict | Meaning | Disclosure decision |
|---|---|---|
| `biographical` | Subject is the focus of the content (their work, decisions, performance, correspondence, identity) | INCLUDE — proceed to redaction |
| `work_context_only` | Subject is peripheral (cc/bcc, routine addressee); topic is about others / unrelated business | EXCLUDE from disclosure pack |
| `ambiguous` | Evidence is mixed; cannot decide cleanly | INCLUDE for safety; flag for operator review |

**Tie-breaker rule:** *default to biographical under uncertainty.*
Under-disclosure is the worse error, so when in doubt the operator (or
downstream review stage) can exclude later — but a doc that was wrongly
excluded never gets seen again.

**Direct-addressee carve-out:** the subject being in the To: line of an
email about a topic that concerns the subject themselves (their contract,
their assignment, their performance review) **IS** `biographical`. The
`work_context_only` verdict is reserved for cases where the subject is a
peripheral cc/bcc on a topic genuinely unrelated to them.

---

## 3. Inputs

Each Durant test invocation consumes:

| Input | Source | Shape |
|---|---|---|
| Document text | `working/<ref>.txt` (extracted by ingest_v3) | UTF-8 string, truncated per §3.1 (model-aware cap; defaults 8,000 chars for local MLX, 32,000 for cloud Opus) |
| Data subject identity | `working/data_subject.json` | `full_name`, `aliases[]`, `email`, `additional_emails[]` |
| Data subject role | `working/data_subject.json` (optional) | `role` (≤100 chars), `role_context` (≤500 chars) — domain visibility hint for the LLM. Sanitised on read (NFKC + invisible-char strip + anti-confusion filter). |
| Doc ref | `working/register.json` entry | unique ref string (case-specific scheme) |

The data subject summary is one line built from `data_subject.json`,
e.g.:

```
name='<full_name>'; aliases=[<aliases>];
primary_email='<email>'; additional_emails=[<additional>]
```

When `role` is set, the USER prompt also includes a "Subject's
organisational role" section + a "How to apply the role" guidance
block. The role context gives the LLM domain visibility but does NOT
automatically make role-domain documents biographical — see §4 for
the precise prompt template.

The LLM never sees the operator-curated `subject_protected_phrases` for
the Durant test itself — those phrases gate **redaction**, not scope.

### 3.1 Truncation strategy

The doc-text input is capped before reaching the LLM. Caps and modes
are model-aware and configured in
`src/dsar_pipeline/config/model_context.json` (toolkit).

| Field | Meaning |
|---|---|
| `max_text_chars` | Character cap. Defaults: 8,000 for `mini@mlx`, 32,000 for `claude-opus-4-7@anthropic`. |
| `target_input_tokens` | Optional. If set + the router has a tokenizer for the model, a token-aware safety belt re-truncates to fit; up to 5 iterations. |

Three modes are available (toolkit `gates/text_truncation.py`):

1. **`head_tail`** (default) — keep `head_ratio × (cap − marker)` chars from
   the start + the rest from the tail, with a `[... N characters elided ...]`
   marker in the middle. `head_ratio` defaults to 0.75 (front-heavy because
   email subject/header is usually load-bearing). Marker size is computed
   via fixed-point iteration so the truncated body fits the cap exactly.
2. **`structure_aware`** (opt-in) — when the document looks like an email
   thread (`_looks_like_email_thread`) and has ≥2 messages, keep the first
   and last message verbatim and elide the middle. Falls back to `head_tail`
   on any structural anomaly.
3. **`none`** — no truncation; raises if the doc exceeds the cap. Used in
   tests; not exposed in production runs.

**Subject-mention audit scan.** After truncation, the toolkit counts
case-insensitive substring matches of `data_subject.full_name`,
`email`, and `additional_emails` in the *elided* range (between
`elided_start` and `elided_end`). The count is recorded as
`subject_mentions_in_elided` in the audit row but **never injected into
the LLM prompt** — it's an operator-review signal for "this truncated
doc dropped material that mentions the subject; flag for human review".

**Audit row fields added (per ref):**

```json
{
  "truncation_mode": "head_tail | structure_aware_email_2msg | none",
  "original_char_count": 27432,
  "truncated_char_count": 7943,
  "subject_mentions_in_elided": 12,
  "token_safety_iterations": 0
}
```

The previous implementation (blind tail-slice) dropped the tail and
silently lost subject mentions there. The current strategy retains both
ends and surfaces lost subject signal to the operator via
`truncate_with_token_check`.

---

## 4. Programmatic approach

```
                                  ┌─────────────────────────┐
                                  │  working/<ref>.txt      │
                                  │  (extracted document)   │
                                  └──────────┬──────────────┘
                                             │
                                             ▼
┌─────────────────────────────────────────────────────────┐
│ truncate_with_token_check(text, …)  (see §3.1)          │
│  - head_tail (default) or structure_aware (opt-in)      │
│  - token-aware safety belt if tokenizer available       │
│  - audit-only subject-mention scan over elided range    │
└──────────┬──────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────┐         ┌─────────────────────────┐
│ data_subject.json    │────────►│ build_user_prompt()     │
│ (full_name, aliases, │         │  - subject summary      │
│  email, role,        │         │  - role + how-to-apply  │
│  role_context, …)    │         │    block if role set    │
│                      │         │  - doc ref              │
└──────────────────────┘         │  - truncated text       │
                                 └──────────┬──────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────┐
│ PromptLoader.load("durant.system")                       │
│  - reads gates/prompts/durant.system.md                  │
│  - verifies canonical_seal_sha256                        │
│  - applies any droppable strips (e.g. placeholder-tokens │
│    when the deployment skips de-identification)          │
│  - returns body + effective_sha256                       │
└──────────┬──────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│ POST /v1/chat/completions   (OpenAI-compat endpoint)     │
│   model: <configured>                                    │
│   system: <loader-resolved system prompt>                │
│   user:   <prompt above>                                 │
│   temperature: 0.0                                       │
│   max_tokens: 400                                        │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼  (response, retry on 500/ConnectError)
┌──────────────────────────────────────────────────────────┐
│ {"durant_verdict": "<bio|work_context_only|ambiguous>",  │
│  "rationale": "<one-or-two sentences>"}                  │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼  (validate + coerce to ambiguous on parse error)
┌──────────────────────────────────────────────────────────┐
│ Append row to working/durant_verdicts.jsonl              │
│ {case_id, doc_ref, durant_verdict, rationale,            │
│  model, prompt_id, prompt_canonical_seal_sha256,         │
│  prompt_applied_strips, prompt_effective_sha256,         │
│  truncation_mode, original_char_count,                   │
│  truncated_char_count, subject_mentions_in_elided,       │
│  token_safety_iterations, elapsed_sec,                   │
│  error_state? (model_unreachable | schema_validation     │
│                _failed | empty_response)}                │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼  (only for primary verdict == work_context_only)
┌──────────────────────────────────────────────────────────┐
│ RecheckStage (toolkit) — calibration-gated by default    │
│  Runs GateDurantRecheck with PromptLoader.load(          │
│   "durant.recheck.system") if mode_effective="always".   │
│  Writes working/recheck_decision.json (canonical "stage  │
│  ran" marker) + working/durant_underdisclosure_recheck   │
│  .jsonl + working/recheck_summary.json (cost telemetry). │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│ Agent22ScopeCheck.synthesise_verdict(...)                │
│   Reads durant_verdicts.jsonl + recheck JSONL +          │
│   recheck_decision.json + temporal verdicts. Computes    │
│   `effective_durant` and synthesises `scope_verdict`     │
│   per §4.6 of the hardening spec. Writes per-ref         │
│   scope_verdicts.jsonl + working/synthesis_summary.json. │
└──────────────────────────────────────────────────────────┘
```

Key properties of the runtime:

- **Two-pass design with calibration gate.** Every doc gets a primary Durant
  call. Refs that the primary classified `work_context_only` are *conditionally*
  re-examined by `GateDurantRecheck` (the under-disclosure safety net, §8).
  The recheck SKIPS only when the operator's calibration cache is 95% confident
  the false-negative rate is below the configured `fn_threshold`. Default is
  "run the recheck" because under-disclosure is the worse error.
- **Strict allowed-values coercion.** Unknown verdicts collapse to `ambiguous`
  so a flaky LLM response can never silently introduce a bad verdict.
- **Error-as-data.** Network failures and parse errors produce an output row
  with `error_state` set and a safe default verdict; nothing throws. **The
  recheck enforces `error_state != null ↔ recheck_verdict == null`** as a
  schema-level oneOf constraint — an errored row never carries a verdict.
- **Resume-safe.** The script re-reads the output file on start, drops any
  errored rows (atomic temp-file + replace), and re-attempts only the missing
  doc refs.
- **Retry-with-backoff.** On HTTP 500 or connection errors (a local MLX
  downstream `mlx_lm.server` can crash and respawn taking 30–60 s), the
  script waits 2 → 8 → 30 → 60 s before giving up on a single record.
- **Prompt integrity.** The system prompt is loaded via `PromptLoader`,
  which verifies the asset's `canonical_seal_sha256` matches the body+metadata
  on every load. A tampered or out-of-sync asset raises `PromptIntegrityError`
  immediately — the run aborts before any LLM cost is incurred.

---

## 5. Outputs

Primary output: `working/durant_verdicts.jsonl` — one row per ingested
document, append-only.

```jsonl
{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"biographical","rationale":"…short sentence citing evidence…","model":"<alias>@<host>","prompt_id":"durant.system","prompt_canonical_seal_sha256":"<hex>","prompt_applied_strips":[],"prompt_effective_sha256":"<hex>","truncation_mode":"head_tail","original_char_count":27432,"truncated_char_count":7943,"subject_mentions_in_elided":3,"token_safety_iterations":0,"elapsed_sec":1.5}
{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"work_context_only","rationale":"…","model":"<alias>@<host>","prompt_id":"durant.system","prompt_canonical_seal_sha256":"<hex>","prompt_applied_strips":[],"prompt_effective_sha256":"<hex>","truncation_mode":"head_tail","original_char_count":1432,"truncated_char_count":1432,"subject_mentions_in_elided":0,"token_safety_iterations":0,"elapsed_sec":1.3}
```

Recheck outputs (only present when the calibration gate ran the safety net):

- **`working/recheck_decision.json`** — single-object JSON; the canonical "did the
  recheck stage run?" marker (read this rather than the JSONL because the
  JSONL is empty when the gate skipped). Records `mode_effective`,
  `mode_requested`, `reason` (e.g. `ci_upper_above_threshold`),
  `calibration_entry_used`, `fn_threshold`, `decided_at`.
- **`working/durant_underdisclosure_recheck.jsonl`** — per-ref recheck verdicts
  (one row per recheck'd `work_context_only` ref). Each row carries
  `recheck_verdict` (`reclassify_to_biographical` | `reclassify_to_ambiguous` |
  `confirmed_work_context_only`) or `error_state` (mutually exclusive).
- **`working/recheck_summary.json`** — cost + count telemetry: `docs_examined`,
  `docs_reclassified_to_biographical`, `docs_reclassified_to_ambiguous`,
  `docs_confirmed_wco`, `errors`, `estimated_cost_usd`, `elapsed_sec_total`.

Downstream consumers:

- **`working/scope_verdicts.jsonl`** is derived from `durant_verdicts.jsonl`
  by `Agent22ScopeCheck.synthesise_verdict` (5-arg form). Each row's
  `evidence` block carries `durant_verdict`, `recheck_verdict`,
  `error_state`, `recheck_mode_effective`, `effective_durant`, and
  `temporal_verdict`. See §6 + §8 for the synthesis semantics.
- **`working/synthesis_summary.json`** records the per-batch counts:
  `recheck_promoted` (WCO→bio), `recheck_escalated` (WCO→amb),
  `recheck_confirmed`, `recheck_errored`, `recheck_missing_anomaly`,
  `primary_wco_recheck_disabled`. Operators consult this to verify
  the safety net is doing useful work for the current
  prompt/model/data combination.
- **Redaction (toolkit stage 7)** filters to `scope_verdict = present` —
  PII tagging + redaction only runs on the biographical set, saving 4–6×
  LLM time vs running on the full ingested corpus.
- **Operator review (toolkit stage 11)** consults `durant_verdicts.jsonl`
  rationale fields when the operator is deciding ambiguous cases.

Secondary outputs:

- `<engagement>/audit/agent-durant-progress.jsonl` — periodic progress rows
  (every 50 docs) with rate, errors, ETA estimate.

---

## 6. Toolkit canonical implementation

Lives in the `dsar-toolkit` repo:

| File | Purpose |
|---|---|
| `src/dsar_pipeline/gates/gate_durant.py` | `GateDurant(BaseGateAgent)`. Loads system prompt via `PromptLoader.load("durant.system")` (no inline constant). Runs `truncate_with_token_check` before composing the user prompt. Defaults to `claude-opus-4-7` via the toolkit's `RoleRouter`. |
| `src/dsar_pipeline/gates/gate_durant_recheck.py` | `GateDurantRecheck(BaseGateAgent)`. Used by `RecheckStage` for the inverse-question safety-net pass. Loads `durant.recheck.system` via the same loader. Does NOT see the primary verdict's rationale (confirmation-bias mitigation). |
| `src/dsar_pipeline/gates/prompt_loader.py` | `PromptLoader.load(prompt_id, strip_sections=…)` + `compute_seal()` + `sign_asset()` + `build_registry()`. Backs the `dsar-prompt` CLI. |
| `src/dsar_pipeline/gates/text_truncation.py` | `truncate()` + `truncate_with_token_check()` + `count_subject_mentions_in_elided()` helpers (see §3.1). |
| `src/dsar_pipeline/gates/prompts/durant.system.md` | The canonical primary-pass system prompt. YAML frontmatter carries `prompt_id`, `version`, `seal_sha256`, `droppable_blocks`. The `placeholder-tokens` block is droppable for deployments that skip de-identification. |
| `src/dsar_pipeline/gates/prompts/durant.recheck.system.md` | The recheck pass's inverse-question system prompt. Same loader semantics. |
| `src/dsar_pipeline/gates/prompts/_registry.json` + `_archive/<id>/<version>.md.gz` | Append-only version history of every signed asset. Used by `dsar-conductor verify --check prompt-versions` to detect runs against retired prompts. |
| `src/dsar_pipeline/recheck_stage.py` | `RecheckStage(BaseStage)`. Calibration-gated orchestration: reads `~/.dsar/calibration_registry.json`, decides `mode_effective`, writes `recheck_decision.json` unconditionally. `dsar-recheck` CLI. |
| `src/dsar_pipeline/scope_check_stage.py` | `ScopeCheckStage(BaseStage)` orchestrates `gate_durant` + `gate_temporal_scope` over a register, writes per-ref `scope_verdicts.jsonl`. Exposes the `dsar-scope-check` CLI. Now also invokes `RecheckStage` after the primary durant pass when the case YAML configures it. |
| `src/dsar_pipeline/agents/agent22_scope_check.py` | JSONL-contract adapter; consumes per-ref gate outputs + recheck JSONL + `recheck_decision.json` + temporal verdicts, and emits the final `scope_verdict`. Synthesis lives in module-level `synthesise_verdict(primary, recheck, recheck_err, recheck_mode, temporal)` returning `(scope, rationale, effective_durant)`. Writes `synthesis_summary.json`. |
| `src/dsar_pipeline/fitness_canary.py` | Implements the `dsar-fitness-canary` CLI. Runs both gates against an operator-curated corpus and writes a Wilson-bounded fitness report (§9.0). |
| `src/dsar_pipeline/config/model_context.json` | Per-model `max_text_chars` + `target_input_tokens` caps. |
| `src/dsar_pipeline/config/pricing.json` | Per-model in/out token pricing (USD per 1k). Feeds `recheck_summary.estimated_cost_usd`. |
| `examples/canary_baseline/` | Toolkit-shipped baseline canary corpus (≥6 Durant-classic patterns). CI verifies its seal against a pinned value. |

The toolkit version handles:
- LLM model routing via Agent 18 `RoleRouter` (default `scope_check` role) — now also exposes `has_token_counter_for(model_alias)` + `count_tokens(model_alias, text)` for the truncation safety belt
- Pre-LLM de-identification (`[PERSON_0]`, `[EMAIL_3]`, etc.) — the model
  sees placeholder tokens instead of raw names, so the prompt has a
  dedicated *"About placeholder tokens in this prompt"* section
  (droppable via `strip_sections=("placeholder-tokens",)` when the deployment
  does NOT pre-deidentify)
- Cost tracking (`cost_estimate_usd = 0.01` per ref)
- Tier 2 gate classification for the toolkit's gate framework

It does NOT handle:
- Direct routing to a local MLX broker (the model alias config is
  Anthropic-cloud oriented)
- The on-prem "no de-identification preprocessor" path
  — but the per-engagement script (§7) can request the droppable
  `placeholder-tokens` strip via the `dsar-prompt` CLI to get a
  loader-issued, hash-verified prompt suitable for that deployment.

---

## 7. Bypass-script consumption (dsar-prompt CLI / vendored zipapp)

For air-gapped / on-prem deployments routing through `mlx-broker`, the
conductor still runs a per-engagement script (kept in the engagement
folder to satisfy per-engagement data-isolation rules), but the script
**no longer copies the prompt body verbatim**. Instead it consumes the
canonical prompt via one of two channels:

1. **Toolkit-installed deployments** — the script invokes the
   `dsar-prompt` CLI shipped by `dsar-toolkit`:

   ```python
   data = subprocess.run(
       ["dsar-prompt", "show", "durant.system",
        "--strip-section", "placeholder-tokens"],
       capture_output=True, check=True,
   ).stdout                                      # bytes
   sep = b"\n# __dsar_prompt_meta__ "
   idx = data.rfind(sep)
   if idx < 0:
       raise PromptIntegrityError("CLI footer missing")
   body_bytes = data[:idx]
   footer_line = data[idx + 1:].rstrip(b"\n").decode("utf-8")
   meta = parse_footer(footer_line)              # parses k=v pairs
   runtime_effective = hashlib.sha256(body_bytes).hexdigest()
   if runtime_effective != meta["effective_sha256"]:
       raise PromptIntegrityError("bypass: runtime hash mismatch")
   system_prompt = body_bytes.decode("utf-8")
   ```

   The footer line carries `canonical_seal_sha256`, `effective_sha256`,
   `applied_strips`, `prompt_id`, `version` — the script verifies the
   runtime SHA-256 of the captured body matches the loader's
   `effective_sha256` and records all five fields in the audit row.

2. **Air-gapped / non-installed deployments** — the engagement folder
   ships `dsar-prompt-vendored.pyz`, a reproducible zipapp built by
   `bin/build-vendored-zipapp` (`SOURCE_DATE_EPOCH=0`, sorted file
   ordering, hardcoded gzip `mtime=0`, vendored `PyYAML`). The zipapp
   bundles `prompt_loader.py`, the `dsar-prompt` entry script, every
   `prompts/*.md` from the source toolkit version, `_registry.json`,
   and a `VENDOR_MANIFEST.json` recording the toolkit version and
   included-prompts manifest. The consumption protocol is identical
   to (1) — invoke as `python dsar-prompt-vendored.pyz show
   durant.system --strip-section placeholder-tokens`, parse the same
   footer, verify the same hash.

The audit row's `prompt_source` field records which channel was used
(`"toolkit_cli"` or `"vendored@<toolkit_version>"`); the
`prompt_canonical_seal_sha256`, `prompt_applied_strips`, and
`prompt_effective_sha256` fields are populated from the footer.

`dsar-conductor verify --check prompt-versions <case_dir>` cross-checks
audit rows against `_registry.json` in the installed toolkit; mismatches
exit 2, runs against retired-but-registered versions warn (or fail with
`--strict`).

**No more drift risk from copy-paste.** The earlier "diff at release
time" pattern (with an inline provenance comment marking the prompt
source file) is retired. Drift is now caught at runtime by the seal
check and post-hoc by the conductor verify subcommand.

---

## 8. Under-disclosure recheck (the safety net)

Layered on top of the primary Durant pass: a calibration-gated second
LLM pass (`RecheckStage` + `GateDurantRecheck` in the toolkit) that
re-examines `work_context_only` verdicts by asking the *inverse*
question. The stage is first-class — it has its own
`stage_label="durant_recheck"`, its own per-case YAML block, its own
telemetry file, and its own `dsar-recheck` CLI. (It used to live only
in per-engagement bypass scripts.)

**Calibration gating.** The recheck does not run unconditionally. Per
the case YAML's `recheck:` block (mode `auto` | `always` | `never`),
the stage consults `~/.dsar/calibration_registry.json` for an entry
matching `(deployment_id, model_alias, primary_seal, recheck_seal)`
and decides:

- `mode: auto` — runs the recheck UNLESS the cache says we're 95%
  confident the false-negative rate is below the configured
  `fn_threshold` (default 0.10). Specifically: only skip when
  `fn_rate_ci95[1] <= fn_threshold`. Wide CI → recheck runs.
- `mode: always` — runs unconditionally.
- `mode: never` — skipped. The case YAML must supply a non-blank
  `override_reason`; `ConfigError` at stage init otherwise.

The decision (including the reason — `mode_set_explicit`,
`calibration_cache_miss`, `calibration_stale`,
`calibration_prompt_seal_drift`, `ci_upper_above_threshold`,
`ci_upper_below_threshold`) is recorded in
`working/recheck_decision.json` — the canonical "stage ran" marker.
Distributed deployments syncing the working directory should key off
this file, not the (possibly empty) JSONL.

**Outputs:**

- `working/recheck_decision.json` — decision + reason + entry used
- `working/durant_underdisclosure_recheck.jsonl` — per-ref results
  (empty when mode_effective="never")
- `working/recheck_summary.json` — counts + USD cost estimate from
  `pricing.json`

Recheck verdicts:

| Verdict | Meaning | Downstream effect |
|---|---|---|
| `confirmed_work_context_only` | Original Durant was right; keep excluded | No change |
| `reclassify_to_biographical` | Original Durant was wrong; ADD to disclosure | Pull back into the redaction set + operator review |
| `reclassify_to_ambiguous` | Genuine uncertainty | Operator review |

**Mutual-exclusion contract.** Every row in the recheck JSONL
satisfies the invariant `error_state != null ↔ recheck_verdict == null`
(schema-enforced via `oneOf` in
`schemas/durant_recheck_row.schema.json`). An errored row carries
`error_state.code` (one of `model_unreachable`, `schema_validation_failed`,
`empty_response`, `timeout`, `unknown`), a `message`, and a
sanitised `raw` field (≤200 chars, credential patterns redacted via
`_sanitise_raw`). Errored rows are treated as `reclassify_to_ambiguous`
downstream — under-disclosure safety requires "I'm not sure" to
escalate, not silently confirm.

Why a separate pass instead of tuning the primary prompt:

1. **Asymmetric error costs.** Under-disclosure is the worse legal error.
   A second pass with an inverse question framing catches cases the
   primary pass dismissed too quickly.
2. **Confirmation-bias mitigation.** The recheck deliberately does NOT
   see the original Durant rationale; it re-evaluates the document
   independently and any disagreement surfaces as a reclassification
   candidate. (The original rationale is kept in the per-row audit record
   so the chain of reasoning is reconstructible — just not in the prompt.)
3. **Error defaults flag-rather-than-confirm.** The mutual-exclusion
   contract above plus Agent22's `effective_durant("recheck_errored:
   <code>")` mapping ensure errored rechecks become ambiguous scope
   verdicts. For an under-disclosure SAFETY check, "I'm not sure" must
   escalate, not silently agree.

In practice the recheck reclassifies a meaningful fraction (observed
~60% in one real case) of supposedly-excluded docs as candidate-biographical
or ambiguous. That is the safety net working: the primary pass's
false-negative rate was high enough that the operator would otherwise
never have seen those documents.

---

## 9.0 Model-fitness canary (pre-flight)

`dsar-conductor run` aborts before the first stage if the deployed
prompt+model+config tuple hasn't passed a recent fitness canary. The
canary is the upfront fitness check that catches a
poorly-calibrated small model BEFORE it does real damage on a real
case (in contrast to §9.1 calibration, which is *post-hoc*).

**Canary corpus.** Per-machine, operator-curated under
`~/.dsar/canary_sets/<deployment_id>/`:

```
canary_corpus.json     # {"version":1, "baseline_version":"...", "refs":[...]}
refs/<ref>.txt
truth.json             # {"<ref>": "biographical|work_context_only|ambiguous", ...}
```

The toolkit ships `examples/canary_baseline/` with ≥6 Durant-classic
patterns (clear bio, clear WCO, direct-addressee carve-out,
mixed-ambiguous, long-thread-tail mention, signature-only mention).
CI verifies the baseline corpus's seal against a pinned value;
edits require a version-bump.

**Runner.** `dsar-fitness-canary --deployment-id <id> [--corpus-path
<path>]` runs the primary `GateDurant` and (if recheck is configured)
`GateDurantRecheck` against the canary corpus. Output is a Wilson-bounded
fitness report at `~/.dsar/fitness_reports/<deployment_id>/<ts>.json`
containing the full tuple
`(deployment_id, model_alias, primary_seal, recheck_seal,
corpus_sha256, inference_params_sha)`, structured `metrics`
(`agreement`, `agreement_wilson_lower`, `fn_rate`,
`fn_rate_wilson_upper`, `fp_rate`, `fp_rate_wilson_upper`,
`success_rate`, `ambiguous_rate_on_definite_truth`), the case YAML's
`criteria`, `passed: bool`, structured `fails: [{code, kind:
"corpus|model", detail}]`, and a `per_ref` array.

The full `fitness_canary` machinery (`fn_rate_ci95`, Wilson bounds,
class-eligibility thresholds) is the load-bearing implementation of
this pre-flight check — see the toolkit's `fitness_canary.py`.

**Fitness criteria (per-case YAML).** Pass requires ALL of:

```yaml
fitness:
  min_agreement: 0.80           # wilson_lower(agreement) >= this
  max_fn_rate: 0.20             # wilson_upper(fn_rate) <= this
  max_fp_rate: 0.20
  max_ambiguous_ratio: 0.20
  min_success_rate: 0.85
  required_corpus_min_size: 30
  min_class_eligible: 12        # each of bio/WCO must have ≥12 refs
```

Wilson 90% bounds; explicit zero-denominator guards (return `None` when
n=0; the class-size check fires instead). LLM errors are decoupled —
counted toward `success_rate` only, never as false negatives
(error rate is infrastructure-fitness, not model-fitness).

**Conductor pre-flight.** `dsar-conductor run <case_dir>` computes the
live tuple `(deployment_id, model_alias, primary_seal, recheck_seal,
live_corpus_sha, inference_params_sha)`, finds a matching report,
and aborts on:

- canary set path not found
- canary corpus invalid (truth.json malformed, files missing)
- report not found for tuple
- report's `corpus_sha256` differs from `live_corpus_sha` (drift guard)
- report older than `case_cfg.fitness_check.max_report_age_days`
- `report.passed == False` (lists structured fails; operator
  distinguishes `kind=corpus` "expand canary" vs `kind=model`
  "improve prompt/model")

`--auto-fitness` (opt-in) makes the conductor run the canary inline on
miss/stale/fail before proceeding. `--force-skip-fitness "<non-blank
reason>"` bypasses the gate; the reason + `os_user` + `hostname` +
`timestamp` + fitness tuple are recorded in
`case_audit/skip_fitness.json`. CLI rejects empty reasons.

`compute_corpus_sha256(path)` requires both `canonical_corpus.json` and
`truth.json`, validates `truth.json` is a non-empty JSON object,
deduplicates the explicit list ∪ `refs/*.txt` glob, canonicalises
`.json` files (`json.dumps(sort_keys, separators)`) before hashing,
and normalises line endings — cosmetic edits don't break the seal.

---

## 9.1 Calibration (post-hoc)

§9.0's canary is the *pre-flight* fitness gate (does the deployment
meet minimum quality before we touch a real case?). Calibration is the
complementary *post-hoc* check on a finished real case: how did the
verdicts compare against a stratified human review?

The conductor does not assume LLM verdicts are ground truth. A separate
**operator-calibration portal** serves a stratified document sample for
manual review and produces a per-stratum agreement report:

- Stratum A: disputed — original=`work_context_only`, recheck=`reclassify_to_biographical`
- Stratum B: agreed-exclude — both passes say `work_context_only`
- Stratum C: recheck-ambiguous — recheck=`reclassify_to_ambiguous`
- Stratum D: originally-biographical — validate the positive set

Operator decisions land in `working/operator_calibration_<N>.jsonl`. A
`/report` endpoint computes agreement rates per stratum and overall:
operator vs original Durant, operator vs recheck. Output of that report
is the empirical accuracy estimate for the deployment's data + model
combo. The portal also writes back to
`~/.dsar/calibration_registry.json` (the same file §8's recheck reads),
populating `fn_rate`, `fn_rate_ci95`, `sample_size`, `calibrated_at`,
and the two prompt seal hashes. From the next case onwards, this entry
gates whether the recheck runs or skips.

If post-hoc agreement is low in either direction, the right answer is
to refine the prompt (bump version + re-sign) or change the model and
rerun §9.0 to validate the new tuple before committing to operator
review on the full set.

---

## 10. Known issues / drift risks

The originally-documented drifts (prompt-copy duplication; tail-cut truncation
dropping biographical signal; unconditional recheck cost; no upfront fitness
gate; role-missing-from-data-subject; recheck-not-propagated-to-scope) were
mitigated by §§4.1–4.6 of the durant-pipeline-hardening spec. The new residuals
below are the documented limitations of the post-hardening design.

| Issue | Impact | Mitigation |
|---|---|---|
| `mlx_lm.server` mid-run crashes return HTTP 500 | Sustained passes can have very high error rates after a crash | Retry-with-backoff (2 → 8 → 30 → 60 s) absorbs transient failures; resume-cleanup re-attempts errored rows on rerun. |
| Loading other models evicts the primary model mid-pass | Sustained durant pass fails after model eviction | Operator discipline: no other broker calls during long passes; or pin models if the broker supports it. |
| Small models produce JSON with the right keys but occasionally not the right shape | One-off bad rows | Strict allowed-values coercion; bad responses default to `ambiguous`; rationale field captures the raw model output for audit. |
| `data_subject.role_context` sanitiser is anti-confusion, not security | Homoglyph substitution (Cyrillic ѕ vs Latin s), linguistic paraphrase of "ignore previous instructions", markdown-structural injection in `role_context` — none are caught by the regex/Unicode filters | Documented residual; threat model is accidental drift, not adversarial input. Operator-curated `role_context` is the only entry point. |
| NFKC normalisation in the role-field sanitiser may merge visually-distinct characters (e.g. ﬁ → fi) | Rare false rejection if the merged form trips the injection-pattern regex; could also rarely change the prompt's effective length post-cap | Operator rephrases when sanitiser rejects; truncation is generous enough that small length shifts are absorbed. |
| `_iter_jsonl_safe` (Agent22's recheck-index builder) is a streaming generator — mid-stream `OSError` yields a partial index | Local FS: vanishingly rare. Network FS: partial recheck-by-ref index could silently treat refs as "missing recheck" → `ambiguous` scope. | Acceptable under operator-trust threat model; documented for distributed deployments. The unified `_build_index_first_wins` logs OSError + falls back to empty dict, so the failure is operator-visible. |
| Bypass-script vendored zipapp must stay in sync with toolkit's `_registry.json` | If the engagement folder ships a stale zipapp, runs use a retired prompt version | `dsar-conductor verify --check prompt-versions` exits non-zero on retired-but-registered versions (or warns without `--strict`); zipapp's `VENDOR_MANIFEST.json` records the toolkit version. |
| `dsar-fitness-canary` baseline corpus is 30 refs minimum, which limits Wilson-bound tightness | A model that just clears `fn_rate_wilson_upper <= 0.20` on a 30-ref corpus still has substantial uncertainty in the true FN rate | Operators are encouraged to expand the per-deployment canary beyond the shipped baseline; spec §4.4 documents `n_biographical_truth` / `n_biographical_successful` separately so under-sized classes are visible. |
| Calibration cache is per-machine only | A second operator on a different workstation pays the calibration cost again | Cross-deployment calibration sharing is explicit out-of-scope for the hardening spec (§2). Operator practice: distribute a known-good `calibration_registry.json` via the engagement's encrypted bundle. |
| Combined "durant + general classification" in one prompt loses Durant accuracy on small models | Single-call multi-task design is tempting but unreliable | Two-pass design preserved — Durant alone, then general classification on the durant-included subset only. |

---

## 11. Cross-references

Toolkit (`harkers/dsar-toolkit`):

- `src/dsar_pipeline/gates/gate_durant.py` — canonical gate implementation
- `src/dsar_pipeline/gates/gate_durant_recheck.py` — recheck (safety-net) gate
- `src/dsar_pipeline/gates/prompt_loader.py` — `PromptLoader.load`, `compute_seal`, `sign_asset`, `build_registry`
- `src/dsar_pipeline/gates/text_truncation.py` — `truncate`, `truncate_with_token_check`, `count_subject_mentions_in_elided`
- `src/dsar_pipeline/gates/prompts/durant.system.md` — primary-pass system prompt asset (signed)
- `src/dsar_pipeline/gates/prompts/durant.recheck.system.md` — recheck system prompt asset (signed)
- `src/dsar_pipeline/gates/prompts/_registry.json` — append-only prompt-version archive index
- `src/dsar_pipeline/recheck_stage.py` — calibration-gated `RecheckStage`; backs `dsar-recheck` CLI
- `src/dsar_pipeline/fitness_canary.py` — `dsar-fitness-canary` CLI
- `src/dsar_pipeline/agents/agent22_scope_check.py` — JSONL-contract wrapper
- `src/dsar_pipeline/scope_check_stage.py` — stage driver + CLI
- `src/dsar_pipeline/config/model_context.json` — per-model truncation caps
- `src/dsar_pipeline/config/pricing.json` — per-model USD/1k tokens for recheck cost telemetry
- `examples/canary_baseline/` — toolkit-shipped baseline canary corpus

Conductor (this repo):

- `docs/durant-test.md` — this document
- `docs/durant-doc-lint.yaml` — lint rules consumed by `tools/check_durant_doc.py`
- `tools/check_durant_doc.py` — CI lint for this doc (§4.7)
- `src/dsar_orchestrator/verify.py` — backs `dsar-conductor verify --check {prompt-versions,fitness-report}`
- Per-engagement bypass scripts live in the engagement folder
  (`<engagement>/audit/agent-durant.py` etc.) — never in this repo, per
  the per-engagement data-isolation rule.

Operator-machine state:

- `~/.dsar/canary_sets/<deployment_id>/` — per-deployment fitness corpus
- `~/.dsar/fitness_reports/<deployment_id>/<ts>.json` — fitness reports
- `~/.dsar/calibration_registry.json` — post-hoc calibration cache; read by `RecheckStage`

## 12. Glossary

- **work_context_only** — Durant's term for "subject is incidental, not the focus". Excluded from disclosure under Art 15.
- **biographical** — Durant's term for "doc IS about the subject". Included in disclosure.
- **ambiguous** — neither pass could decide cleanly. Escalates to operator.
- **subject_protected_phrases** — operator-curated do-not-redact terms (the subject's own business identifiers). Separate from Durant scope; consulted by the redactor + verifier, NOT by the Durant gate.
- **scope_verdict** — the synthesised verdict downstream agents consume. One-to-one mapping from durant_verdict when no temporal gate applies (no date window specified in case_context.json).
- **canonical_seal_sha256** — SHA-256 over a prompt asset's full canonical frontmatter (sans seal_sha256 itself) + body. Stored in the asset's frontmatter; verified on every PromptLoader.load call. Tampering or accidental drift raises PromptIntegrityError immediately. The companion hash `effective_sha256` is taken post-strip — see below.
- **effective_sha256** — SHA-256 over the prompt body *after* applied strips + whitespace normalisation. This is the hash recorded per-row in durant_verdicts.jsonl (and the recheck JSONL) so audit rows track the exact text the LLM saw.
- **PromptLoader** — the toolkit class (in gates/prompt_loader.py) that loads a signed prompt asset, applies optional droppable strips, verifies the canonical seal, and returns the body plus metadata. Every Durant call goes through PromptLoader.
- **RecheckStage** — the toolkit BaseStage subclass that owns the calibration-gated under-disclosure recheck. RecheckStage writes recheck_decision.json unconditionally and recheck_summary.json with telemetry.
- **GateDurantRecheck** — the toolkit BaseGateAgent used by RecheckStage. GateDurantRecheck loads durant.recheck.system via the same PromptLoader, asks the inverse question, and never sees the primary verdict's rationale.
- **fitness_canary** — the §9.0 pre-flight check. Operator-curated corpus + truth labels; the dsar-fitness-canary CLI produces a Wilson-bounded report; dsar-conductor run consults a matching report and aborts the case if absent / stale / failing. See `fitness_canary.py` in the toolkit.
- **fn_rate_ci95** — the 95% confidence-interval pair `[lower, upper]` on the recheck's measured false-negative rate. Recorded in the calibration cache (calibration_registry.json) by the calibration portal. The fn_rate_ci95 upper bound is what RecheckStage compares to fn_threshold to decide whether to skip.
- **recheck_decision** — the JSON file recheck_decision.json recording the recheck stage's mode_effective + reason + calibration_entry_used. Written *unconditionally* whether the recheck ran or skipped — operators read this to know "did the safety net engage?".
- **effective_durant** — Agent22 synthesis intermediate: the per-doc Durant outcome AFTER the recheck override is applied. One of `present | not_present | ambiguous`. Carried into scope_verdicts.jsonl's evidence block alongside the raw durant_verdict and recheck_verdict. Also surfaced via synthesis_summary.json's per-batch counts.
- **role_context** — optional 500-char field in data_subject.json describing the subject's organisational responsibilities. Used to disambiguate role-domain documents (a doc *about* HR policy is biographical for an HR Director, work_context_only for an IT Admin). The role_context field is sanitised on read; never injected raw into prompts.
- **truncate_with_token_check** — the toolkit helper in text_truncation.py that combines the character-cap truncation modes (see §3.1) with the optional token-aware safety belt. truncate_with_token_check returns the truncated body plus the audit-row fields (truncation_mode, original_char_count, etc.).
