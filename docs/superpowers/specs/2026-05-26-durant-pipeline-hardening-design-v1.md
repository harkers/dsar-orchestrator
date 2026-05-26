# Durant pipeline hardening — design v1

| Version | Date | Author | Notes |
|---|---|---|---|
| v1 | 2026-05-26 | Claude (Opus 4.7, 1M ctx) | Initial design covering §§1–7 — derived from 3-frontier-model jury review of `docs/durant-test.md` (round 0). Each section iterated through jury rounds; lock criterion ≥2/3 approve. §§4 and §6 locked at 1/3 with documented residuals after design-fundamentals approval. |

## 1. Why

The 3-frontier-model jury (DeepSeek / GPT-OSS / Gemini) reviewed `docs/durant-test.md` (commit `067ae86`) and identified seven hardening targets in the existing Durant pipeline. Convergent findings:

1. **Prompt-drift**: `DURANT_SYSTEM_PROMPT` is copied verbatim into per-engagement bypass scripts; toolkit refinements don't propagate.
2. **Unconditional recheck**: the under-disclosure recheck runs on every `work_context_only` ref, doubling LLM cost regardless of model fitness.
3. **Blind tail-truncation**: `text[:max_text_chars]` drops biographical signals at the end of long threads.
4. **No upfront model-fitness gate**: calibration is post-hoc; a poorly-calibrated model can do real harm on a real case before any gate catches it.
5. **`role` missing from `data_subject.json`** (single reviewer): role-domain documents can't be disambiguated against the subject's organisational scope.
6. **Recheck output not propagated to `scope_verdicts.jsonl`** (process-breaking): the safety net's reclassifications never reach the redaction stage.
7. **`durant-test.md` reference doc** will drift out of sync as §§1–6 land unless an editorial process is established.

This spec covers all seven as a single "module-drop" deliverable: when implemented, the Durant pipeline gains a centralised prompt-asset system, calibration-gated recheck, smarter truncation, an upfront fitness canary, role-aware prompting, end-to-end recheck propagation, and a CI-enforced reference doc.

## 2. Scope

**In scope** (spans two repos):
- `dsar-toolkit` (`harkers/dsar-toolkit`): all toolkit-side code — prompt loader, recheck stage, truncation helper, canary CLI, Agent22 synthesis, prompt assets.
- `dsar-orchestrator` (this repo): conductor pre-flight checks, audit-verify CLI, `durant-test.md` updates + lint.
- Per-engagement bypass scripts: documented changes (CLI-based prompt consumption); not edited by this spec (engagement folders are isolated).

**Out of scope:**
- Operator-calibration portal itself (§9 of `durant-test.md`); this spec consumes its output format.
- Cross-deployment calibration sharing (per-machine cache only).
- Auto-tuning of prompts or models from canary/calibration failures.
- Multi-role subjects; per-document role versioning.
- PKI signing of prompt assets or reports (threat model is accidental drift, not adversarial).
- Multipass recheck-of-recheck.
- Streaming / sharded processing for cases >100k docs (current design assumes operator-workstation scale).

## 3. Threat model

The pipeline runs on operator-controlled workstations. The threat model is **accidental drift, misconfiguration, and silent regression**, not adversarial input or insider tampering. Filters and integrity checks are anti-confusion / anti-rot safeguards; cryptographic signatures, RBAC, and sandboxing are explicitly deferred.

The LLM is an internal participant in the operator's workflow, not an adversarial channel. Documents in `working/<ref>.txt` are operator-curated case corpus; not user-submitted input.

## 4. Component summary

| § | Component | Repo | Lock status |
|---|---|---|---|
| 4.1 | Centralised prompt asset + loader | dsar-toolkit | ≥2/3 approve (R4) |
| 4.2 | Calibration-gated recheck stage | dsar-toolkit | ≥2/3 approve (R6) |
| 4.3 | Smarter truncation helper | dsar-toolkit | ≥2/3 approve (R7) |
| 4.4 | Model-fitness canary | dsar-toolkit + orchestrator | 1/3 approve + fundamentals (R6) |
| 4.5 | Subject `role` field | dsar-toolkit | ≥2/3 approve (R9) |
| 4.6 | Recheck → `scope_verdicts.jsonl` synthesis | dsar-toolkit | 1/3 + fundamentals (R9) |
| 4.7 | Reference-doc update process + CI lint | dsar-orchestrator | Principles-only (R6) |

---

## 4.1 Centralised prompt asset + loader

### Problem

`DURANT_SYSTEM_PROMPT` lives as a Python string constant in `dsar_pipeline/gates/gate_durant.py`. Per-engagement bypass scripts hold a verbatim copy with a "diff at release time" provenance comment — human discipline only.

### Design

**(A) Asset format.** `dsar-toolkit/src/dsar_pipeline/gates/prompts/durant.system.md`:

```markdown
---
prompt_id: durant.system
version: 1.0.0
seal_sha256: <hex>
droppable_blocks: [placeholder-tokens]
---

You are a UK data-protection adjudicator applying the Durant v FSA …

<!-- block:placeholder-tokens -->
# About placeholder tokens in this prompt
…
<!-- endblock -->

[rest of body]
```

- LF-only line endings (CI-enforced).
- File ends with exactly one `\n` (CI-enforced).
- Block delimiters are HTML comments — invisible to the LLM, robust to heading text edits.
- Frontmatter values `prompt_id` and `version` are explicitly strings (CI lint asserts; catches accidental YAML number coercion like `1.10 → 1.1`).

**(B) Seal computation.** Covers full canonical frontmatter (sans `seal_sha256` itself) + body. Forward-compatible — any added field auto-protected.

```python
def compute_seal(meta: Mapping, body: str) -> str:
    sealed_meta = {k: v for k, v in meta.items() if k != "seal_sha256"}
    canonical = yaml.safe_dump(sealed_meta, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(
        (canonical + "\n---\n" + body).encode("utf-8")
    ).hexdigest()
```

`yaml.safe_dump` (not `json.dumps`) handles arbitrary YAML types without `TypeError`.

**(C) Loader.** `dsar_pipeline.gates.prompt_loader`:

```python
@dataclass(frozen=True)
class PromptAsset:
    prompt_id: str
    version: str
    canonical_seal_sha256: str       # canonical frontmatter+body seal
    effective_sha256: str            # hash of body after applied strips + whitespace normalisation
    applied_strips: tuple[str, ...]
    body: str                        # actually returned to caller

class PromptIntegrityError(Exception): ...

class PromptLoader:
    @classmethod
    @functools.lru_cache(maxsize=64)
    def load(cls, prompt_id: str, *,
             strip_sections: tuple[str, ...] = ()) -> PromptAsset:
        ...
```

Behaviour:
- Read bytes; assert no `\r`; UTF-8 decode.
- Parse frontmatter via `yaml.safe_load`.
- Recompute seal; mismatch → `PromptIntegrityError(asset_path, expected_hex, actual_hex)` (full 64-char hex; no truncation).
- For each strip section: assert in `droppable_blocks`; locate `<!-- block:id --> … <!-- endblock -->`; remove inclusive; raise on missing markers.
- Post-strip whitespace normalisation: collapse 3+ consecutive newlines to 2.
- Compute `effective_sha256` from stripped body.
- `lru_cache` keyed on `(prompt_id, strip_sections)` — thread-safe via CPython.

`GateDurant.__init__` switches from importing the constant to `PromptLoader.load("durant.system")`. **No back-compat shim.** Bypass scripts that imported the constant directly are explicit migration targets (CLI or loader).

**(D) Bypass-script consumption.** CLI `dsar-prompt <prompt_id> [--strip-section <id> …]`:

Stdout byte layout — strict spec:
```
<body bytes exactly as Loader.body returns>
\n
# __dsar_prompt_meta__ canonical_seal_sha256=<hex> effective_sha256=<hex> applied_strips=<csv> prompt_id=<id> version=<ver>
\n
```

Bypass-script consumption:
```python
data = subprocess.run([...], capture_output=True).stdout  # bytes
sep = b"\n# __dsar_prompt_meta__ "
idx = data.rfind(sep)
if idx < 0: raise PromptIntegrityError("CLI footer missing")
body_bytes = data[:idx]
footer_line = data[idx + 1:].rstrip(b"\n").decode("utf-8")
meta = parse_footer(footer_line)
runtime_effective = hashlib.sha256(body_bytes).hexdigest()
if runtime_effective != meta["effective_sha256"]:
    raise PromptIntegrityError("bypass: runtime hash mismatch")
```

Audit row records `prompt_canonical_seal_sha256`, `prompt_applied_strips`, `prompt_effective_sha256` (runtime-computed), `prompt_id`, `version`, `prompt_source` (`"toolkit_cli"` or `"vendored@<toolkit_version>"`).

**Vendored mode.** Engagements without `dsar-toolkit` installed bundle `dsar-prompt-vendored.pyz` (reproducible zipapp: `SOURCE_DATE_EPOCH=0`, sorted file ordering, vendored `PyYAML`, hardcoded `gzip.GzipFile(compresslevel=6, mtime=0)`). Zipapp contains: `prompt_loader.py`, the `dsar-prompt` entry script, all `prompts/*.md`, `_registry.json`, and a `VENDOR_MANIFEST.json` with toolkit version + included-prompts manifest.

**(E) CI lint.**
- `test_prompt_canonical_sha256_matches` (per asset): parse, recompute seal, assert equal.
- `test_prompt_id_unique` across `prompts/*.md`.
- `test_block_ids_unique` per file; matching `<!-- endblock -->`.
- `test_droppable_blocks_have_markers`: every `droppable_blocks` entry exists in body.
- `test_assets_are_lf_only`: scan for `\r`.
- `test_assets_end_with_single_newline`.
- `test_metadata_string_types`: `prompt_id` and `version` are `str`, not `int`/`float`.
- `test_registry_is_current`: run `bin/build-prompt-registry` in-memory; assert no diff from committed `_registry.json`.
- `test_registry_is_append_only`: diff against `origin/main`'s `_registry.json`; no entries removed or mutated. Baseline path overridable via `PROMPT_REGISTRY_BASELINE`; **fails-closed if no baseline** (no silent skip). Documented: CI must `fetch-depth: 0`.
- `test_archive_exists_per_registry_entry`: every registry record has a `_archive/<prompt_id>/<version>.md.gz` with matching seal.
- `test_zipapp_is_reproducible`: build twice; assert byte-identical.

**(F) Registry + archive.** `bin/build-prompt-registry`:
- Append-only `_registry.json`:
  ```json
  {"durant.system": [
     {"version": "1.0.0", "seal_sha256": "...", "archived_at": "2026-05-26"},
     ...
  ]}
  ```
- For each new `(prompt_id, version)`: append + write `prompts/_archive/<prompt_id>/<version>.md.gz` (full file).
- Existing record present with different seal → exit 1 ("version not bumped").
- Atomic write protocol: archive files written to `*.tmp` siblings + fsync + rename; then `_registry.json.tmp` written + fsync + renamed.
- `test_no_leftover_tmp_files`: walks the prompts tree post-build.

**`dsar-prompt sign <file>`**: computes seal, updates frontmatter via `ruamel.yaml` round-trip, atomic write (tmp + fsync + os.replace). `--bump {major,minor,patch,none}` — default `patch` if body changed; `none` allowed only if seal unchanged from prior registry entry. **Auto-converts CRLF to LF on input** (quiet fix during signing; CI still rejects CRLF on un-signed commits). Errors include full 64-char hex (no truncation).

**(G) Conductor verify.** `dsar-conductor verify --check prompt-versions [--strict] <case_dir>`:
1. Read `_registry.json` from installed `dsar-toolkit`.
2. For each row in `working/durant_verdicts.jsonl` (and §4.2's recheck file):
   - Lookup `prompt_canonical_seal_sha256` in registry → resolve to `(prompt_id, version)`.
   - Cross-check `prompt_id` matches registry entry's `prompt_id` (defence-in-depth past the seal check).
3. Load `_archive/<prompt_id>/<version>.md.gz`; parse; replay `applied_strips` against archived body; recompute effective; assert == audit row's `prompt_effective_sha256`. Mismatch → exit 2.
4. If row's canonical matches a registered older version (not current) → exit 0 + WARN (or fatal with `--strict`).

### Scope

**IN:** `durant.system` asset + loader + sign CLI + auto registry + archive + CI tests + conductor verify subcommand + vendored zipapp build.

**OUT:** migrating other toolkit prompts to this pattern (each future); HTTP-served prompts; runtime auto-update; PKI signing; signer-identity tracking; storage deprecation.

---

## 4.2 Calibration-gated recheck stage

### Problem

The under-disclosure recheck currently lives only in per-engagement bypass scripts, runs on every `work_context_only` ref, and there's no principled policy for when to skip it. Operators have no cost-safety knob.

### Design

**(A) Promote to toolkit stage.**
- `dsar_pipeline.gates.gate_durant_recheck.GateDurantRecheck(BaseGateAgent)` — per-doc LLM call; consumes ONLY refs the primary pass classified `work_context_only`. Uses §4.1 loader for `prompts/durant.recheck.system.md`. Recheck prompt asks the inverse question independently and does NOT include the primary verdict's rationale (confirmation-bias mitigation per `durant-test.md` §8).
- `dsar_pipeline.recheck_stage.RecheckStage(BaseStage)` — orchestrates, writes `working/durant_underdisclosure_recheck.jsonl`. Exposes `dsar-recheck` CLI.

**(B) Gating policy** (per-case YAML):

```yaml
recheck:
  mode: auto                       # auto | always | never
  fn_threshold: 0.10               # auto runs when CI upper > this
  calibration_max_age_days: 90
  max_concurrency: 4               # 1=serial, ≤32
  override_reason: ""              # required non-blank when mode != auto
  deployment_id: "<operator-set>"
```

Threshold semantics: `fn_threshold` is the maximum acceptable false-negative rate. **Recheck SKIPS only when 95% confident the true FN rate ≤ threshold** (`fn_rate_ci95[1] <= fn_threshold`). Wide CI → recheck runs. Recheck is the safety net; threshold is acceptable background risk.

```python
def decide_mode(cfg, cache_entry):
    if cfg.mode == "always": return ModeDecision("always", reason="mode_set_explicit")
    if cfg.mode == "never":  return ModeDecision("never", reason="mode_set_explicit")
    # auto
    if cache_entry is None: return ModeDecision("always", reason="calibration_cache_miss")
    if cache_entry.age_days() > cfg.calibration_max_age_days:
        return ModeDecision("always", reason="calibration_stale")
    if seal_drift(cache_entry, primary_seal, recheck_seal):
        return ModeDecision("always", reason="calibration_prompt_seal_drift")
    ci_upper = cache_entry.fn_rate_ci95[1]
    if ci_upper > cfg.fn_threshold:
        return ModeDecision("always", reason="ci_upper_above_threshold", entry=cache_entry)
    return ModeDecision("never", reason="ci_upper_below_threshold", entry=cache_entry)
```

Stage `__init__` validates `mode=never` has non-blank `override_reason` (post `(cfg.override_reason or "").strip()`); raises `ConfigError` early.

**(C) Calibration cache (READ).** Location resolution: `DSAR_CALIBRATION_REGISTRY` env → `case_config.recheck.calibration_registry_path` → `~/.dsar/calibration_registry.json`. S3/blob URIs supported via toolkit's existing storage abstraction.

```json
{
  "schema_version": 1,
  "entries": [{
    "schema_version": 1,
    "deployment_id": "...",
    "model_alias": "mini@mlx",
    "primary_prompt_seal_sha256": "...",
    "recheck_prompt_seal_sha256": "...",
    "calibrated_at": "ISO-8601",
    "sample_size": 120,
    "fn_rate": 0.42,
    "fn_rate_ci95": [0.34, 0.51],
    "source_case_id": "..."
  }]
}
```

Strict per-entry validation: `schema_version == 1`; `fn_rate_ci95` is a list of exactly 2 floats in `[0.0, 1.0]` with `lo ≤ hi` (JSON schema `minItems:2 maxItems:2`); `fn_rate` in `[lo, hi]`; hex fields 64-char lowercase. Any failure → entry skipped + warning.

Registry loaded **once at stage init**, not per-doc. Local file errors: `FileNotFoundError` → silent None; `PermissionError` → ConfigError (loud); other OSError → propagate (disk/FS failure operator-visible). Remote read: retry 3× with uniform-random jitter `[0.5–1.5, 1.0–3.0, 2.0–6.0]`; `RemoteResourceNotFound` (404/NoSuchKey) is terminal (no retry).

Multi-match tie-break: `max(calibrated_at, sample_size, source_case_id or "")` for full lexicographic determinism.

Hash comparison via `_normalise_hash` (`.strip().lower()`).

**(D) Decision logging.** `working/recheck_decision.json`:
```json
{
  "mode_requested": "auto",
  "mode_effective": "always" | "never",
  "reason": "mode_set_explicit | calibration_cache_miss | calibration_stale | calibration_prompt_seal_drift | ci_upper_above_threshold | ci_upper_below_threshold",
  "calibration_entry_used": null | {...},
  "fn_threshold": 0.10,
  "decided_at": "ISO-8601"
}
```

`recheck_decision.json` is the **canonical "stage ran" marker**. Distributed deployments (S3 sync) that ignore zero-byte files should key off this, not the recheck JSONL.

**(E) Cost telemetry.** `working/recheck_summary.json` with `mode_effective`, `docs_examined`, `docs_reclassified_to_biographical`, `docs_reclassified_to_ambiguous`, `docs_confirmed_wco`, `elapsed_sec_total`, `estimated_cost_usd`, `errors`.

`estimated_cost_usd` derived from `dsar_pipeline/config/pricing.json`:
```json
{
  "schema_version": 1,
  "entries": [
    {"model_alias": "claude-opus-4-7@anthropic", "in_per_1k_tokens_usd": 0.015, "out_per_1k_tokens_usd": 0.075},
    {"model_alias": "claude-opus-4-7@bedrock",   "in_per_1k_tokens_usd": ...},
    {"model_alias": "mini@mlx",                  "in_per_1k_tokens_usd": 0.0, "out_per_1k_tokens_usd": 0.0}
  ]
}
```

Provider distinction via alias convention (`@anthropic`, `@bedrock`, `@mlx`). If `model_alias` not in `pricing.json`: row's `estimated_cost_usd: null`; one-time warning per run. CI test asserts every `model_context.json` (§4.3) and `pricing.json` alias exists in the toolkit's model registry.

**(F) Output JSONL row.** Successful:
```json
{
  "case_id": "...",
  "doc_ref": "...",
  "recheck_verdict": "reclassify_to_biographical" | "reclassify_to_ambiguous" | "confirmed_work_context_only",
  "rationale": "...",
  "model": "mini@mlx",
  "prompt_id": "durant.recheck.system",
  "prompt_canonical_seal_sha256": "...",
  "prompt_applied_strips": [],
  "prompt_effective_sha256": "...",
  "elapsed_sec": 1.4,
  "error_state": null,
  "estimated_cost_usd": 0.0024,
  "token_safety_iterations": 0
}
```

Errored:
```json
{
  "recheck_verdict": null,
  "error_state": {
    "code": "model_unreachable" | "schema_validation_failed" | "empty_response" | "timeout" | "unknown",
    "message": "...",
    "raw": "<sanitised ≤200 chars>"
  },
  "estimated_cost_usd": null,
  ...
}
```

**Invariant:** `error_state != null` ↔ `recheck_verdict == null` (mutually exclusive, schema-enforced via `oneOf`).

`error_state.raw` sanitiser:
```python
_CRED_PATTERNS = [bearer/sk-/Basic/Authorization:/AWS_*KEY/://user:pass@host patterns]
def _sanitise_raw(s):
    s = s[:16384]                                # ReDoS bound
    for pat in _CRED_PATTERNS: s = pat.sub("[REDACTED]", s)
    return s[:200]                                # final cap
```

JSON schema for the row format: `dsar_pipeline/schemas/durant_recheck_row.schema.json`. CI validates fixtures.

**(G) Concurrency + thread-safe writer.** `RecheckStage` uses `ThreadPoolExecutor(max_workers=cfg.max_concurrency)`. Init validates `0 < max_concurrency ≤ 32`; values >16 emit warning. Per-call rate-limit/backoff lives in `RoleRouter` (not this layer).

```python
class JsonlAppender:
    def __init__(self, path: Path):
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        try:
            self._fh.close()
        except OSError as close_err:
            if exc_type is None:
                raise                            # close failure with no in-block exc → fail
            log.warning("close failed while in-block exc active: %s", close_err)
        return False
    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if len(line.encode("utf-8")) >= 512:    # POSIX PIPE_BUF (macOS/BSD min)
            raise RowSizeError(...)
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
```

CI `test_jsonl_row_under_pipe_buf` uses multi-byte UTF-8 payloads to verify byte-count enforcement. Documented: 512-byte cap is cross-platform; Linux-only deployments may raise via config (defaults stay portable).

**Rationale truncated to ~100 chars** to fit cap; documented constraint.

### Scope

**IN:** Gate + stage + `dsar-recheck` CLI; recheck prompt asset (via §4.1); gating policy + config; calibration cache READ; telemetry files; thread-safe JSONL writer; size-capped rows; JSON schema; sanitised `error_state`.

**OUT:** operator-calibration portal itself; cross-deployment calibration sharing; adaptive `fn_threshold` tuning; recheck on `ambiguous`; mid-pass cost-budget abort; auth/RBAC on `dsar-recheck`; push observability; S3 source-signature verification; multiprocessing safety; tempfile-rename JSONL atomicity; directory fsync.

---

## 4.3 Smarter truncation

### Problem

`gate_durant._load_ref_text()` does `text[:max_text_chars]` (blind tail-cut, default 8000). Biographical signals at the tail of long threads / appended attachments are dropped. The cap is set for small-model context but applied uniformly.

### Design

**(A) `truncate()` helper.** New `dsar_pipeline.gates.text_truncation`:

```python
_MARKER_FMT = "\n\n[... %d characters elided ...]\n\n"
_MARKER_FMT_MIN_LEN = len(_MARKER_FMT % 0)
_MIN_AVAIL = 200
_MAX_CONVERGE_ITERS = 12   # covers up to 10^12 char documents

@dataclass(frozen=True)
class TruncationResult:
    """
    elided_start / elided_end: indices into the ORIGINAL text such that
    text_original[elided_start:elided_end] is EXACTLY the portion not present
    in `truncated`. When no truncation occurred, both == original_char_count.
    """
    truncated: str
    mode: str
    original_char_count: int
    truncated_char_count: int
    elided_start: int
    elided_end: int

def truncate(text: str, max_chars: int, *,
             mode: str = "head_tail",
             head_ratio: float = 0.75,
             display_original_len: Optional[int] = None) -> TruncationResult:
    if not 0.5 <= head_ratio <= 0.95:
        raise ValueError(f"head_ratio out of range: {head_ratio}")
    orig = len(text)
    orig_for_marker = display_original_len if display_original_len is not None else orig
    if mode == "none":
        if orig > max_chars:
            raise ValueError(f"mode=none but text exceeds cap: {orig} > {max_chars}")
        return TruncationResult(text, "none", orig, orig, orig, orig)
    if orig <= max_chars:
        return TruncationResult(text, "none", orig, orig, orig, orig)
    if mode == "head_tail":
        head_n, tail_n, marker = _converge_sizes(max_chars, head_ratio, orig_for_marker)
        body = text[:head_n] + marker + text[-tail_n:]
        return TruncationResult(body, "head_tail", orig, len(body),
                                elided_start=head_n,
                                elided_end=orig - tail_n)
    if mode == "structure_aware":
        return _structure_aware(text, max_chars)
    raise ValueError(f"unknown truncation mode: {mode}")
```

**(B) `_converge_sizes` (fixed-point iteration, exact-size guarantee):**

```python
def _converge_sizes(max_chars, head_ratio, orig_for_marker):
    marker_len = _MARKER_FMT_MIN_LEN
    for _ in range(_MAX_CONVERGE_ITERS):
        avail = max_chars - marker_len
        if avail < _MIN_AVAIL:
            raise ValueError(
                f"max_chars={max_chars} too small for head+tail with marker"
                f" (need ≥ {marker_len + _MIN_AVAIL})")
        head_n = int(avail * head_ratio)
        tail_n = avail - head_n
        elided_for_display = max(0, orig_for_marker - head_n - tail_n)
        marker = _MARKER_FMT % elided_for_display
        if len(marker) == marker_len:
            assert head_n + len(marker) + tail_n == max_chars
            return head_n, tail_n, marker
        marker_len = len(marker)
    raise AssertionError(
        f"_converge_sizes failed in {_MAX_CONVERGE_ITERS} iterations; "
        f"this is a bug.")
```

**(C) Model-aware caps.** `dsar_pipeline/config/model_context.json`:
```json
{
  "schema_version": 1,
  "entries": [
    {"model_alias": "claude-opus-4-7@anthropic", "max_text_chars": 32000, "target_input_tokens": 8000},
    {"model_alias": "mini@mlx", "max_text_chars": 8000, "target_input_tokens": null},
    {"model_alias": "default", "max_text_chars": 8000, "target_input_tokens": null}
  ]
}
```

`GateDurant.__init__(max_text_chars=None)`; if `None`, look up by `model_alias`. Per-case override via `case_config.truncation.max_text_chars`. Unknown alias → use `default` + warning emitted ONCE per alias per process (module-level set + lock).

**(D) Token safety belt.** When tokenizer is available for the model:

```python
def truncate_with_token_check(text, char_cap, *, model_alias, router, ...):
    result = truncate(text, char_cap, ...)
    target_tokens = lookup_target_tokens(model_alias)
    if target_tokens is None or not router.has_token_counter_for(model_alias):
        return result, 0
    iterations = 0
    while iterations < 5:
        try:
            tokens = router.count_tokens(model_alias, result.truncated)
        except Exception as e:
            log.warning("token counter raised %s; shipping last", e)
            return result, iterations
        if tokens <= 0:
            return result, iterations
        if tokens <= target_tokens:
            return result, iterations
        scale = (target_tokens / tokens) * 0.95
        new_cap = max(int(char_cap * scale), _MARKER_FMT_MIN_LEN + _MIN_AVAIL)
        if new_cap == char_cap: return result, iterations
        char_cap = new_cap
        result = truncate(text, char_cap, ...)
        iterations += 1
    return result, iterations
```

Audit row records `token_safety_iterations`.

**(E) Structure-aware (opt-in, boundary-anchored).** `_split_email_thread` returns `(content, start_index, end_index)` tuples directly (no `find/rfind` fragility):

```python
def _structure_aware(text, max_chars):
    orig_len = len(text)
    try:
        if _looks_like_email_thread(text):
            msgs = _split_email_thread(text)        # returns [(content, start, end), ...]
            if len(msgs) >= 2:
                first = msgs[0]
                last = msgs[-1]
                # Invariant guards: first must anchor at 0, last must end at orig_len.
                if first.start != 0 or last.end != orig_len:
                    raise ValueError("first/last not anchored to source boundaries")
                if first.end > last.start:
                    raise ValueError("first/last overlap")
                elided_chars = last.start - first.end
                if elided_chars == 0:
                    raise ValueError("no middle to elide")
                struct_marker = (
                    f"\n\n[... {elided_chars} characters elided from middle "
                    f"of thread ...]\n\n"
                )
                # Strip boundary whitespace before composing (prevents redundant newlines).
                joined = first.content.rstrip() + struct_marker + last.content.lstrip()
                if len(joined) <= max_chars:
                    return TruncationResult(
                        joined, "structure_aware_email_2msg",
                        orig_len, len(joined),
                        elided_start=first.end,
                        elided_end=last.start)
    except (ValueError, AttributeError) as e:        # specific exceptions, not blanket
        log.debug("structure_aware parse failed: %s", e)
    return truncate(text, max_chars, mode="head_tail")
```

**(F) Subject-mention scan (audit-only).** After truncation, count case-insensitive substring matches of `data_subject.full_name`, `email`, and each `additional_emails` in `text[result.elided_start:result.elided_end]`:

```python
def count_subject_mentions_in_elided(text, result, data_subject) -> int:
    if result.elided_start >= result.elided_end:
        return 0
    elided = text[result.elided_start:result.elided_end].lower()
    total = 0
    for ident in [data_subject.get("full_name"), data_subject.get("email")] + (
            data_subject.get("additional_emails", []) or []):
        ident = (ident or "").strip().lower()
        if len(ident) < 3: continue
        total += elided.count(ident)
    return total
```

**NOT injected into the LLM prompt.** Audit row records `subject_mentions_in_elided: N` only. Operator review surfaces high-mention truncated docs for human attention.

**(G) Audit row additions.**
```json
{
  "truncation_mode": "none" | "head_tail" | "structure_aware_email_2msg" | ...,
  "original_char_count": 27432,
  "truncated_char_count": 7943,
  "subject_mentions_in_elided": 12,
  "token_safety_iterations": 0
}
```

### Scope

**IN:** `truncate()` helper, model_context.json (char + target tokens), token-aware safety belt, `structure_aware` best-effort 2-msg pattern with boundary-anchored invariants, audit-only subject-mention scan, warning-once helper, CI alias check.

**OUT:** tokenizer-as-primary truncation; PDF/Word structure parsers; multi-window scanning; compression of elided content; dynamic ratio from detected header/signature blocks.

---

## 4.4 Model-fitness canary

### Problem

Calibration (`durant-test.md` §9) computes the FN rate AFTER running a real case. A poorly-calibrated small model can do real damage on a real case before any gate catches it. We need an upfront fitness check.

### Design

**(A) Canary corpus.** Per-machine, operator-curated:
```
~/.dsar/canary_sets/<deployment_id>/
    canary_corpus.json     # {"version":1, "baseline_version":"...", "refs":[...]}
    refs/<ref>.txt
    truth.json             # {"<ref>": "biographical"|"work_context_only"|"ambiguous", ...}
```

Toolkit ships `examples/canary_baseline/` with 6+ Durant-classic patterns: clear bio, clear WCO, direct-addressee carve-out, mixed-ambiguous, long-thread-tail mention, signature-only mention. CI verifies the baseline's seal against a pinned value; edits require version-bump.

**(B) `dsar-fitness-canary --deployment-id <id> [--corpus-path <path>]`:**
- Runs primary `GateDurant` and (if recheck configured) `GateDurantRecheck` against the canary corpus.
- Writes `~/.dsar/fitness_reports/<deployment_id>/<timestamp>.json`.

**(C) Fitness criteria (per-case YAML):**
```yaml
fitness:
  min_agreement: 0.80           # wilson_lower(agreement) >= this
  max_fn_rate: 0.20             # wilson_upper(fn_rate) <= this
  max_fp_rate: 0.20
  max_ambiguous_ratio: 0.20     # ambiguous-on-definite-truth / successful-definite
  min_success_rate: 0.85
  required_corpus_min_size: 30
  min_class_eligible: 12        # each of bio/WCO truth class must have ≥12 refs
```

Pass requires ALL: corpus size, success rate, class sizes, Wilson lower agreement, Wilson upper FN, Wilson upper FP, ambiguous ratio. **Wilson 90% bounds with explicit zero-denominator guards** (return `None` when n=0; class-size check fires instead).

Worked math (defensive against under-sized corpora):

| Scenario | bio/WCO | FN | wilson_upper(FN) | Result |
|---|---|---|---|---|
| Balanced 30, perfect | 12/12 | 0 | ~0.18 | PASS |
| Balanced 30, 1 FN | 12/12 | 1 | ~0.27 | FAIL |
| Balanced 50, 1 FN | 20/20 | 1 | ~0.18 | PASS |

**(D) Class counts.** `n_biographical_truth` etc. counted from FULL corpus (all truth labels, including errored refs); `n_biographical_successful` (rate denominator) counts only successful refs. Closes the loophole where a class with many errors could slip past `min_class_eligible`. Separate fail codes for "corpus lacks X" vs "X refs errored".

**(E) Error decoupling.** LLM errors (`error_state` set) excluded from agreement/FN/FP calculations; counted only toward `success_rate`. Errors do NOT count as false negatives — that conflates infrastructure with model fitness.

**(F) Conductor pre-flight (`dsar-conductor run <case_dir>`):**

```python
def conductor_preflight(case_cfg):
    canary_path = Path(case_cfg.fitness_check.canary_set_path).expanduser()
    if not canary_path.exists():
        abort("canary set path not found")
    try:
        live_corpus_sha = compute_corpus_sha256(canary_path)
    except ValueError as e:                      # truth.json malformed, files missing, etc.
        abort(f"canary corpus invalid: {e}")
    primary_seal = PromptLoader.load("durant.system").canonical_seal_sha256
    recheck_seal = (PromptLoader.load("durant.recheck.system").canonical_seal_sha256
                    if case_cfg.recheck.mode != "never" else None)
    inference_params_sha = compute_inference_params_sha256(case_cfg)
    tuple_ = (case_cfg.fitness_check.deployment_id,
              case_cfg.model_alias,
              primary_seal, recheck_seal,
              live_corpus_sha,
              inference_params_sha)
    report = find_matching_report(tuple_)
    if report is None: abort(...)
    if report.corpus_sha256 != live_corpus_sha:   # explicit drift guard
        abort(f"corpus_sha256 drift: report={...} live={...}; rerun canary")
    if report.prompt_id != "durant.system": abort(...)
    if (utcnow() - report.generated_at).days > case_cfg.fitness_check.max_report_age_days:
        abort(...)
    if not report.passed:
        abort(f"fitness failed:\n  " + "\n  ".join(
            f"{f.kind}: {f.code} — {f.detail}" for f in report.fails))
```

`--auto-fitness` (opt-in): on missing/stale/failing, conductor inline-runs `dsar-fitness-canary` then proceeds on pass.

`--force-skip-fitness "<non-blank reason>"`: bypass; records `{reason, os_user, hostname, timestamp, fitness_tuple, last_known_report_id}` in `case_audit/skip_fitness.json`. CLI rejects empty reason.

**(G) Corpus hash.** `compute_corpus_sha256(path)`:
- Requires `canary_corpus.json` AND `truth.json` (raises `ValueError` if missing).
- Validates `truth.json` is non-empty JSON object.
- Deduplicated file set (explicit list ∪ `refs/*.txt` glob; deduplicated via `set()`).
- For `.json` files: canonicalize (`json.dumps(json.loads(content), sort_keys=True, separators=(",", ":"))`) before hashing — cosmetic edits don't break the seal.
- LF normalisation; `as_posix()` for path keys.

**(H) Report shape.** Includes `report_id`, `generated_at`, full tuple, `corpus_size`, structured `metrics` (success_rate, agreement, agreement_wilson_lower, fn_rate, fn_rate_wilson_upper, fp_rate, fp_rate_wilson_upper, ambiguous_rate_on_definite_truth, n_biographical_truth, n_biographical_successful, etc.), `criteria`, `passed`, structured `fails: [{code, kind:"corpus|model", detail}]`, per_ref array. Operator workflow: `kind=corpus` → expand canary; `kind=model` → improve prompt/model.

### Scope

**IN:** Canary corpus convention, `dsar-fitness-canary` CLI, report shape + archival, conductor pre-flight integration, `--auto-fitness` + `--force-skip-fitness` with mandatory audited reason, baseline canary in `examples/`.

**OUT:** Auto-tuning from canary failures; multi-corpus per deployment; cross-deployment sharing; stratified sampling enforcement; PKI signing.

### Locked at 1/3 with documented residuals

After 6 jury rounds, design fundamentals approved across rounds (DeepSeek R4, partial Gemini R5). Remaining R6 concerns are implementation-detail (try/except wrapping in pre-flight, per-component diff messages for drift) folded into v7 and will be caught by code-review-jury during implementation.

---

## 4.5 Subject `role` field

### Problem

`data_subject.json` has `full_name`, `aliases`, `email`, `additional_emails` but no `role`. A doc about "HR Policy" is biographical for an HR Director, work_context_only for an IT Admin. Without role context, the LLM applies Durant blindly.

### Design

**(A) Schema extension.** `data_subject.json` adds optional fields:
```json
{
  "role": "HR Director",
  "role_context": "Responsible for organization-wide HR policy; oversees disciplinary procedures; reports to CEO."
}
```

Both string|null; JSON schema enforces `role` ≤ 100 chars, `role_context` ≤ 500 chars. Existing files without these fields work unchanged.

**(B) Sanitisation pipeline.**
```python
_RAW_MAX_LEN = 2000          # DoS guard at sanitiser entry
_PRESERVE_CF = {"‌", "‍"}    # ZWNJ, ZWJ (Arabic/Indic/emoji)
def _preserve_variation_selector(c): return 0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF
_DROP_CATEGORIES_OTHER = {"Cc", "Cs", "Co", "Cn"}

_INJECTION_PATTERNS = [
    re.compile(r"\bignore\s+(?:previous|prior|above)\s+(?:instructions?|prompt|system)\b", re.I),
    re.compile(r"\b(?:system|assistant|human|user)\b[\s>#*_`~-]*:", re.I),  # markdown-aware chat-turn
    re.compile(r"<\|[^|]*\|>", re.I),
]

def sanitise_role_field(value, field_name):
    if not value: return None
    if len(value) > _RAW_MAX_LEN:
        raise ValueError(f"raw input > {_RAW_MAX_LEN}; likely paste error")
    normalised = unicodedata.normalize("NFKC", value)
    single_line = re.sub(r"[^\S\t]+", " ", normalised)     # whitespace→space; tab preserved
    cleaned = _strip_invisibles(single_line).strip()       # strip AFTER invisible-strip
    if not cleaned: return None
    if len(cleaned) > _FIELD_MAX_LEN[field_name]:
        raise ValueError(...)
    for pat in _INJECTION_PATTERNS:
        if pat.search(cleaned):
            raise ValueError(f"data_subject.{field_name}: anti-confusion filter matched")
    return cleaned
```

`_strip_invisibles` drops Cc/Cs/Co/Cn unconditionally; for Cf, preserves explicit allowlist (ZWJ/ZWNJ + variation selectors); strips bidi controls / ZWSP / soft hyphen / etc.

**Filters are anti-confusion safeguards, not security controls.** Documented residuals:
- Homoglyph substitution bypasses (Cyrillic ѕ vs Latin s).
- Linguistic paraphrase of "ignore previous instructions".
- Markdown-structural injection in `role_context`.
- Mid-paragraph `system:`/`user:`/`human:`/`assistant:` colons cause rejection (operator rephrases).

**(C) Prompt template** (USER prompt; §4.1 system prompt unchanged):

```
# Data subject
The data subject is Alice Smith (email: alice@example.com).

# Subject's organisational role
Role: HR Director
Context: Responsible for organization-wide HR policy; …

# How to apply the role
The role and context above give the subject domain visibility, but
documents ABOUT that role's domain are not automatically biographical
for the subject. A document is biographical only if it focuses on the
SUBJECT's specific actions, decisions, performance, or correspondence
— not on the role's broader remit. If the document text contradicts
the role context above, the document content is authoritative.
```

The "How to apply" block appears whenever `role` is set; conditional wording when `role_context` is absent.

**(D) Audit row.** `subject_role: str|null`, `subject_role_context: str|null` — both post-sanitisation strings; enables exact-reproduction debugging.

### Scope

**IN:** Optional `role` + `role_context` fields; prompt template change; audit fields; backwards-compat schema; sanitisation pipeline; raw-size DoS guard.

**OUT:** Auto-inference from corpus; multi-role subjects; per-document role versioning; role-based gating logic.

---

## 4.6 Recheck → `scope_verdicts.jsonl` synthesis

### Problem

Recheck output isn't currently wired into `Agent22.synthesise_verdict()`. Safety-net reclassifications never reach the redaction stage — process-breaking per DeepSeek's R0 finding.

### Design

**(A) `effective_durant()` helper.**
```python
def effective_durant(primary, recheck, recheck_err, recheck_mode):
    if primary == "present":   return ("present", "primary_biographical")
    if primary == "ambiguous": return ("ambiguous", "primary_ambiguous")
    # primary == "not_present" (WCO)
    if recheck_err is not None:
        code = _safe_extract_error_code(recheck_err)
        return ("ambiguous", f"recheck_errored:{code}")
    if recheck == "confirmed_work_context_only":
        return ("not_present", "recheck_confirms_wco")
    if recheck == "reclassify_to_biographical":
        return ("present", "recheck_reclassified_biographical")
    if recheck == "reclassify_to_ambiguous":
        return ("ambiguous", "recheck_reclassified_ambiguous")
    if recheck is None:
        if recheck_mode == "never":
            return ("not_present", "primary_wco_recheck_disabled")
        return ("ambiguous", "recheck_expected_but_missing_for_ref")
    return ("ambiguous", f"unknown_recheck_verdict:{recheck}")
```

**(B) `synthesise_verdict()` — preserves ambiguous, surfaces conflict, returns 3-tuple:**
```python
def synthesise_verdict(primary, recheck, recheck_err, recheck_mode, temporal):
    eff, reason = effective_durant(primary, recheck, recheck_err, recheck_mode)
    if temporal == "out_of_scope":
        if eff == "present":
            return ("ambiguous",
                    f"durant=present ({reason}) vs temporal=out_of_scope; operator should reconcile",
                    eff)
        if eff == "ambiguous":
            return ("ambiguous", f"durant=ambiguous ({reason}) + temporal=out_of_scope", eff)
        return ("not_present", f"effective_durant=not_present ({reason}); temporal=out_of_scope", eff)
    if eff == "not_present": return ("not_present", f"effective_durant=not_present ({reason})", eff)
    if eff == "present" and (temporal == "in_scope" or temporal is None):
        return ("present", f"effective_durant=present ({reason})", eff)
    return ("ambiguous", f"effective_durant={eff} ({reason}), temporal={temporal}", eff)
```

**Critical**: temporal=out_of_scope does NOT silently flatten ambiguous to not_present. Recheck-promoted refs with conflicting temporal escalate to ambiguous (operator-reconcilable).

**(C) Field names.** Recheck JSONL uses `recheck_verdict` and `error_state` (matching §4.2's schema exactly — no `durant_*` prefix in the merged record).

**(D) `scope_verdicts.jsonl` row:**
```json
{
  "case_id": "...",
  "doc_ref": "...",
  "scope_verdict": "present|not_present|ambiguous",
  "evidence": {
    "durant_verdict": "biographical|work_context_only|ambiguous",
    "recheck_verdict": "...|null",
    "error_state": {"code": "...", "message": "...", "_extra": {...}} | null,
    "recheck_mode_effective": "always|never",
    "effective_durant": "present|not_present|ambiguous",
    "temporal_verdict": "in_scope|out_of_scope|null"
  },
  "decision_rationale": "..."
}
```

**(E) Pipeline orchestration in Agent22ScopeCheck.**
- Read primary `durant_verdicts.jsonl` (required; raises if missing).
- Build `recheck_by_ref` and `temporal_by_ref` via unified `_build_index_first_wins` helper (collision → first wins + warning; missing file → empty dict; OSError → empty dict + error logged).
- `_iter_jsonl_safe` uses `with` block + outer `except OSError` covering both open and read; per-line guards: line >1MB skipped+warned; non-UTF-8 skipped+warned; malformed JSON skipped+warned.
- Read `recheck_decision.json` for `mode_effective`. Missing/malformed → default `"always"` + warning (safe — under-disclosure is worse error).
- Atomic output write: tmp + `os.replace`; `try/finally` cleans tmp on exception.

**(F) `SynthesisSummary`** counts: `primary_*`, `recheck_promoted` (WCO→bio), `recheck_escalated` (WCO→amb), `recheck_confirmed`, `recheck_errored`, `recheck_missing_anomaly`, `primary_wco_recheck_disabled`, `recheck_other`, `scope_*`, `temporal_out_blocked`, `temporal_recheck_conflict`. `recheck_other` counts unexpected recheck rows for non-WCO primaries (architectural invariant monitor).

**(G) `_trim_error_state` (audit footprint).** Drops `raw` field (full trace lives in recheck JSONL per §4.2); preserves `code` + `message`; unknown keys → `_extra` (type-agnostic via `_truncate_any` returning `(value, was_truncated)`; large containers via cheap-size descriptor `"list[N items]"`); preserves untruncated mutable types via `copy.copy()` defensive copy. Outer write uses `default=str` for any non-serialisable residuals.

```python
_TRIM_VALUE_MAX_CHARS = 400
_TRIM_EXTRA_MAX_KEYS = 8
_LARGE_CONTAINER_THRESHOLD = 1000

def _truncate_any(v, max_chars=_TRIM_VALUE_MAX_CHARS) -> tuple[Any, bool]:
    if isinstance(v, str):
        if len(v) > max_chars: return (v[:max_chars-1] + "…", True)
        return (v, False)
    if isinstance(v, (list, tuple, dict, set)):
        try:
            n = len(v)
            if n > _LARGE_CONTAINER_THRESHOLD:
                descriptor = f"{type(v).__name__}[{n} items]"
                return (descriptor, True)
        except TypeError: pass
    try: s = str(v)
    except Exception as e:
        return (f"<str_failed:{type(v).__name__}:{type(e).__name__}>", True)
    if len(s) > max_chars: return (s[:max_chars-1] + "…", True)
    return (copy.copy(v), False)            # defensive copy on preservation
```

### Scope

**IN:** Agent22 input-record extension, `effective_durant` helper, updated `synthesise_verdict`, `scope_verdicts.jsonl` schema additions, `synthesis_summary.json`, atomic orchestration, unified index builder.

**OUT:** Operator-review UI prioritisation; mid-pipeline re-redaction triggers; multi-pass recheck-of-recheck; temporal-gate recheck (temporal is deterministic).

### Locked at 1/3 with documented residuals

After 9 jury rounds, design fundamentals approved across rounds (Gemini R3 + R9). Remaining R9 concerns are micro (mutable-return defensive copy — added; container-OOM under adversarial input — out of scope under operator-trust threat model).

---

## 4.7 Reference doc updates + CI lint

### Goal

After §§4.1–4.6 are implemented, `docs/durant-test.md` (this repo) must accurately reflect post-hardening behaviour. Operators rely on it as the canonical reference; drift between doc and code is a regulator-visible risk.

### Design

**(A) Incremental update process.** Each implementation PR for §§4.1–4.6 includes the corresponding `durant-test.md` edit inline. No "big-bang final editorial PR." If a spec is delayed, the doc stays accurate to what's deployed.

Per-spec doc touchpoints:
| Spec | durant-test.md sections updated |
|---|---|
| §4.1 prompt-loader | §6 (Toolkit canonical), §7 (Local-broker bypass), §11 (Cross-references) |
| §4.2 recheck stage | §4 (Programmatic), §5 (Outputs), §8 (Under-disclosure recheck), §11 |
| §4.3 truncation | §3 (Inputs); adds new §3.1 (Truncation strategy) |
| §4.4 canary | Adds §9.0 (Model-fitness canary); renumbers existing §9 → §9.1; updates §10 |
| §4.5 role field | §3 (Inputs), §12 (Glossary) |
| §4.6 propagation | §5 (Outputs), §6, §10 |

**(B) CI lint script.** `tools/check_durant_doc.py` (Python; replaces the bash subshell-buggy approach):

Principles:
- **CommonMark parser** (`markdown-it-py`) used for code-region detection (not regex).
- **Mask code regions preserving newlines** (replace non-newline chars with spaces) so line numbers and `^`-anchored heading regex still work.
- **Single parse** of the document; tokens reused for heading detection and code masking.
- **Bisect for O(log n) line lookups** — precomputed line-starts once.
- **All three checks operate on `body_no_code`**: path linter, stale-phrase guard, required-term guard.
- **Rules in `docs/durant-doc-lint.yaml`** (separation of policy from script):
  - `bare_filename_allowlist`: bare filenames (`README.md`) only checked when in allowlist.
  - `stale_phrases`: phrase + reason + (optional) `obsolete_since_spec`. Reported with line numbers; all occurrences (via `re.finditer`).
  - `required_headings`: structural match against parser-extracted headings (case-insensitive); whitespace-immune.
  - `required_terms`: substring match against `body_no_code`.
- **Distinct exit codes**: 0 = pass; 1 = lint failure; 2 = config error (missing/malformed YAML, unreadable doc).
- Wired into orchestrator CI on every PR touching `docs/` or `src/dsar_orchestrator/`.

Inline-code masking detail (acknowledged residual: `markdown-it-py` inline tokens don't carry `map`; multiple approaches possible — implementation-phase code-review jury will pick the cleanest). Acceptable starting point: walk parent tokens and locate spans within `content`.

**(C) Conformance verification.** After each spec lands, the CI lint catches:
- Removed paths still referenced in doc.
- Legacy phrases like "single LLM call. No multi-pass refinement" (obsoleted by §4.2).
- Missing required headings (§3.1, §9.0).
- Missing required terms (`canonical_seal_sha256`, `effective_durant`, etc.).

**(D) Merge-conflict mitigation.** Spec PRs touching overlapping `durant-test.md` sections rebase on prior spec's doc edits; conflicts surface as text, not silent omissions.

### Scope

**IN:** Edits to `docs/durant-test.md` in this repo; new `tools/check_durant_doc.py`; `docs/durant-doc-lint.yaml`; CI integration.

**OUT:** New separate doc files (toolkit prompt-asset cookbook, canary corpus runbook, engagement-script migration guide).

### Locked at principles-only

After 6 jury rounds, principles are clear: CommonMark parser, externalised rules, line-accurate reporting, single parse, atomic exit codes. Implementation details (inline-code token mapping precisely, large-doc performance) deferred to the implementation-phase code-review jury per `~/.claude/CLAUDE.md`'s code-review-jury amendment.

---

## 5. Dependencies between sections

```
§4.1 (prompt asset) ──► §4.2 (recheck uses §4.1 loader)
                  ├──► §4.4 (canary's report tuple includes §4.1 seal hashes)
                  └──► §4.7 (durant-test.md cites the asset path)

§4.2 (recheck stage) ──► §4.4 (canary's tuple includes recheck seal)
                   └──► §4.6 (Agent22 consumes recheck JSONL)

§4.3 (truncation) ──► §4.4 (audit row's truncation fields fed into canary verdicts)
              └──► §4.7 (durant-test.md §3.1 documents this)

§4.4 (canary) ──► (no downstream toolkit dep; pre-flight gate only)

§4.5 (role field) ──► §4.1 (prompt template change in user prompt; §4.1 seal unchanged)
              └──► §4.4 (truth labels for role-aware corpus)

§4.6 (synthesis) ──► (terminal; consumes everything above)

§4.7 (doc updates) ──► (lands per-PR alongside each spec)
```

## 6. Phased implementation order

The dependencies above imply a partial order:

1. **Phase 1 — Foundations (parallelisable):**
   - §4.1 prompt asset + loader.
   - §4.3 truncation helper.
   - §4.5 role field + sanitisation.

2. **Phase 2 — Stages (depends on Phase 1):**
   - §4.2 recheck stage (uses §4.1, references §4.3 audit fields).
   - §4.6 Agent22 synthesis (depends on §4.2 JSONL schema).

3. **Phase 3 — Pre-flight gate (depends on Phases 1+2):**
   - §4.4 canary (validates against §4.1 + §4.2 prompts; outputs feed conductor pre-flight).

4. **Phase 4 — Doc + Lint (continuous, per-PR):**
   - §4.7 doc updates land in each PR; CI lint added as the first PR in Phase 1.

## 7. Open residuals

These are known limitations of the design accepted under the operator-trust threat model:

- **§4.1**: PKI signing of prompt assets; runtime auto-update; backwards-compat shim retired.
- **§4.2**: Adaptive `fn_threshold` tuning; recheck on `ambiguous`; mid-pass cost-budget abort.
- **§4.3**: Token-aware as primary truncation; PDF/Word structure-aware; multi-window LLM scans.
- **§4.4**: Auto-tuning from canary failures; cross-deployment fitness sharing; minimum-class-eligible vs Wilson trade-offs documented in spec body.
- **§4.5**: Filters are best-effort; homoglyph substitution and linguistic paraphrase not addressed; multi-role subjects deferred.
- **§4.6**: `_iter_jsonl_safe` is a streaming generator — mid-stream OSError yields partial index (acceptable on local FS; document for network-FS deployments).
- **§4.7**: Inline-code masking via `markdown-it-py` has implementation alternatives; selected at code-review time.

## 8. Implementation cost estimate (rough)

| § | Toolkit LoC | Orchestrator LoC | Test LoC | Days |
|---|---|---|---|---|
| 4.1 | ~600 | ~200 (verify CLI) | ~400 | 4-5 |
| 4.2 | ~500 | — | ~300 | 3-4 |
| 4.3 | ~300 | — | ~200 | 2-3 |
| 4.4 | ~400 | ~150 (preflight) | ~250 | 3-4 |
| 4.5 | ~150 | — | ~100 | 1-2 |
| 4.6 | ~250 | — | ~200 | 2 |
| 4.7 | — | ~200 (lint script) | ~150 | 1-2 |
| **Total** | **~2200** | **~550** | **~1600** | **~16-22** |

With concurrent dev (Phase 1 in parallel), real elapsed time ≈ 10–14 working days for a focused single contributor.

## 9. Acceptance criteria

The spec is implemented when:
- All toolkit CI tests pass on a fresh checkout.
- Orchestrator CI lint passes against an updated `docs/durant-test.md`.
- `dsar-conductor verify --check prompt-versions <case_dir>` succeeds on a fixture case run end-to-end.
- `dsar-fitness-canary` against the shipped baseline corpus passes for `claude-opus-4-7@anthropic` and a representative local MLX model.
- A fixture corpus exercising all seven hardening features produces a `scope_verdicts.jsonl` whose `synthesis_summary.json` records non-zero `recheck_promoted` (the safety net working).

---

## 10. Integration with existing code

This module **extends** existing code; it does not replace `gate_durant`, `scope_check_stage`, or `agent22_scope_check`. Every existing test under `tests/test_gate_durant.py`, `test_durant_prompt_template.py`, `test_scope_decisions.py` continues to pass — the changes are additive (loader-backed prompt, optional truncation strategies, optional recheck stage, etc.). Existing operator workflows (`dsar-pipeline`, `dsar-scope-check`, conductor's `scope_classify` stage) keep their interfaces.

### 10.1 dsar-toolkit changes

| File | Current state | Change |
|---|---|---|
| `src/dsar_pipeline/gates/gate_durant.py` | `GateDurant(BaseGateAgent)`, inline `DURANT_SYSTEM_PROMPT`, `max_text_chars=8000`, uses `RoleRouter` | **Refactor:** switch system prompt source to `PromptLoader.load("durant.system")`; remove the inline constant; switch `[:max_text_chars]` to `truncate_with_token_check(...)`. Existing `_build_user_prompt` stays, but conditionally appends §4.5 role section. Audit row in `working/biographical_refs.json` extended with seal hashes + truncation metadata + subject_mentions_in_elided. **No interface change.** |
| `src/dsar_pipeline/gates/gate_durant_recheck.py` | does not exist | **NEW:** `GateDurantRecheck(BaseGateAgent)` per §4.2. |
| `src/dsar_pipeline/gates/prompt_loader.py` | does not exist | **NEW:** §4.1 loader. |
| `src/dsar_pipeline/gates/text_truncation.py` | does not exist | **NEW:** §4.3 helper. |
| `src/dsar_pipeline/gates/prompts/` | does not exist | **NEW directory.** Houses `durant.system.md`, `durant.recheck.system.md`, `_registry.json`, `_archive/<id>/<version>.md.gz`. (Sibling to `config/prompts/scope_check.txt`, which is unchanged — that's a different routing-role prompt.) |
| `src/dsar_pipeline/scope_check_stage.py` | `ScopeCheckStage(BaseStage)` orchestrates `gate_temporal_scope ∥ gate_durant` via `GateRunner` → writes `scope_verdicts.jsonl` | **Extend:** after primary `gate_durant` runs, conditionally drive `RecheckStage` over the `work_context_only` refs. The synthesis call inside this stage now reads §4.2 recheck output before producing scope_verdicts. `ScopeCheckStage.summary_filename` unchanged; the toolkit-side `dsar-scope-check` CLI entry-point unchanged. |
| `src/dsar_pipeline/recheck_stage.py` | does not exist | **NEW:** §4.2 stage subclassing `BaseStage`. `stage_label="durant_recheck"`. New entry in `VALID_STAGE_LABELS` (in `_stage_base.py`). |
| `src/dsar_pipeline/agents/agent22_scope_check.py` | `_synthesise_verdict(durant, temporal)` 2-arg, returns `(verdict, rationale)` | **Extend:** `synthesise_verdict(primary, recheck, recheck_err, recheck_mode, temporal)` 5-arg returning `(scope, rationale, effective_durant)` per §4.6. Backwards-compat helper `_synthesise_verdict` retained as a thin shim for any out-of-tree callers; deprecation warning. |
| `src/dsar_pipeline/llm_router.py` | `RoleRouter` with `call(role, system, user, …)` + per-call audit to `llm_calls.jsonl` | **Add method:** `RoleRouter.has_token_counter_for(model_alias) -> bool` and `RoleRouter.count_tokens(model_alias, text) -> int` for §4.3's safety belt. Cloud models (anthropic) use the SDK's `count_tokens`; local MLX returns `False` for now. No existing-caller break. |
| `src/dsar_pipeline/config/model_context.json` | does not exist | **NEW** (under-`config/` already houses `llm_routing.yaml` etc.). §4.3 entries. |
| `src/dsar_pipeline/config/pricing.json` | does not exist | **NEW.** §4.2 entries. |
| `src/dsar_pipeline/schemas/durant_recheck_row.schema.json` | does not exist | **NEW.** §4.2 row schema (sibling to existing `scope_verdict.schema.json`). |
| `src/dsar_pipeline/schemas/scope_verdict.schema.json` | exists; required keys: `ref, verdict, rationale, iteration, model, ts` | **Extend** the optional-properties block with `evidence.recheck_verdict`, `evidence.error_state`, `evidence.recheck_mode_effective`, `evidence.effective_durant`. Backwards-compat (existing rows still validate). |
| `bin/build-prompt-registry`, `bin/check-prompt-assets`, `bin/build-vendored-zipapp` | do not exist | **NEW** scripts per §4.1. Sibling to existing `bin/` content. |
| `pyproject.toml` `[project.scripts]` | `dsar-pipeline`, `dsar-signoff-console`, `dsar-session` | **Add:** `dsar-prompt = "dsar_pipeline.gates.prompt_loader:cli_main"`, `dsar-recheck = "dsar_pipeline.recheck_stage:main"`, `dsar-fitness-canary = "dsar_pipeline.fitness_canary:main"`. |
| `tests/test_prompt_assets.py`, `tests/test_gate_durant_recheck.py`, `tests/test_text_truncation.py`, `tests/test_fitness_canary.py`, `tests/test_role_field_sanitiser.py` | none | **NEW** test files. (The spec body referred to `tests/gates/...`; toolkit convention is FLAT `tests/test_*.py` — match that.) Existing `test_gate_durant.py`, `test_durant_prompt_template.py`, `test_scope_decisions.py` should continue to pass unchanged. |
| `examples/canary_baseline/` | does not exist | **NEW.** §4.4 baseline corpus + truth.json + canary_corpus.json. |

**Note on existing `biographical_refs.json` vs the spec's `durant_verdicts.jsonl`:** `GateDurant._persist_verdicts()` currently writes a single `working/biographical_refs.json` with `{biographical, work_context_only, ambiguous, per_ref}`. The spec's design talks about `durant_verdicts.jsonl` (one row per ref). **Decision:** keep writing `biographical_refs.json` for backwards compat AND additionally emit a per-ref JSONL row (`working/durant_verdicts.jsonl`) shaped per the spec. Existing consumers of `biographical_refs.json` (notably `agent22_scope_check`'s legacy path) keep working; new consumers (recheck stage, agent22's new synthesis) read the JSONL. Migration of `biographical_refs.json` to a derived view is a follow-up.

### 10.2 dsar-orchestrator changes

| File | Current state | Change |
|---|---|---|
| `src/dsar_orchestrator/pipeline.py` | `STAGE_ORDER` includes `scope_classify`; `run()` runs stages with `StageBanner` + `PipelineAuditor` | **Insert pre-flight hook** before `STAGE_ORDER[0]`: if `cfg.fitness_check.enabled` (new field), call new `_run_fitness_preflight(cfg)`. Failure raises `PipelineHalt`. No change to `STAGE_ORDER` itself; recheck happens INSIDE the toolkit's `scope_check_stage`, transparent to the conductor. |
| `src/dsar_orchestrator/config.py` | `CaseConfig` dataclass with `pii_classify_mode`, `rerank_mode`, … | **Add fields:** `fitness_check_enabled: bool = True`, `fitness_check_canary_path: Path | None = None`, `fitness_check_max_report_age_days: int = 30`, `force_skip_fitness_reason: str = ""`. All optional in the loaded YAML; defaults preserve current behaviour (fitness check ON by default, but an explicit miss is what aborts — operator can opt out via `force_skip_fitness_reason`). |
| `src/dsar_orchestrator/cli.py` | `dsar-conductor --case X [--from] [--through] [--only]` | **Add subcommands:** convert the current flat parser to argparse subparsers (`run` is the default, preserving today's CLI: `dsar-conductor --case X` continues to work because `run` is the default subcommand). Add `dsar-conductor verify --check {prompt-versions,fitness-report} --case X [--strict]`. Add `--auto-fitness` and `--force-skip-fitness "<reason>"` flags on the `run` subcommand. |
| `src/dsar_orchestrator/adapters/scope_classify.py` | shells out to `dsar-scope-check` CLI | **Unchanged.** The recheck happens inside the toolkit's stage; the adapter's contract (`scope_classify_complete.jsonl` cascade anchor) is unaffected. Adapter only changes if/when its retirement contract triggers (toolkit ships `run_for_case(case_path)`). |
| `src/dsar_orchestrator/audit.py` | `PipelineAuditor`, `StageBanner` write `pipeline.jsonl` | **No change.** Fitness pre-flight gets its own `StageBanner("fitness_preflight")` so it appears in the audit. |
| `src/dsar_orchestrator/verify.py` | does not exist | **NEW** module hosting `verify_prompt_versions(case_dir)` and `verify_fitness_report(case_dir)`. `cli.py`'s `verify` subcommand dispatches here. |
| `tools/check_durant_doc.py`, `docs/durant-doc-lint.yaml` | do not exist | **NEW** per §4.7. Wired into orchestrator CI (`.github/workflows/...` or pre-commit). |

**Note on `dsar-conductor verify`:** the design body talks about a flag `--check prompt-versions`. Implementation should use argparse subparsers since the orchestrator has no `verify` subcommand today. Form: `dsar-conductor verify --check prompt-versions --case <id>` (or `--case-root` analogue). Strict mode: `--strict` upgrades older-version warnings to fatal.

### 10.3 Per-engagement scripts (not in either repo)

Per-engagement bypass scripts live in `<engagement>/audit/` per the per-engagement data-isolation rule. **This spec does NOT edit them**; it makes the new tooling available for them to consume. Migration is engagement-by-engagement:

1. Replace inline `DURANT_SYSTEM_PROMPT` copy with `subprocess.run(["dsar-prompt", "durant.system", "--strip-section", "placeholder-tokens"], ...)` (or `dsar-prompt-vendored.pyz` for non-installed deployments).
2. Add runtime sha256 verification of the captured body against the footer's `effective_sha256`.
3. Record `prompt_canonical_seal_sha256`, `prompt_applied_strips`, `prompt_effective_sha256`, `prompt_source` in audit rows.
4. Where the bypass script also performed recheck: drop the local implementation and use `dsar-recheck` (or its vendored equivalent) which now reads the same prompt asset.

Migration is documented in a separate engagement-script migration guide (out of scope for this spec; touched on in §4.7's `durant-test.md` §7 update).

### 10.4 Integration acceptance tests

Beyond unit tests per section, end-to-end integration tests should verify:

- **`test_e2e_durant_with_recheck`** (tests/test_durant_pipeline_e2e.py — NEW): run `ScopeCheckStage` on a fixture case where the primary gate misclassifies 5 of 30 WCO docs as biographical-tail-only; assert recheck reclassifies them, and `scope_verdicts.jsonl` shows `scope_verdict=present` for those refs. Synthesis summary records `recheck_promoted: 5`.
- **`test_e2e_fitness_preflight_aborts_run`** (tests/test_conductor_fitness_preflight.py — NEW orchestrator test): conductor with stale fitness report aborts before any stage runs; `pipeline.jsonl` records the abort.
- **`test_e2e_prompt_drift_caught_by_conductor_verify`** (orchestrator test): produce a case with audit rows for an old prompt seal; `dsar-conductor verify --check prompt-versions` exits with WARN; `--strict` exits 2.
- **`test_existing_test_gate_durant_unchanged`** (regression): the existing `tests/test_gate_durant.py` continues to pass without modification (proves we extended without breaking).

### 10.5 Phasing — refined for integration

Updated phased order accounting for the toolkit's actual layout:

1. **Phase 1a — Loader + assets (toolkit, parallelisable internally):**
   - `src/dsar_pipeline/gates/prompt_loader.py` (§4.1 Layers A+B).
   - `src/dsar_pipeline/gates/prompts/durant.system.md` (verbatim extract from existing `gate_durant.py:DURANT_SYSTEM_PROMPT`, with v1.0.0 + seal).
   - `bin/build-prompt-registry`, `_registry.json`, archive (§4.1 Layer D).
   - CI: `tests/test_prompt_assets.py`.
   - `pyproject.toml` adds `dsar-prompt` entry (§4.1 Layer C).
   - `GateDurant._classify` refactored to use loader. `test_durant_prompt_template.py` continues to pass (assertions are about prompt body content, not the source).

2. **Phase 1b — Truncation + role (toolkit, parallel to 1a):**
   - `src/dsar_pipeline/gates/text_truncation.py` (§4.3).
   - `src/dsar_pipeline/config/model_context.json`.
   - `data_subject.json` JSON schema gains optional `role` + `role_context` (§4.5).
   - `GateDurant._load_ref_text` swap to `truncate_with_token_check`. `GateDurant._build_user_prompt` conditionally emits role section.
   - `RoleRouter.has_token_counter_for` + `count_tokens` added.

3. **Phase 2 — Recheck stage (depends on 1a + 1b):**
   - `prompts/durant.recheck.system.md`.
   - `src/dsar_pipeline/gates/gate_durant_recheck.py` (§4.2).
   - `src/dsar_pipeline/recheck_stage.py` + `dsar-recheck` CLI.
   - `_stage_base.VALID_STAGE_LABELS` += `"durant_recheck"`.
   - `scope_check_stage.py` extended to invoke `RecheckStage` after primary durant when configured.

4. **Phase 3 — Synthesis (depends on Phase 2):**
   - `agent22_scope_check.synthesise_verdict` 5-arg form (§4.6).
   - Backwards-compat shim for `_synthesise_verdict` 2-arg form.
   - `scope_verdict.schema.json` evidence-block extension.
   - `tests/test_scope_decisions.py` extended for recheck branches.

5. **Phase 4 — Canary + conductor pre-flight (depends on Phases 1–3):**
   - `src/dsar_pipeline/fitness_canary.py` + `dsar-fitness-canary` CLI (§4.4).
   - `examples/canary_baseline/` corpus.
   - `dsar-orchestrator`: `CaseConfig` fields, `verify.py`, `_run_fitness_preflight` in pipeline.
   - `cli.py` subparser conversion + `verify` subcommand.

6. **Phase 5 — Doc + CI lint (continuous, per-PR for Phases 1–4):**
   - `tools/check_durant_doc.py`, `durant-doc-lint.yaml` added in Phase 1a.
   - `docs/durant-test.md` edits ship with the respective code PRs.

### 10.6 Out-of-scope follow-ups surfaced by the integration review

- **Migrate `biographical_refs.json` → `durant_verdicts.jsonl` as authoritative.** Today's `biographical_refs.json` aggregate is consumed by `gate_subject_preservation` and similar downstream gates per `gate_durant.py:7-15` docstring. A follow-up spec deprecates the aggregate (computed-view-only) once all consumers switch to the JSONL.
- **Migrate `config/prompts/scope_check.txt` and `scope_check_strict.txt` to the seal-managed loader.** Those are a separate role (`scope_check` vs Durant's `scope_check` role-as-gate). Same pattern, separate effort.
- **Toolkit's `dsar-scope-check` CLI ↔ orchestrator's `scope_classify` adapter retirement.** The adapter doc notes retirement when toolkit exposes `run_for_case(case_path)`. This spec doesn't trigger that; the recheck integration is internal to `scope_check_stage`.

---

*End of design v1.*
