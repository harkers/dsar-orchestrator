# People-register hardening — design v1

| Version | Date       | Author          | Change                                                     |
|---------|------------|-----------------|------------------------------------------------------------|
| v1      | 2026-05-28 | Stuart Harker / Claude | Initial spec, validated through 6 brainstorm-jury rounds (Kimi / Gemini / Qwen-Coder). Gemini approve at R4 + R6; Kimi changes-requested (consistent polish requests); Qwen-Coder mixed. Lock-in by operator. |

## 0. Why this exists

A live UK GDPR Article 15 DSAR engagement (case 301770, ProPharma controller, ~853 in-scope docs) uncovered a systemic class of third-party PII leak: **signature blocks survived redaction**.

Concrete examples that escaped:
- `TALENT ACQUISITION PARTNER` (job role identifying a specific HR person, with the (redacted) sender name above)
- `Office: +44 1748 828800` (Richmond office direct line)
- `Eva Anthopoulou`, `Kashif Hussain`, `David Berglund` (full names) appearing unredacted in email bodies
- `VICE PRESIDENT`, `HUMAN RESOURCES MANAGER`, `DATA PROTECTION MANAGER` (capitalised role titles in sign-off blocks)

**Root cause analysis:**
1. The existing redactor relies on per-token NER hits. NER detects names, but signature-block role titles + corporate direct lines look like "preserve-eligible boilerplate" rather than third-party PII.
2. The toolkit HAS a `build_people_register` stage, but it only extracts MAILBOX-OWNER fields. On a flat Exchange dump (single root, owners null), the stage silently produces an empty register. It didn't run on the v4_full_corpus case.
3. RFC822 From/To/Cc/Bcc headers in `.eml`/`.msg` files contain — literally — every person who emailed or was emailed in the corpus. That graph IS the third-party denylist. The ingest stage didn't harvest it into a structured form.
4. There is no canonical "list of all third parties found in this corpus" the redactor can enforce against; per-token NER fights the same battle 4595 times independently.

**This spec hardens that gap.** The dictionary is per-engagement, **auto-built from the corpus itself** (every RFC822 header is a fact about who's a third party). Toolkit owns the schema, the discovery stage, the conductor enforcement, and the post-redact PII jury defence-in-depth.

## 1. Architecture

Pipeline flow with new components marked [NEW]:

```
ingest
  ↓
[NEW] source_strategy.detect  → SourceStrategy registry picks the right harvest path
  ↓
[NEW] per-doc people harvest (RFC822 headers + signature regions ONLY)
       → working/<ref>_people.jsonl per doc
  ↓
build_people_register (existing stage, FIXED to consume per-doc harvest)
  ↓ cluster aliases via embed.cosine
  ↓ compute subject_centricity_score (ADVISORY only — never auto-suppress)
working/people_register.json
  ↓
[NEW] sig_block_discovery_stage   (regex post-pass to catch what NER missed)
  ↓ merge candidates into people_register
  ↓
[NEW] /people-register operator review console (frequency-ranked, top-50 default)
  ↓ operator confirms / rejects / merges
working/third_party_denylist.json   ← the "standard dictionary" answer
  ↓ consumed by
scope_check → durant → pii_id → ... → redact_stage
   (redactor always reads denylist at LOAD time, runs continuous subject-protection
    validation with hash-keyed cache)
  ↓
[NEW] pii_jury_review_stage (defence-in-depth)
   (single-juror default, dual-juror opt-in / auto-promote;
    2D NER × text_quality stratification matrix)
working/pii_jury_verdicts.jsonl
  ↓
final_synth → export → deliverable
```

### 1.1 Pluggable source-strategy registry (R2 delta)

Hard-coding four source types is the wrong abstraction. Use a pluggable registry where each strategy implements a small protocol; new source types (Teams export, Slack export, EDRM export, etc) are new strategy files, no core changes.

```python
# dsar_pipeline.source_strategies.base
class SourceStrategy(Protocol):
    name: str                 # "exchange_nested", "exchange_flat", "sharepoint", "raw_dump", ...
    priority: int = 100       # higher wins on confidence tie
    def detect(self, case_dir: Path) -> float:
        """Confidence 0..1 that this strategy applies to this case."""
    def extract_identities(self, ref: dict, text: str) -> list[Identity]:
        """Returns Identity dataclasses extracted from this doc."""
    def validate(self, extractions: list[Identity]) -> ValidationResult:
        """Per-strategy fail-loud conditions."""
```

Strategies registered via setuptools `entry_points` group `dsar_pipeline.source_strategies`. Registry runs `detect()` on all; **filters out zero-confidence candidates first** (a strategy returning 0.0 cannot be the answer), then ranks the survivors: highest confidence wins, then priority, then alphabetical name (deterministic — no operator tie-break in unattended pipelines).

If ALL strategies return 0.0 confidence, the registry falls back to the lowest-priority strategy that opts-in via `is_universal_fallback: bool = False`. Exactly one strategy may set `is_universal_fallback = True` (default = `raw_dump`); this is the catch-all when no positive-confidence strategy fits.

Built-in strategies (toolkit ships):
- `exchange_nested` (priority 110) — when `mailbox_owner_email` is populated on register entries
- `exchange_flat` (priority 100) — Exchange root, mailbox_owner null, RFC822 fallback
- `sharepoint` (priority 100) — document properties: `Author`, `LastModifiedBy`, `Custodian`
- `raw_dump` (priority 50) — fallback, auto-extracts from doc-property + top-500-char header-like strings

### 1.2 Per-doc people harvest (R1 + R3 deltas — scoped to headers + signature regions)

**For `.eml`/`.msg`:**
- Parse RFC822 headers (`From`, `To`, `Cc`, `Bcc`, `Reply-To`, `Sender`) → structured `(email, display_name)` tuples
- Detect signature region using `email_reply_parser` library (English) or `mailparser` raw RFC822 + regex heuristic (non-English) — language detected via `langdetect`
- Extract from signature region only: name lines, `Office:`/`Mobile:`/`Direct:`/`Tel:` phone labels, ALL_CAPS title patterns
- Confidence score per extraction: `0.9` (anchor + keyword proximity), `0.5` (last-N-lines heuristic), `0.3` (NER fallback)
- Confidence < 0.6 surfaces to operator review as "low-confidence extraction"
- **Full-body NER is NOT in scope for the register build** (avoids flooding operator review with incidental mentions; body-text NER stays in the redactor's per-token pass as the second safety net)

**For `.docx`/`.xlsx`/`.pdf`:**
- Document properties: `Author`, `LastModifiedBy`, `Custodian`, `Owner`
- Signature anchors: keywords `{Signatory, Signed by, Executed by, Name, Print Name}` + underscore-line patterns (`_____`) within **min(5 lines, 250 chars)** proximity
- Confidence drops to 0.4 for standalone underscore patterns (likely form-field, not signature)

**Output per doc:** `working/<ref>_people.jsonl` — one Identity record per extracted entity, with `correlation_id` (see §1.7).

### 1.3 Case-level people-register aggregation

Existing `dsar_pipeline.people_register` module, extended to consume the per-doc harvest:

- Merge per-doc extractions
- Cluster aliases via existing `embed.cosine` (similarity threshold configurable, default 0.85)
- Tag `is_data_subject` per cluster (cross-ref `data_subject.json` email + aliases + protected_phrases) using same embed-similarity threshold for fuzzy match
- Compute per cluster:
  - `mention_count`, `distinct_doc_count`
  - `is_subject_confidence: float` (0-1)
  - `subject_centricity_score: float` (R2 delta — advisory; see §1.4)
  - `text_quality_summary` (mode across source docs; see §1.9 for ordering)
  - `confidence_score` (lowest extraction confidence across constituent records)
- Append `correlation_id = uuid5(uuid5(NAMESPACE_DSAR, case_id), f"{strategy_name}:{ref}")` per source-ref to avoid cross-strategy collision

Output: `working/people_register.json`.

### 1.4 `subject_centricity_score` (Durant biographical-focus, entity-level)

The Durant biographical-focus test applies to ENTITIES too, not just documents. A third-party who is biographically focused on the subject (e.g. "my daughter's GP", "the subject's manager") arguably IS the subject's personal data.

```python
cluster.subject_centricity_score = 0.6 * header_proximity_to_subject(cluster, refs) \
                                 + 0.4 * pronoun_co_resolution_with_subject(cluster, refs)
```

**Advisory only (R3 delta):** never auto-suppresses redaction. Always surfaces in the operator review console as `REVIEW PRIORITY: subject_referent_candidate` with explicit per-cluster rationale. Operator must explicitly approve preservation.

**Helper-function definitions** (v1 baseline; refine in v2 calibration):
- `header_proximity_to_subject(cluster, refs) -> float ∈ [0,1]`: fraction of cluster's source-refs where any cluster identifier appears in the same RFC822 header line, or adjacent ≤ 2 lines, to a subject identifier. 0 if no co-occurrence.
- `pronoun_co_resolution_with_subject(cluster, refs) -> float ∈ [0,1]`: fraction of cluster mentions where a possessive pronoun (`my`, `our`, `their`) referring back to the subject sentence-precedes the cluster reference. Uses spaCy's `en_core_web_sm` coref-light heuristic (NOT neuralcoref — kept simple for v1).

**[POLISH]** Weights `0.6/0.4` and threshold `0.7` are uncalibrated v1 defaults. A calibration corpus (10-50 labelled clusters across 2-3 engagements) lands in v2 to set defensible numbers. Until then, the score is documented as "advisory pending calibration corpus" in the operator console help panel.

### 1.4a `sig_block_discovery_stage` (regex post-pass for NER misses)

Runs after `build_people_register`, before the operator review console. Purpose: catch signature-block content the structured RFC822 + sig-region extractor missed because of malformed messages, encoded HTML, or non-standard signatures.

**Inputs:**
- `working/people_register.json` (existing clusters from §1.3)
- All ingested doc text under `working/<ref>.txt`

**Regex patterns:**

```python
# Toolkit-baseline patterns (live in dsar_pipeline.redaction_patterns)
SIGBLOCK_PHONE_LABEL = re.compile(
    r"\b(?:Office|Direct|Tel|Mobile|Phone)\s*:\s*\+?[\d\s()\-]{7,}",
    re.IGNORECASE
)

# ALL-CAPS title detection; allowlist filters out subject + controller + boilerplate
TITLE_CAPS = re.compile(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,}){1,4}\b")

TITLE_CAPS_ALLOWLIST = frozenset({
    # subject identifiers (case must inject from data_subject.json)
    # controller identifiers (case must inject from case_context.json)
    # contract boilerplate
    "THIS AMENDMENT", "THIS AGREEMENT", "WITNESS WHEREOF",
    "STATEMENT OF WORK", "STATEMENT WORK", "MASTER SERVICES AGREEMENT",
    # DocuSign / common system phrases
    "VIEW COMPLETED DOCUMENT", "CLOSED WON OPPORTUNITY REQUEST",
    # Statute / framework
    "DSAR", "GDPR", "DPA", "EWCA", "UK", "EU", "ICH", "GCP",
})
```

**Merge logic:**

1. Scan each doc text. For each match (`TITLE_CAPS` filtered through allowlist, plus `SIGBLOCK_PHONE_LABEL`):
   - Try to associate with an existing cluster (proximity to a known person in the doc)
   - If no association: create a `candidate` cluster with `confidence_score = 0.5` (regex-only, no header-anchored), `is_subject_confidence = 0.0`
2. Candidates merge into `working/people_register.json` flagged with `discovered_by: "sig_block_discovery"`
3. Operator review surfaces these as "regex-discovered candidate; lower confidence than RFC822-anchored entries"

**Output:** updated `working/people_register.json` with `discovered_by` field per cluster.

**Audit event:** `SIG_BLOCK_DISCOVERY_COMPLETED` with counts (`candidates_found`, `merged_with_existing`, `new_clusters_added`).

### 1.5 Operator review console: `/people-register`

Same UX shape as the existing `flag_review` console (operator console v0.13.0+).

**Default view: top-50 clusters** ranked by:
```
ranking_score = mention_count * distinct_doc_count * (1 - is_subject_confidence)
```

Above the top-50, a separate section: `REVIEW PRIORITY: subject_referent_candidate` clusters (`subject_centricity_score > 0.7` advisory).

**Per-cluster actions:**
- `accept_as_third_party` (default) → entry goes into denylist for force-redact
- `preserve` → marked do-not-redact (controller's published main line, generic role title, etc.)
- `merge_with` → alias of another cluster
- `mark_subject_alias` → operator correction; entry moves from third-party to subject side

**Per-cluster display:** name + email + phone + first_seen_ref + mention_count + distinct_doc_count + `text_quality_summary` + extraction confidence.

**Bulk-accept** for high-confidence patterns (e.g. all `@<controller-domain>` emails default to `accept_as_third_party`).

**Expand-all** for long tail beyond top-50.

### 1.6 Continuous subject-protection cross-check (R2/R3 deltas)

A static cross-check at write-time leaves a window where stale operator edits or `data_subject.json` corrections downstream re-introduce the leak. Validation runs at **LOAD-time** in every consumer of `third_party_denylist.json`:

```python
def validate_denylist_against_subject(denylist_path, data_subject_path, embed_model, cache_path):
    """Called by every stage that loads the denylist. Hash-keyed cache prevents
    repeated embedding computation."""
    cache_key = sha256(
        denylist_path.read_bytes() +
        data_subject_path.read_bytes() +
        embed_model.manifest_signature_id.encode()  # immutable hash, not version_string
    ).hexdigest()
    
    if cache_path.exists():
        cache = _load_signed_cache(cache_path)   # HMAC-signed; rejects tampering
        if cache.get("key") == cache_key:
            if cache.get("result") == "ok": return
            raise SubjectInDenylistError(cache["error"])
    
    # Cache miss — re-embed + validate
    denylist = json.loads(_safe_case_path(case_dir, "working/third_party_denylist.json").read_text())
    ds = json.loads(_safe_case_path(case_dir, "working/data_subject.json").read_text())
    subject_vectors = [embed(s) for s in
                       (ds["full_name"], *ds["aliases"], *ds["subject_protected_phrases"])]
    for entry in denylist["entries"]:
        if max(cosine(embed(entry["canonical_name"]), sv) for sv in subject_vectors) > 0.85:
            raise SubjectInDenylistError(
                f"Denylist entry {entry['canonical_name']!r} fuzzy-matches subject."
            )
    
    _write_signed_cache_atomic(cache_path, {"key": cache_key, "result": "ok"})
```

**Cache implementation (R4/R5 deltas):**
- HMAC signature using per-case secret stored at `working/.case_secret` (mode 0600)
- Atomic write: tmp + os.fsync + os.replace
- LRU trim: default max 50MB, configurable via `case_config.subject_protection_cache_max_mb`
- Path canonicalisation via `_safe_case_path()` (uses `Path.relative_to` for containment, not string `startswith`)

**Called by:** `redact_stage` (load), `pii_jury_review_stage` (load), `export_stage` (load).

**[POLISH]** TOCTOU residual: `resolve(strict=False)` follows symlinks before `relative_to` check. v1 mitigates by pre-rejecting absolute paths, `.`, `..`, null bytes. v2 considers atomic open + fstat for fully race-free check.

### 1.7 Redactor integration

The existing `redact_stage` reads `working/third_party_denylist.json` at start:

- Every token matching denylist (email exact, name fuzzy via embed.cosine threshold 0.85, phone exact) is **forced-redact** regardless of per-token NER verdict
- NER-based per-token detection continues IN PARALLEL — the denylist is additive (a safety net for the redactor, not a replacement)
- Body-text NER catches third-party mentions in document body that signature-region harvest can't see — stays as the third safety net
- All redaction events get the per-doc `correlation_id` for Article 30 ROPA traceability

### 1.8 Post-redact PII jury (defence-in-depth)

New stage between `redact_stage` and `final_synth_stage`. Today's two-juror cleanup proved its worth (caught the `TALENT ACQUISITION` leak); promote to a standard verification stage.

**Default: single juror** (Mistral-Small 3.2 24B 4bit, via mlx-broker on `127.0.0.1:8090`, local-only).
- ~5-15 sec per doc
- Lower cost on routine cases (~3hr per 750-doc corpus)

**Dual-juror opt-in OR auto-promotion** triggers if:
- `case_config.pii_jury.dual_juror = True` (explicit operator choice), OR
- `case_config.data_subject.vulnerable = True` (vulnerable adult cases), OR
- ≥20% of top-50 ranked clusters have `is_subject_confidence < 0.85` (uncertainty heuristic)

Then juror B = Llama 3.3 70B 4bit DWQ (Meta lineage, independent vendor).

**Schema (pydantic, strict-validation):** `ThirdPartyPiiCheck`:
```python
class ThirdPartyPiiCheck(BaseModel):
    has_third_party_pii: bool
    pii_categories: list[Literal["full_name","email","phone","address","postcode",
                                  "id_number","ipv4","date_of_birth","other"]]
    example_tokens: list[str]            # up to 5
    severity: Literal["none","low","medium","high"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=600)
```

**Sampling policy** (R3/R4/R5 deltas — `select_jury_docs`):

```python
JURY_SAMPLE_RATES: dict[tuple[str, str], float] = {
    # 4 ner_band × 4 text_quality (ocr_failure pre-filtered out — never reaches here)
    ("very_low_ner", "high"):    1.00,
    ("very_low_ner", "medium"):  0.80,
    ("very_low_ner", "low"):     0.50,
    ("very_low_ner", "unknown"): 0.80,    # treat unknown as suspicious
    ("low_ner",      "high"):    1.00,
    ("low_ner",      "medium"):  0.60,
    ("low_ner",      "low"):     0.40,
    ("low_ner",      "unknown"): 0.60,
    ("med_ner",      "high"):    0.30,
    ("med_ner",      "medium"):  0.20,
    ("med_ner",      "low"):     0.15,
    ("med_ner",      "unknown"): 0.25,
    ("high_ner",     "high"):    0.10,
    ("high_ner",     "medium"):  0.10,
    ("high_ner",     "low"):     0.10,
    ("high_ner",     "unknown"): 0.15,
}

def select_jury_docs(refs, sampling_policy, min_floor):
    # PRE-FILTER: ocr_failure refs → manual_preprocessing_queue, never to jury
    refs_for_jury = [r for r in refs if getattr(r, "text_quality", "unknown") != "ocr_failure"]
    refs_manual = [r for r in refs if getattr(r, "text_quality", "unknown") == "ocr_failure"]
    if refs_manual:
        _write_manual_queue(refs_manual)
    if not refs_for_jury: return []

    # Bin into 16-cell matrix
    bins: dict[tuple[str, str], list] = {}
    for r in refs_for_jury:
        tq = getattr(r, "text_quality", "unknown") or "unknown"
        bins.setdefault((ner_band(r.ner_count), tq), []).append(r)

    selected: list = []
    for bin_key, items in bins.items():
        rate = JURY_SAMPLE_RATES.get(bin_key, 0.30)   # default conservative
        selected += items if rate >= 1.0 else random_sample(items, pct=rate)

    # Min floor — top-up with O(1) set lookup (R6 perf fix)
    if len(selected) < min_floor:
        selected_ids = {id(r) for r in selected}
        topup_pool = [r for r in refs_for_jury if id(r) not in selected_ids]
        topup_k = min(min_floor - len(selected), len(topup_pool))
        if topup_k > 0:
            selected += random_sample(topup_pool, k=topup_k)

    return selected
```

**Disagreement adjudication (dual-juror):** A vs B disagree → audit event `PII_JURY_DISAGREEMENT` + auto-assigned disposition `"operator_review_required"`. NO auto-resolution.

**Output:** `working/pii_jury_verdicts.jsonl` — one row per (doc × juror). Gates `final_synth`.

**Article 30 ROPA per verdict event:**
```python
{
    "event_type": "PII_JURY_INFERENCE_RECORD",
    "doc_ref": ref, "juror": "A"|"B",
    "model_alias": "mistral", "model_repo": "mlx-community/Mistral-Small-3.2-24B-Instruct-2506-4bit",
    "manifest_signature_id": "<sha256-of-model-weights>",
    "quantisation": "4bit-mxfp4", "temperature": 0.0,
    "prompt_id": "pii_jury.system",
    "prompt_canonical_seal_sha256": "<hash>",
    "prompt_effective_seal_sha256": "<hash>",
    "schema_version": "ThirdPartyPiiCheck.v1",
    "verdict": {...pydantic model_dump()...},
    "correlation_id": "<uuid5>",
    "broker_endpoint": "mlx-broker-local:8090",
    "infer_latency_ms": 5432,
}
```

### 1.9 `text_quality_summary` definition

Per-doc field on the ingest register: `Literal["high","medium","low","ocr_failure","unknown"]`, derived from `ocr_confidence_avg + char_density + extraction_success_ratio`.

Per-cluster aggregation: **mode** (most common band across source docs), NOT average (categorical, not numeric).

**Tie-breaking precedence** (R5/R6 delta — deterministic, conservative):
```python
TEXT_QUALITY_ORDER = ["ocr_failure", "unknown", "low", "medium", "high"]
# In a tie, the band appearing FIRST in this list wins (i.e. lowest quality
# or highest uncertainty). Conservative: surfaces concerns rather than masks.
```

## 2. Conductor enforcement (`dsar_orchestrator.pipeline`)

Following the same pattern as the existing `_run_fitness_preflight` (built in durant-pipeline-hardening Phase 5).

### 2.1 Identity-set + source-type pre-flight (replaces blanket coverage %)

```python
def _run_people_register_preflight(case_dir, config):
    register_path = _safe_case_path(case_dir, "working/people_register.json")
    
    if not register_path.exists():
        if config.force_skip_people_register_reason:
            _emit_skip_event(case_dir, config.force_skip_people_register_reason)
            return
        # Auto-run with source-strategy registry
        from dsar_pipeline.build_people_register import build_people_register
        build_people_register(case_dir)
        if not register_path.exists():
            raise PeopleRegisterBuildError(
                f"build_people_register did not produce {register_path}. "
                f"Source strategy detection: <X>. Check ingest output."
            )
    
    register = json.loads(register_path.read_text())
    third_party_clusters = [c for c in register if not c["is_data_subject"]]
    
    # Sufficiency check (NOT blanket coverage %)
    if not third_party_clusters and _corpus_has_communicants(case_dir):
        raise PeopleRegisterEmptyError(
            "people_register has zero third-party clusters but corpus contains "
            "communicants. Likely source-strategy misdetection or extraction failure."
        )
    
    # Strategy-specific validation
    strategy = _resolve_source_strategy(case_dir)
    strategy.validate(register)   # raises strategy-specific errors
```

### 2.2 `CaseConfig` additions

```python
class CaseConfig(BaseModel):
    # ... existing fields ...
    people_register_enabled: bool = True
    force_skip_people_register_reason: Optional[str] = None
    
    pii_jury_dual_juror: bool = False
    pii_jury_sampling: Literal["full", "tiered", "spot_check"] = "tiered"
    pii_jury_disagreement_policy: Literal["operator_review", "redact_safer"] = "operator_review"
    
    subject_protection_cache_max_mb: int = Field(default=50, gt=0)
```

### 2.3 Audit chain — new event types

In `dsar_pipeline.audit.AuditEventType`:
- `SOURCE_TYPE_DETECTED` (with strategy chosen, confidence, rationale)
- `PEOPLE_REGISTER_BUILD_STARTED` / `_BUILT` (with extraction stats)
- `PEOPLE_REGISTER_GATE_PASSED` / `_GATE_BYPASSED`
- `DENYLIST_SUBJECT_PROTECTION_VIOLATION` (operator must triage)
- `EXTRACTION_QUALITY_GATE_WARNING` (10%-50% OCR failure soft-gate)
- `PII_JURY_INFERENCE_RECORD` (Article 30 ROPA — per doc × juror)
- `PII_JURY_DISAGREEMENT` (dual-juror A/B mismatch)

All events carry the `correlation_id` field for doc-lifecycle forensic traceability.

### 2.4 Extraction-quality gate (R4 delta — soft-gate, not hard-halt)

```python
def _check_extraction_quality(case_dir):
    refs = load_refs(case_dir)
    if not refs:
        raise EmptyIngestError("0 refs ingested")
    ocr_failure_rate = sum(1 for r in refs
                           if getattr(r, "text_quality", "unknown") == "ocr_failure") / len(refs)
    if ocr_failure_rate > 0.50:
        # Hard halt — single unified exception (R5 delta)
        raise ExtractionQualityCatastrophicError(
            f"OCR_FAILURE rate {ocr_failure_rate:.1%} > 50%. Halt pipeline; "
            f"operator must triage extraction failures upstream."
        )
    if ocr_failure_rate > 0.10:
        # Soft warning — pipeline continues; operator can "Proceed with reduced set"
        _emit_warning("EXTRACTION_QUALITY_GATE_WARNING",
                      rate=ocr_failure_rate, refs_total=len(refs))
```

### 2.5 Threat-model artefact

`working/threat_model.md` mandatory per case. Conductor validates **content** (not just presence — R4/R5 delta):

```python
REQUIRED_SECTIONS_NORMALISED = {
    "embed endpoint", "isolation posture", "denylist scope",
    "per-engagement data flow", "subject identifier handling",
}
HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)

def _verify_threat_model(case_dir):
    tm = _safe_case_path(case_dir, "working/threat_model.md")
    if not tm.exists():
        raise ThreatModelMissingError("working/threat_model.md absent")
    content = tm.read_text()
    found_normalised = {m.group(1).strip().lower().lstrip("#").strip()
                        for m in HEADING_RE.finditer(content)}
    missing = REQUIRED_SECTIONS_NORMALISED - found_normalised
    if missing:
        raise ThreatModelIncompleteError(f"missing required sections: {sorted(missing)}")
    # Each section ≥30 chars of content
    for section in REQUIRED_SECTIONS_NORMALISED:
        body = _extract_section(content, section)
        if len(body.strip()) < 30:
            raise ThreatModelIncompleteError(
                f"section {section!r} has <30 chars of content")
```

## 3. Schemas (new + extended)

### 3.1 `schemas/people_register.schema.json`

JSON Schema for `working/people_register.json` — list of cluster entries:
```json
{
  "$id": "https://harkers.dsar/schemas/people_register.schema.json",
  "type": "array",
  "items": {
    "type": "object",
    "required": ["canonical_name", "is_data_subject", "mention_count",
                 "distinct_doc_count", "is_subject_confidence",
                 "source_refs", "correlation_ids"],
    "properties": {
      "canonical_name": {"type": "string"},
      "emails": {"type": "array", "items": {"type": "string"}},
      "phones": {"type": "array", "items": {"type": "string"}},
      "titles": {"type": "array", "items": {"type": "string"}},
      "is_data_subject": {"type": "boolean"},
      "is_subject_confidence": {"type": "number", "minimum": 0, "maximum": 1},
      "subject_centricity_score": {"type": "number", "minimum": 0, "maximum": 1},
      "mention_count": {"type": "integer", "minimum": 1},
      "distinct_doc_count": {"type": "integer", "minimum": 1},
      "text_quality_summary": {"enum": ["high","medium","low","ocr_failure","unknown"]},
      "confidence_score": {"type": "number", "minimum": 0, "maximum": 1},
      "source_refs": {"type": "array", "items": {"type": "string"}},
      "correlation_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}}
    }
  }
}
```

### 3.2 `schemas/third_party_denylist.schema.json`

```json
{
  "$id": "https://harkers.dsar/schemas/third_party_denylist.schema.json",
  "type": "object",
  "required": ["schema_version", "controller", "populated_at", "entries"],
  "properties": {
    "schema_version": {"const": 1},
    "controller": {"type": "string"},
    "populated_at": {"type": "string", "format": "date-time"},
    "operator_id": {"type": "string"},
    "entries": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["canonical_name", "redact"],
        "properties": {
          "canonical_name": {"type": "string"},
          "redact": {"type": "boolean"},
          "operator_note": {"type": "string"},
          "people_register_cluster_id": {"type": "string"}
        }
      }
    }
  }
}
```

### 3.3 `schemas/pii_jury_verdict.schema.json`

```json
{
  "$id": "https://harkers.dsar/schemas/pii_jury_verdict.schema.json",
  "type": "object",
  "required": ["doc_ref", "juror", "model_alias", "manifest_signature_id",
               "prompt_canonical_seal_sha256", "schema_version", "verdict",
               "correlation_id", "ts"],
  "properties": {
    "doc_ref": {"type": "string"},
    "juror": {"enum": ["A", "B"]},
    "model_alias": {"type": "string"},
    "model_repo": {"type": "string"},
    "manifest_signature_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "quantisation": {"type": "string"},
    "temperature": {"type": "number"},
    "prompt_id": {"type": "string"},
    "prompt_canonical_seal_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "prompt_effective_seal_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "schema_version": {"const": "ThirdPartyPiiCheck.v1"},
    "verdict": {
      "type": "object",
      "additionalProperties": false,
      "required": ["has_third_party_pii", "pii_categories", "example_tokens",
                   "severity", "confidence", "rationale"],
      "properties": {
        "has_third_party_pii": {"type": "boolean"},
        "pii_categories": {
          "type": "array",
          "items": {
            "enum": ["full_name","email","phone","address","postcode",
                     "id_number","ipv4","date_of_birth","other"]
          }
        },
        "example_tokens": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "severity": {"enum": ["none","low","medium","high"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "rationale": {"type": "string", "maxLength": 600}
      }
    },
    "correlation_id": {"type": "string", "format": "uuid"},
    "broker_endpoint": {"type": "string"},
    "infer_latency_ms": {"type": "integer"},
    "ts": {"type": "string", "format": "date-time"}
  }
}
```

## 4. Implementation phases (durant-style)

Same shape as the durant-pipeline-hardening project (6 phases, atomic tasks, per-task code-review-jury checkpoint).

### Phase 1 — Source-strategy registry + RFC822 harvest
- `dsar_pipeline/source_strategies/{base,exchange_nested,exchange_flat,sharepoint,raw_dump}.py`
- `dsar_pipeline/build_people_register.py` rewritten to delegate to strategies
- Tests: per-strategy unit tests with synthetic fixtures
- Output: `build_people_register` now produces a non-empty register on flat-Exchange dumps

### Phase 2 — `subject_centricity_score` + cluster aggregation
- `dsar_pipeline/people_register.py` extended for `subject_centricity_score`
- `dsar_pipeline/gates/subject_protection.py` — shared validator with hash-keyed cache
- `working/.case_secret` initialisation + HMAC infrastructure
- Tests: validation cache correctness, hash invalidation, HMAC tamper-detection

### Phase 3 — `sig_block_discovery_stage` + operator console route
- `dsar_pipeline/sig_block_discovery_stage.py`
- `dsar_orchestrator/operator_console.py` new route `/people-register` (model after `/flag-review`)
- Tests: regex correctness, console route HTTP smoke test
- Output: `working/third_party_denylist.json` populated post-operator-review

### Phase 4 — Redactor integration
- `dsar_pipeline/redact_stage.py` reads denylist at load, runs continuous subject-protection validation
- Tests: forced-redact on denylist tokens regardless of NER verdict; subject-protection cross-check correctness

### Phase 5 — `pii_jury_review_stage` + Article 30 ROPA events
- `dsar_pipeline/pii_jury_review_stage.py`
- 2D NER × text_quality stratification matrix
- Dual-juror opt-in + auto-promotion heuristic
- Tests: stratified sampling correctness (deterministic seed for CI), pydantic schema validation, ROPA event completeness

### Phase 6 — Conductor enforcement + threat-model
- `dsar_orchestrator/pipeline.py::_run_people_register_preflight`
- `dsar_orchestrator/pipeline.py::_check_extraction_quality`
- `dsar_orchestrator/pipeline.py::_verify_threat_model`
- CaseConfig fields
- `dsar-conductor verify --check people_register` subcommand
- Status dashboard updates (`~/dsars/runbooks/case-<id>-status.sh`)
- Runbook template updates (Phase 0 mandatory pre-redact step)

Each phase ends with code-review-jury checkpoint (LiteLLM frontier model panel) per CLAUDE.md amendment.

## 5. Runbook updates (the operator-facing change)

New mandatory **Phase 0** prepended to every release runbook:

```markdown
## Phase 0 — People-register pre-flight (MANDATORY)

1. **Build the register**:
   dsar-build-people-register --case "$CASE_ID"

2. **Verify identity-set sufficiency** (NOT blanket coverage %):
   dsar-conductor verify --case "$CASE_ID" --check people_register
   Expected: ✓ Source strategy: <X> (priority/confidence), N third-party clusters,
   1 subject cluster, 0 SubjectInDenylistError, K refs in manual-preprocessing-queue.

3. **Operator review** at /people-register console route:
   dsar-operator-console --case "$CASE_ID" --port 8089
   - Review top-50 ranked clusters (frequency × distinct-doc × (1 - subject_conf))
   - Review subject_referent_candidates separately (advisory)
   - Bulk-accept controller-domain emails as third-party-redact
   - Mark controller's published main number as preserve if desired

4. **Threat-model** at working/threat_model.md must exist and validate:
   Required sections: Embed endpoint / Isolation posture / Denylist scope /
   Per-engagement data flow / Subject identifier handling

5. **Gate confirmation**: conductor refuses dsar-conductor run until:
   - working/people_register.json exists with ≥1 third-party cluster on
     non-empty-communicant corpora
   - working/third_party_denylist.json validates against subject (no fuzzy match)
   - working/threat_model.md sections complete

   Override (synthetic test cases): case_config.force_skip_people_register_reason
   = "<explicit text>" + audit event recorded.
```

Existing Phase 6 (DPO sign-off) extended: closure letter checklist gains item
"People-register pre-flight passed (Phase 0)".

Status dashboard gets a new section:
```
=== People register coverage ===
  ✓ working/people_register.json present (last built ...)
  source strategy: <X> (confidence Y)
  entries: 247 persons / 412 emails / 89 phones
  data_subject: 1 cluster (X aliases)
  third_party: 246 clusters
  ✓ pre-redact gate: PASSED
```

If missing → verdict line flips to: `✗ BLOCKED — run dsar-build-people-register --case <id>`.

## 6. Risk register

| Risk | Mitigation |
|---|---|
| Body-text NER over-harvest floods operator review with incidental mentions | Scoped to headers + sig regions ONLY; body-text NER stays in redactor per-token pass |
| Multi-line signature patterns missed by single-line search | `email_reply_parser` library + char-offset window supplement to line proximity |
| Subject's own name accidentally redacted (denylist contains subject alias) | Continuous subject-protection validation at every consumer load; fuzzy match via embed.cosine |
| Operator-mid-flow edit of denylist bypasses build-time check | Continuous load-time validation, not just write-time |
| Path-traversal via symlinked case dir | `_safe_case_path` uses `Path.relative_to` containment; pre-rejects abs paths / `.` / `..` / null bytes |
| Two-juror local LLM cost on 10k+ corpora | Single-juror default; dual-juror opt-in / auto-promote; 2D stratified sampling |
| `subject_centricity_score` false-positive suppresses redaction → leak | Score is ADVISORY only; never auto-suppresses; operator must explicitly approve |
| Strategy collision (multiple strategies report high confidence) | Tie-break: confidence → priority → alphabetical name (deterministic, no operator block) |
| OCR-failure docs flood the jury (high-NER-density assumption inverted) | OCR_FAILURE pre-filtered to manual queue; 2D stratification on (ner × text_quality) |
| Non-English signatures bleed into review | `langdetect` → fallback parser; confidence <0.5 surfaces to operator |
| Embedding endpoint misconfigured as public → subject vectors leak | Threat model mandatory; `mlx-broker` local-only on 127.0.0.1; subject vectors never leave case dir |
| Cache tampering bypasses subject-protection check | HMAC-signed cache entries; per-case secret; atomic write |
| `mlx-broker` unavailable during `pii_jury_review_stage` | Stage halts with `MlxBrokerUnreachableError`; conductor refuses to advance to `final_synth`; operator must restart broker. NO auto-fallback to a different model (would invalidate Article 30 ROPA reproducibility). |
| Operator misconfigures `mlx-broker` to bind 0.0.0.0 → potential exfil | Threat model `Embed endpoint` section explicitly requires 127.0.0.1 binding. Conductor pre-flight verifies broker reachable on loopback only (not via the public interface) before invoking jury. |

## 7. Open / [POLISH] items (tracked for implementation phase + v2)

1. **[POLISH]** Cache LRU memory tracker — Python `lru_cache` is entry-count-based; need custom wrapper measuring serialised size against `subject_protection_cache_max_mb` (Gemini R6)
2. **[POLISH]** `random_sample` reproducibility seed for CI determinism (Kimi R6)
3. **[POLISH]** Metrics emission for OCR success/failure split ratio (Qwen-Coder R6)
4. **[POLISH]** `_write_manual_queue` idempotency — re-run safety (Kimi R6)
5. **[POLISH]** `_safe_case_path` TOCTOU residual — pre-reject abs paths / `.` / `..` / null bytes before resolve (Kimi R6)
6. **[POLISH]** Char-offset window calibration for `min(5 lines, 250 chars)` proximity (Gemini R5)
7. **[v2]** `subject_centricity_score` weights `0.6/0.4` + threshold `0.7` calibrated against labelled corpus (10-50 ground-truth clusters across 2-3 engagements). Until then advisory-only.
8. **[v2]** HMAC key rotation via `dsar-conductor rotate-case-secret --case <id>` (R5)
9. **[v2]** Cross-engagement controller gazetteer — if same controller across multiple cases, share validated third-party identities behind an explicit operator opt-in + audit trail (per-engagement isolation rule remains the default)
10. **[v2]** Body-text NER feedback loop — when PII jury repeatedly flags a token pattern across cases, suggest adding to the toolkit baseline regex
11. **[v2]** Multilingual signature parser library evaluation — extend beyond `email_reply_parser` (English-centric) for non-English signature handling

## 8. Acceptance criteria

This design ships v1 when, on a fresh DSAR case (no prior people_register):

1. `dsar-conductor run --case <id>` **refuses** to start redact until the conductor pre-flight passes (auto-runs `build_people_register` if needed; refuses on empty register + non-empty corpus)
2. `build_people_register` produces a non-empty register on:
   - Exchange-nested dump (uses `exchange_nested` strategy)
   - Exchange-flat dump (uses `exchange_flat` strategy with RFC822 fallback)
   - SharePoint dump (uses `sharepoint` strategy)
   - Raw file dump (uses `raw_dump` fallback strategy)
3. `working/people_register.json` validates against `schemas/people_register.schema.json`
4. Operator can review + curate the register via `/people-register` console route
5. The continuous subject-protection cross-check raises `SubjectInDenylistError` when a denylist entry fuzzy-matches a subject identifier
6. Cross-check uses the hash-keyed cache (verified by performance: second load is <50ms regardless of denylist size)
7. `pii_jury_review_stage` produces verdicts conforming to `ThirdPartyPiiCheck` pydantic schema for the docs sampled per the 2D matrix
8. Every `pii_jury_review` verdict carries the Article 30 ROPA fields (model_alias, manifest_signature_id, prompt seal hashes, correlation_id)
9. Status dashboard correctly reports gate state; runbook Phase 0 enforced
10. On the recreated `301770_v4_full_corpus`-shape input (flat Exchange root, 850+ in-scope docs, mistral-detected third-party names), the new pipeline produces a release pack with **zero** instances of `TALENT ACQUISITION PARTNER` + `Office: +44 1748 828800` leak class

## 9. Notes for the implementer

- Test fixture corpus: re-use the existing `301770_v4_full_corpus` snapshot in `output.pre-rerun-2026-05-27.bak/` + `audit/` as the regression corpus for end-to-end testing (sanitise subject identifier before committing to test data)
- Calibration corpus for `subject_centricity_score`: collect 50 cluster examples flagged by operator from this case + future cases; label each as `subject_referent_yes` / `subject_referent_no` / `subject_referent_ambiguous`; tune weights against labelled set in v2
- Test pattern for the continuous subject-protection cache: build, load (cache miss → re-embed), load again (cache hit → fast), modify denylist (cache miss → re-embed + raise), modify subject (cache miss → re-embed + raise)
- For Phase 0 runbook step 3 (operator review), reuse the BLK-003 `flag_review` console code as the structural template — same modal pattern, same accept/reject/merge actions

## 10. Cross-references

- Prior hardening project: `2026-05-26-durant-pipeline-hardening-design-v1.md` — same shape, same 6-phase pattern, builds on calibration-gated recheck stage
- Real-case post-mortem driving this spec: case 301770_v4_full_corpus, see `audit/` directory + `output.excluded-pii-jury-*` snapshots
- Brainstorm-jury synthesis: 6 rounds, validated via cloud LiteLLM panel (Kimi / Gemini / Qwen-Coder); Gemini approve stable R4 + R6; locked in by operator decision
- Code-review-jury checkpoints at each phase per CLAUDE.md machine-wide amendment (5-frontier-model panel)

**[v2 / v3 candidates beyond this spec]**

- `subject_centricity_score` calibration corpus
- Cross-engagement controller gazetteer with operator opt-in
- HMAC key rotation
- Body-text NER feedback loop
- Multilingual signature parser ecosystem
- Stronger TOCTOU defence on cache file IO
