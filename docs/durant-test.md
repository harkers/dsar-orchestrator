# The Durant Test — How It Works

Reference doc for the biographical-focus test the DSAR conductor uses to
filter the corpus before redaction. Covers the legal foundation, the
programmatic shape (input / reasoning / output), both the toolkit canonical
implementation and the local-broker bypass pattern, and the under-disclosure
recheck layered on top.

Companion code: `dsar-toolkit` provides `gate_durant.py` + `scope_check_stage`.
Local deployments that route to an on-prem MLX broker bypass the toolkit's
default cloud-LLM routing with a thin per-engagement script that re-uses the
canonical prompt verbatim.

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

**Tie-breaker rule:** *default to `biographical` under uncertainty.*
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
| Document text | `working/<ref>.txt` (extracted by ingest_v3) | UTF-8 string, truncated to 6,000–8,000 chars |
| Data subject identity | `working/data_subject.json` | `full_name`, `aliases[]`, `email`, `additional_emails[]` |
| Doc ref | `working/register.json` entry | unique ref string (case-specific scheme) |

The data subject summary is one line built from `data_subject.json`,
e.g.:

```
name='<full_name>'; aliases=[<aliases>];
primary_email='<email>'; additional_emails=[<additional>]
```

The LLM never sees the operator-curated `subject_protected_phrases` for
the Durant test itself — those phrases gate **redaction**, not scope.

---

## 4. Programmatic approach

```
                                  ┌─────────────────────────┐
                                  │  working/<ref>.txt      │
                                  │  (extracted document)   │
                                  └──────────┬──────────────┘
                                             │
                                             ▼
┌──────────────────────┐         ┌─────────────────────────┐
│ data_subject.json    │────────►│ build_user_prompt()     │
│ (full_name, aliases, │         │  - subject summary      │
│  email, ...)         │         │  - doc ref              │
└──────────────────────┘         │  - truncated text       │
                                 └──────────┬──────────────┘
                                            │
                                            ▼
┌──────────────────────────────────────────────────────────┐
│ POST /v1/chat/completions   (OpenAI-compat endpoint)     │
│   model: <configured>                                    │
│   system: DURANT_SYSTEM_PROMPT                           │
│   user:   <prompt above>                                 │
│   temperature: 0.0                                       │
│   max_tokens: 400                                        │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼  (response, possibly with retry on 500/ConnectError)
┌──────────────────────────────────────────────────────────┐
│ {"durant_verdict": "<bio|work_context_only|ambiguous>",  │
│  "rationale": "<one-or-two sentences>"}                  │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼  (validate verdict ∈ ALLOWED; coerce to "ambiguous" on parse error)
┌──────────────────────────────────────────────────────────┐
│ Append row to working/durant_verdicts.jsonl              │
│ {case_id, doc_ref, durant_verdict, rationale,            │
│  model, prompt_version, elapsed_sec,                     │
│  error_state? (model_unreachable | schema_validation     │
│                _failed | empty_response)}                │
└──────────────────────────────────────────────────────────┘
```

Key properties of the runtime:

- **Per-doc, single LLM call.** No multi-pass refinement, no chain-of-thought
  extraction. The model returns one JSON object.
- **Strict allowed-values coercion.** Unknown verdicts collapse to `ambiguous`
  so a flaky LLM response can never silently introduce a bad verdict.
- **Error-as-data.** Network failures and parse errors produce an output row
  with `error_state` set and a safe default verdict; nothing throws.
- **Resume-safe.** The script re-reads the output file on start, drops any
  errored rows (atomic temp-file + replace), and re-attempts only the missing
  doc refs. Running the script twice never duplicates LLM work but does
  recover from transient failures.
- **Retry-with-backoff.** On HTTP 500 or connection errors (a local MLX
  downstream `mlx_lm.server` can crash and respawn taking 30–60 s), the
  script waits 2 → 8 → 30 → 60 s before giving up on a single record. Most
  transient broker failures are absorbed silently.

---

## 5. Outputs

Primary output: `working/durant_verdicts.jsonl` — one row per ingested
document, append-only.

```jsonl
{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"biographical","rationale":"…short sentence citing evidence…","model":"<alias>@<host>","prompt_version":"<version>","elapsed_sec":1.5}
{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"work_context_only","rationale":"…","model":"<alias>@<host>","prompt_version":"<version>","elapsed_sec":1.3}
```

Downstream consumers:

- **`working/scope_verdicts.jsonl`** is derived from `durant_verdicts.jsonl`
  via a one-pass synthesis script (mapping `biographical → present`,
  `work_context_only → not_present`, `ambiguous → ambiguous`).
- **Redaction (toolkit stage 7)** filters to `scope_verdict = present` —
  PII tagging + redaction only runs on the biographical set, saving 4–6×
  LLM time vs running on the full ingested corpus.
- **Operator review (toolkit stage 11)** consults `durant_verdicts.jsonl`
  rationale fields when the operator is deciding ambiguous cases.

Secondary outputs:

- `<engagement>/audit/agent-durant-progress.jsonl` — periodic progress rows
  (every 50 docs) with rate, errors, ETA estimate. Useful for monitoring
  without reading the broker logs.

---

## 6. Toolkit canonical implementation

Lives in the `dsar-toolkit` repo:

| File | Purpose |
|---|---|
| `src/dsar_pipeline/gates/gate_durant.py` | `GateDurant(BaseGateAgent)` class. Holds `DURANT_SYSTEM_PROMPT`, runs per-ref. Defaults to `claude-opus-4-7` via the toolkit's `RoleRouter`. |
| `src/dsar_pipeline/scope_check_stage.py` | `ScopeCheckStage(BaseStage)` orchestrates `gate_durant` + `gate_temporal_scope` over a register, writes per-ref `scope_verdicts.jsonl`. Exposes the `dsar-scope-check` CLI. |
| `src/dsar_pipeline/agents/agent22_scope_check.py` | JSONL-contract adapter; consumes per-ref gate outputs and emits the final `scope_verdict`. Synthesis rule lives in module-level `_synthesise_verdict(durant, temporal)`. |

The toolkit version handles:
- LLM model routing via Agent 18 `RoleRouter` (default `scope_check` role)
- Pre-LLM de-identification (`[PERSON_0]`, `[EMAIL_3]`, etc.) — the model
  sees placeholder tokens instead of raw names, so the prompt has a
  dedicated *"About placeholder tokens in this prompt"* section
- Cost tracking (`cost_estimate_usd = 0.01` per ref)
- Tier 2 gate classification for the toolkit's gate framework

It does NOT handle:
- Direct routing to a local MLX broker (the model alias config is
  Anthropic-cloud oriented)
- The on-prem "no de-identification preprocessor" path

---

## 7. Local-broker bypass pattern

For air-gapped / on-prem deployments routing through `mlx-broker`, the
conductor runs a per-engagement script (kept in the engagement folder
to satisfy per-engagement data-isolation rules) that:

1. POSTs directly to the broker's OpenAI-compatible endpoint
   (`http://127.0.0.1:8090/v1/chat/completions`) using a local model
   alias (e.g. `mini` = Qwen2.5-7B-Instruct-4bit).
2. Reuses `DURANT_SYSTEM_PROMPT` **verbatim** from `gate_durant.py`,
   marked with a `# Verbatim from dsar_pipeline/gates/gate_durant.py:
   DURANT_SYSTEM_PROMPT` provenance comment.
3. Drops the placeholder-tokens preamble (when the deployment does not
   pre-deidentify text, the preamble would confuse the model).
4. Adds a `Respond with VALID JSON ONLY` schema tail (no parser layer
   sits between the broker and the script).
5. Implements retry-with-backoff + resume-cleanup for downstream crashes.

The bypass pattern emerged because making `GateDurant`'s model routing
pluggable is a v0.5.0 task; meanwhile the script is small (~360 lines
stdlib) and the prompt-source comment surfaces the drift risk.

**Drift risk.** Because the prompt is duplicated, a future refinement to
`gate_durant.py::DURANT_SYSTEM_PROMPT` won't propagate automatically.
Diff the local copy against the toolkit at each conductor release.

---

## 8. Under-disclosure recheck (the safety net)

Layered on top of the primary Durant pass: a second LLM pass that
re-examines every `work_context_only` verdict by asking the *inverse*
question.

Output: `working/durant_underdisclosure_recheck.jsonl`

Recheck verdicts:

| Verdict | Meaning | Downstream effect |
|---|---|---|
| `confirmed_work_context_only` | Original Durant was right; keep excluded | No change |
| `reclassify_to_biographical` | Original Durant was wrong; ADD to disclosure | Pull back into the redaction set + operator review |
| `reclassify_to_ambiguous` | Genuine uncertainty | Operator review |

Why a separate pass instead of tuning the primary prompt:

1. **Asymmetric error costs.** Under-disclosure is the worse legal error.
   A second pass with an inverse question framing catches cases the
   primary pass dismissed too quickly.
2. **Confirmation-bias mitigation.** The recheck deliberately does NOT
   see the original Durant rationale; it re-evaluates the document
   independently and any disagreement surfaces as a reclassification
   candidate. (The original rationale is kept in the per-row audit record
   so the chain of reasoning is reconstructible — just not in the prompt.)
3. **Error defaults flag-rather-than-confirm.** If the recheck call
   errors (network failure, schema validation failure), the default is
   `reclassify_to_ambiguous` — the doc surfaces to the operator instead
   of silently staying excluded. For an under-disclosure SAFETY check,
   "I'm not sure" must escalate, not silently agree.

In practice the recheck reclassifies a meaningful fraction (observed
~60% in one real case) of supposedly-excluded docs as candidate-biographical
or ambiguous. That is the safety net working: the primary pass's
false-negative rate was high enough that the operator would otherwise
never have seen those documents.

---

## 9. Calibration

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
combo. If agreement is low in either direction, the right answer is to
rerun the primary Durant pass with a refined prompt or a heavier model
before committing to operator review on the full set.

---

## 10. Known issues / drift risks

| Issue | Impact | Mitigation |
|---|---|---|
| Toolkit `GateDurant` defaults to `claude-opus-4-7`; not routable to local broker | Toolkit canonical implementation unusable on on-prem Macs as-shipped | Local-broker bypass script. v0.5.0 task: make model routing pluggable. |
| `DURANT_SYSTEM_PROMPT` duplicated in bypass script | Toolkit prompt refinements don't propagate | Verbatim-source comment marks provenance; drift is detectable via diff at release time. |
| `mlx_lm.server` mid-run crashes return HTTP 500 | Sustained passes can have very high error rates after a crash | Retry-with-backoff (2 → 8 → 30 → 60 s) absorbs transient failures; resume-cleanup re-attempts errored rows on rerun. |
| Loading other models (e.g. `code-qwen25` for code review, `chat` for gate decisions) evicts the primary model mid-pass | Sustained durant pass fails after model eviction | Operator discipline: no other broker calls during long passes; or pin models if the broker supports it. |
| Small models produce JSON with the right keys but occasionally not the right shape | One-off bad rows | Strict allowed-values coercion; bad responses default to `ambiguous`; rationale field captures the raw model output for audit. |
| Combined "durant + general classification" in one prompt loses Durant accuracy on small models | Single-call multi-task design is tempting but unreliable | Two-pass design — Durant alone, then general classification on the durant-included subset only. |

---

## 11. Cross-references

Toolkit (`harkers/dsar-toolkit`):

- `src/dsar_pipeline/gates/gate_durant.py` — canonical gate implementation
- `src/dsar_pipeline/agents/agent22_scope_check.py` — JSONL-contract wrapper
- `src/dsar_pipeline/scope_check_stage.py` — stage driver + CLI

Conductor (this repo):

- `docs/durant-test.md` — this document
- Per-engagement bypass scripts live in the engagement folder
  (`<engagement>/audit/agent-durant.py` etc.) — never in this repo, per
  the per-engagement data-isolation rule.

## 12. Glossary

- **`work_context_only`** — Durant's term for "subject is incidental, not the focus". Excluded from disclosure under Art 15.
- **`biographical`** — Durant's term for "doc IS about the subject". Included in disclosure.
- **`ambiguous`** — neither pass could decide cleanly. Escalates to operator.
- **`subject_protected_phrases`** — operator-curated do-not-redact terms (the subject's own business identifiers). Separate from Durant scope; consulted by the redactor + verifier, NOT by the Durant gate.
- **`scope_verdict`** — the synthesised verdict downstream agents consume. One-to-one mapping from `durant_verdict` when no temporal gate applies (no date window specified in `case_context.json`).
