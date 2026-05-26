# Durant Pipeline Hardening — Phase 5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Scope of this plan: Phase 5 only.** Phase 5 covers spec §4.4 (model-fitness canary) end-to-end + spec §10.2 (orchestrator-side config / verify / pre-flight / CLI subparsers). It depends on Phase 1 (prompt loader, `compute_seal`, `durant.system` asset signed), Phase 2 (truncation + role field), Phase 3 (recheck stage `GateDurantRecheck` + recheck prompt + Wilson helpers in `gates/_wilson.py`), and Phase 4 (Agent22 synthesis — not strictly required for canary, but the spec assumes `recheck_decision.json` semantics from Phase 3).
>
> Subsequent phase plans:
>
> - Phase 6 plan: durant-test.md updates + CI lint (§4.7)
>
> Each phase plan stands on its own (working software at the end of each phase).

**Goal:** Land the upfront model-fitness gate: a `dsar-fitness-canary` CLI that runs primary GateDurant (+ optional GateDurantRecheck) against a per-deployment canary corpus, writes a structured PASS/FAIL report with Wilson-bounded metrics, and a conductor pre-flight hook that refuses to start a real case run unless a fresh+passing fitness report exists for the exact `(deployment_id, model_alias, primary_seal, recheck_seal, live_corpus_sha, inference_params_sha)` tuple. Adds `dsar-conductor verify --check prompt-versions` for post-hoc audit-row hash drift detection. Converts `cli.py` from flat argparse to subparsers while preserving the existing `dsar-conductor --case X` invocation as the default `run` subcommand.

**Architecture:**

| File | Repo | Action |
|---|---|---|
| `src/dsar_pipeline/fitness_canary.py` | toolkit | CREATE — Metrics dataclass, FitnessFail, compute_metrics, evaluate, find_matching_report helper, main() CLI |
| `src/dsar_pipeline/canary_corpus.py` | toolkit | CREATE — `compute_corpus_sha256()` + corpus-loader helpers |
| `examples/canary_baseline/canary_corpus.json` | toolkit | CREATE — 6-pattern baseline |
| `examples/canary_baseline/truth.json` | toolkit | CREATE — truth labels |
| `examples/canary_baseline/refs/*.txt` | toolkit | CREATE — 6 reference documents |
| `tests/test_fitness_canary.py` | toolkit | CREATE — Wilson math worked examples, corpus hash, evaluate, CLI smoke |
| `tests/test_baseline_corpus_seal.py` | toolkit | CREATE — pinned corpus_sha256 |
| `pyproject.toml` `[project.scripts]` | toolkit | MODIFY — add `dsar-fitness-canary` entry |
| `src/dsar_orchestrator/config.py` | orchestrator | MODIFY — add 4 fitness_check fields to CaseConfig |
| `src/dsar_orchestrator/verify.py` | orchestrator | CREATE — `verify_prompt_versions()`, `verify_fitness_report()` |
| `src/dsar_orchestrator/pipeline.py` | orchestrator | MODIFY — `_run_fitness_preflight()` + hook into `run()` |
| `src/dsar_orchestrator/cli.py` | orchestrator | REWRITE — subparsers (`run` default, `verify` new); add `--auto-fitness`, `--force-skip-fitness` |
| `tests/test_verify.py` | orchestrator | CREATE — both `verify_*` functions |
| `tests/test_conductor_fitness_preflight.py` | orchestrator | CREATE — preflight halts on stale/missing; --auto-fitness inlines; --force-skip-fitness records audit |
| `tests/test_cli_subparsers.py` | orchestrator | CREATE — default `run` subcommand back-compat |

**Tech Stack:** Python 3.10+ (orchestrator), 3.11+ (toolkit); `pytest`. Wilson math via `gates/_wilson.py` (added in Phase 3 — provides `wilson_lower(k, n, z=1.645)` for 90% CI and `wilson_upper(k, n, z=1.645)`; both return `None` when `n == 0`). No new third-party deps.

---

## File structure

### dsar-toolkit (creates 5 files; modifies 1)

```
src/dsar_pipeline/
├── fitness_canary.py                        # CREATE — CLI + metrics + evaluate
├── canary_corpus.py                         # CREATE — compute_corpus_sha256 + load helpers
└── gates/
    └── _wilson.py                           # (assumed from Phase 3; this plan READS but does NOT modify)
examples/
└── canary_baseline/                         # CREATE DIR
    ├── canary_corpus.json
    ├── truth.json
    └── refs/
        ├── ref_001_clear_bio.txt
        ├── ref_002_clear_wco.txt
        ├── ref_003_direct_addressee.txt
        ├── ref_004_ambiguous_mixed.txt
        ├── ref_005_long_thread_tail.txt
        └── ref_006_signature_only.txt
tests/
├── test_fitness_canary.py                   # CREATE
└── test_baseline_corpus_seal.py             # CREATE
pyproject.toml                               # MODIFY: add dsar-fitness-canary script
```

### dsar-orchestrator (creates 4 files; modifies 3)

```
src/dsar_orchestrator/
├── config.py                                # MODIFY: 4 fitness_check fields
├── verify.py                                # CREATE: verify_prompt_versions, verify_fitness_report
├── pipeline.py                              # MODIFY: _run_fitness_preflight + hook in run()
└── cli.py                                   # REWRITE: subparsers (run|verify); --auto-fitness, --force-skip-fitness
tests/
├── test_verify.py                           # CREATE
├── test_conductor_fitness_preflight.py      # CREATE
└── test_cli_subparsers.py                   # CREATE
```

---

## Phase 5a — Toolkit canary corpus + hash

### Task 47: Author `examples/canary_baseline/` (6 reference patterns + truth.json + canary_corpus.json)

**Files (toolkit):**
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/canary_corpus.json`
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/truth.json`
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_001_clear_bio.txt`
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_002_clear_wco.txt`
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_003_direct_addressee.txt`
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_004_ambiguous_mixed.txt`
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_005_long_thread_tail.txt`
- Create: `~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_006_signature_only.txt`

This baseline is referenced by spec §4.4 (A). It ships as a default canary set inside the toolkit so operators can copy it into `~/.dsar/canary_sets/<deployment_id>/` and extend it (the corpus_sha256 will change once they add refs — that's expected; only the SHIPPED baseline's seal is pinned in CI).

- [ ] **Step 1: Create the directory layout**

```bash
mkdir -p ~/projects/dsar-toolkit/examples/canary_baseline/refs
```

- [ ] **Step 2: Author `refs/ref_001_clear_bio.txt` (truth = biographical)**

The subject ([PERSON_5]) is the explicit focus of the document. A clear, unambiguous biographical case used to test that the model does not over-classify as `work_context_only` (false negative — the dangerous mode).

```bash
cat > ~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_001_clear_bio.txt <<'EOF'
From: [PERSON_2] <[EMAIL_2]>
To: [PERSON_5] <[EMAIL_5]>
Subject: Your annual performance review — meeting on [DATE_0]

[PERSON_5],

Following our discussion last week, I am writing to confirm the
performance review meeting on [DATE_0] at [TIME_0] in
[LOCATION_0]. Please bring the self-assessment you prepared.

The review will cover your work on the [PROJECT_0] migration, the
[PROJECT_1] launch (where your contribution was central), and the
two managers who escalated concerns about your communication style
during the [DATE_1] all-hands.

Outcomes from the review will be recorded in your personnel file
and will inform the upcoming compensation cycle.

[PERSON_2]
HR Director
EOF
```

- [ ] **Step 3: Author `refs/ref_002_clear_wco.txt` (truth = work_context_only)**

The subject ([PERSON_5]) is a CC recipient on an email about a third party's project. Subject is not the focus; the topic is unrelated to them.

```bash
cat > ~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_002_clear_wco.txt <<'EOF'
From: [PERSON_0] <[EMAIL_0]>
To: [PERSON_1] <[EMAIL_1]>
Cc: [PERSON_5] <[EMAIL_5]>, [PERSON_6] <[EMAIL_6]>
Subject: [PROJECT_2] — vendor proposal review

[PERSON_1],

Attaching the three vendor proposals received for [PROJECT_2].
[PERSON_3] from procurement has scored each on the standard
matrix; [VENDOR_0] is leading on price, [VENDOR_1] on technical
fit, [VENDOR_2] is non-viable (no SOC2).

Decision needs to be by [DATE_3] to keep the rollout on schedule.

CC'd for awareness only — no action needed from the wider list.

[PERSON_0]
EOF
```

- [ ] **Step 4: Author `refs/ref_003_direct_addressee.txt` (truth = biographical)**

Direct-addressee carve-out: subject is in the To: line on an email *about* the subject's own employment / contract / assignment. The default classification is biographical, not work_context_only. This is the most-mis-classified pattern in Durant test history; explicit in the prompt body.

```bash
cat > ~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_003_direct_addressee.txt <<'EOF'
From: [PERSON_4] <[EMAIL_4]>
To: [PERSON_5] <[EMAIL_5]>
Subject: Your assignment to [PROJECT_3] — secondment terms

[PERSON_5],

Confirming the terms of your secondment to [PROJECT_3] starting
[DATE_5]. The headline terms:

  - Duration: 6 months, with a review at month 4
  - Reporting line: [PERSON_7], dotted-line to your current
    manager [PERSON_2]
  - Salary protection: maintained at current grade
  - Travel: weekly Mon/Thu to [LOCATION_1]

Please confirm acceptance by [DATE_6] so HR can issue the
secondment letter.

[PERSON_4]
EOF
```

- [ ] **Step 5: Author `refs/ref_004_ambiguous_mixed.txt` (truth = ambiguous)**

Mixed signals: the subject is mentioned as a participant in a meeting *about another person's* disciplinary case. Cannot decide cleanly without more context. Truth label = ambiguous; canary checks that the model uses the ambiguous verdict (not silently defaults one way or the other).

```bash
cat > ~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_004_ambiguous_mixed.txt <<'EOF'
From: [PERSON_2] <[EMAIL_2]>
To: [PERSON_8] <[EMAIL_8]>
Cc: [PERSON_5] <[EMAIL_5]>
Subject: Re: [PERSON_9] — investigation update

[PERSON_8],

Investigation interview with [PERSON_9] completed on [DATE_7].
[PERSON_5] attended as the union representative; their notes are
attached as [ATTACHMENT_0].

Key points from [PERSON_5]'s observations:
  - Procedural concern around the late notification (raised on
    record)
  - No objection to the substantive line of questioning

Next step: drafting the outcome letter. [PERSON_5] has asked to
review the procedural section before issue.

[PERSON_2]
EOF
```

- [ ] **Step 6: Author `refs/ref_005_long_thread_tail.txt` (truth = biographical)**

Long email thread (>8k chars) where the biographical signal appears in the *tail*, not the head. Tests the §4.3 head_tail truncation. Without smart truncation, blind tail-cut would drop the biographical signal and produce a false negative.

```bash
python3 - <<'EOF' > ~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_005_long_thread_tail.txt
# Build a long thread: head/middle are about an unrelated topic,
# tail contains the biographical signal about [PERSON_5].

head = """From: [PERSON_0] <[EMAIL_0]>
To: distribution-list@[ORGANIZATION_0]
Subject: [PROJECT_4] weekly status — week of [DATE_8]

Team,

Status update on [PROJECT_4]:

"""

middle_lines = []
for i in range(200):
    middle_lines.append(
        f"  - Workstream {i % 12}: progressed {i % 5}/5 items "
        f"this week; [PERSON_{i % 4}] driving."
    )
middle = "\n".join(middle_lines)

tail = """

----- Forwarded message -----
From: [PERSON_2] <[EMAIL_2]>
To: [PERSON_5] <[EMAIL_5]>
Subject: Your secondment paperwork

[PERSON_5],

The secondment letter is ready for your signature. Please collect
from HR before [DATE_9]. Your start date on [PROJECT_4] remains
[DATE_10] — see the thread above for the workstream you'll join.

[PERSON_2]
HR Director

----- End forwarded message -----
"""

print(head + middle + tail, end="")
EOF
```

- [ ] **Step 7: Author `refs/ref_006_signature_only.txt` (truth = work_context_only)**

Subject's only appearance is in a signature block at the bottom of a broadcast (e.g., a generic newsletter or all-hands announcement). Tests that the model does not over-promote signature-only mentions to biographical. False positive here = noise; safe but operator-burden.

```bash
cat > ~/projects/dsar-toolkit/examples/canary_baseline/refs/ref_006_signature_only.txt <<'EOF'
From: [PERSON_5] <[EMAIL_5]>
To: all-staff@[ORGANIZATION_0]
Subject: [ORGANIZATION_0] Q3 town hall — invite + agenda

All,

Quick reminder that the Q3 town hall is [DATE_11] at [TIME_1].

Agenda:
  1. CEO opening
  2. Financial summary (CFO)
  3. Product roadmap (CPO)
  4. Q&A

Dial-in details and slides will be circulated 24h before. As
always, anonymous questions can be submitted via the internal
form linked from the intranet.

See you there.

—
[PERSON_5]
Internal Communications
[ORGANIZATION_0]
EOF
```

- [ ] **Step 8: Author `truth.json`**

```bash
cat > ~/projects/dsar-toolkit/examples/canary_baseline/truth.json <<'EOF'
{
  "ref_001_clear_bio": "biographical",
  "ref_002_clear_wco": "work_context_only",
  "ref_003_direct_addressee": "biographical",
  "ref_004_ambiguous_mixed": "ambiguous",
  "ref_005_long_thread_tail": "biographical",
  "ref_006_signature_only": "work_context_only"
}
EOF
```

- [ ] **Step 9: Author `canary_corpus.json`**

```bash
cat > ~/projects/dsar-toolkit/examples/canary_baseline/canary_corpus.json <<'EOF'
{
  "version": 1,
  "baseline_version": "1.0.0",
  "description": "Durant-classic 6-pattern baseline corpus shipped with dsar-toolkit examples/canary_baseline. Patterns: clear-bio, clear-WCO, direct-addressee, ambiguous-mixed, long-thread-tail, signature-only.",
  "refs": [
    "ref_001_clear_bio",
    "ref_002_clear_wco",
    "ref_003_direct_addressee",
    "ref_004_ambiguous_mixed",
    "ref_005_long_thread_tail",
    "ref_006_signature_only"
  ]
}
EOF
```

- [ ] **Step 10: Verify LF-only + no trailing CR**

```bash
cd ~/projects/dsar-toolkit
for f in examples/canary_baseline/*.json examples/canary_baseline/refs/*.txt; do
  if grep -q $'\r' "$f"; then
    echo "FAIL: CR found in $f"
    exit 1
  fi
done
echo "OK — all files LF-only"
```

- [ ] **Step 11: Commit**

```bash
cd ~/projects/dsar-toolkit
git add examples/canary_baseline/
git commit -m "feat(canary): ship 6-pattern Durant baseline corpus in examples/"
```

---

### Task 48: Implement `compute_corpus_sha256()` in `canary_corpus.py`

**Files (toolkit):**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/canary_corpus.py`
- Create: `~/projects/dsar-toolkit/tests/test_fitness_canary.py` (start of file)

Per spec §4.4 (G): requires both `canary_corpus.json` AND `truth.json` exist; validates truth.json is a non-empty JSON object; deduplicated file set (explicit `refs` list ∪ `refs/*.txt` glob, set-deduped); `.json` files canonicalized via `json.dumps(json.loads(content), sort_keys=True, separators=(",", ":"))`; LF normalisation on text files; `path.as_posix()` for path keys; paths sorted lexicographically.

- [ ] **Step 1: Write failing test**

Create `tests/test_fitness_canary.py`:

```python
"""Tests for §4.4 model-fitness canary (spec
docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md
§4.4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_pipeline.canary_corpus import compute_corpus_sha256


def _seed_corpus(tmp_path: Path,
                  refs: list[str] | None = None,
                  truth: dict | None = None) -> Path:
    refs = refs if refs is not None else ["ref_a", "ref_b"]
    truth = truth if truth is not None else {
        "ref_a": "biographical", "ref_b": "work_context_only",
    }
    (tmp_path / "refs").mkdir()
    (tmp_path / "canary_corpus.json").write_text(
        json.dumps({"version": 1, "refs": refs}), encoding="utf-8")
    (tmp_path / "truth.json").write_text(
        json.dumps(truth), encoding="utf-8")
    for ref in refs:
        (tmp_path / "refs" / f"{ref}.txt").write_text(
            f"Body of {ref}.\n", encoding="utf-8")
    return tmp_path


def test_compute_corpus_sha256_is_deterministic(tmp_path):
    """Same corpus content → same hash; different content → different hash."""
    a = _seed_corpus(tmp_path / "a")
    b = _seed_corpus(tmp_path / "b")
    h_a = compute_corpus_sha256(a)
    h_b = compute_corpus_sha256(b)
    assert h_a == h_b
    assert len(h_a) == 64


def test_compute_corpus_sha256_json_canonicalised(tmp_path):
    """Cosmetic JSON formatting (indent/key order) does not change the hash."""
    base = _seed_corpus(tmp_path / "base")
    h_base = compute_corpus_sha256(base)
    # Rewrite truth.json with indent + reordered keys.
    (base / "truth.json").write_text(
        json.dumps({
            "ref_b": "work_context_only",
            "ref_a": "biographical",
        }, indent=4), encoding="utf-8")
    h_after = compute_corpus_sha256(base)
    assert h_base == h_after


def test_compute_corpus_sha256_changes_with_ref_body(tmp_path):
    corpus = _seed_corpus(tmp_path / "x")
    h0 = compute_corpus_sha256(corpus)
    (corpus / "refs" / "ref_a.txt").write_text(
        "DIFFERENT body.\n", encoding="utf-8")
    h1 = compute_corpus_sha256(corpus)
    assert h0 != h1


def test_compute_corpus_sha256_lf_normalisation(tmp_path):
    """CRLF in ref text normalises to LF before hashing."""
    corpus = _seed_corpus(tmp_path / "crlf")
    h_lf = compute_corpus_sha256(corpus)
    (corpus / "refs" / "ref_a.txt").write_bytes(b"Body of ref_a.\r\n")
    h_crlf = compute_corpus_sha256(corpus)
    assert h_lf == h_crlf


def test_compute_corpus_sha256_missing_canary_corpus_raises(tmp_path):
    """No canary_corpus.json → ValueError."""
    (tmp_path / "truth.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="canary_corpus.json"):
        compute_corpus_sha256(tmp_path)


def test_compute_corpus_sha256_missing_truth_raises(tmp_path):
    (tmp_path / "canary_corpus.json").write_text(
        '{"version":1,"refs":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="truth.json"):
        compute_corpus_sha256(tmp_path)


def test_compute_corpus_sha256_empty_truth_raises(tmp_path):
    (tmp_path / "canary_corpus.json").write_text(
        '{"version":1,"refs":[]}', encoding="utf-8")
    (tmp_path / "truth.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="truth.json.*non-empty"):
        compute_corpus_sha256(tmp_path)


def test_compute_corpus_sha256_truth_not_object_raises(tmp_path):
    (tmp_path / "canary_corpus.json").write_text(
        '{"version":1,"refs":[]}', encoding="utf-8")
    (tmp_path / "truth.json").write_text('["not", "object"]', encoding="utf-8")
    with pytest.raises(ValueError, match="truth.json.*object"):
        compute_corpus_sha256(tmp_path)


def test_compute_corpus_sha256_includes_refs_glob_dedup(tmp_path):
    """Files in refs/*.txt outside the explicit list are included once."""
    corpus = _seed_corpus(tmp_path / "extra", refs=["ref_a"],
                           truth={"ref_a": "biographical"})
    h_initial = compute_corpus_sha256(corpus)
    # Add a stray ref not in canary_corpus.json refs[] — hash should change
    # (covered by the glob union) but only once (dedup).
    (corpus / "refs" / "ref_extra.txt").write_text("stray\n",
                                                     encoding="utf-8")
    h_after = compute_corpus_sha256(corpus)
    assert h_initial != h_after
```

- [ ] **Step 2: Run; verify ImportError failures**

```bash
cd ~/projects/dsar-toolkit
uv run pytest tests/test_fitness_canary.py -v
```

Expected: ImportError for `dsar_pipeline.canary_corpus`.

- [ ] **Step 3: Implement `compute_corpus_sha256()`**

Create `src/dsar_pipeline/canary_corpus.py`:

```python
"""Canary-corpus utilities (§4.4 G of durant-pipeline-hardening design).

`compute_corpus_sha256(canary_set_path)` produces a stable 64-char hex
fingerprint of (canary_corpus.json + truth.json + refs/*.txt). Cosmetic
JSON edits and CRLF line-endings do NOT change the hash; substantive
content edits DO.

Hash inputs (sorted by POSIX path):
  - canary_corpus.json (canonicalised via json.dumps(sort_keys=True,
    separators=(",", ":")))
  - truth.json (canonicalised the same way)
  - every refs/*.txt file in the set ∪ explicit refs[] list (dedup
    via set), LF-normalised
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _canonicalise_json_bytes(raw: bytes) -> bytes:
    """Parse + re-emit with sort_keys, compact separators — strips
    incidental whitespace/key-order differences."""
    obj = json.loads(raw.decode("utf-8"))
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")


def _normalise_lf(raw: bytes) -> bytes:
    """CRLF/CR → LF; preserves the rest of the file byte-for-byte."""
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def compute_corpus_sha256(canary_set_path: Path) -> str:
    """Compute the canary-corpus seal. Spec §4.4 (G).

    Raises ValueError if:
      - canary_corpus.json missing
      - truth.json missing
      - truth.json is not a JSON object
      - truth.json is empty
    """
    canary_set_path = Path(canary_set_path)
    corpus_path = canary_set_path / "canary_corpus.json"
    truth_path = canary_set_path / "truth.json"
    if not corpus_path.is_file():
        raise ValueError(
            f"canary_corpus.json not found at {corpus_path}")
    if not truth_path.is_file():
        raise ValueError(
            f"truth.json not found at {truth_path}")

    # Validate truth.json shape early.
    try:
        truth_obj = json.loads(truth_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"truth.json at {truth_path} is not valid JSON: {e}") from e
    if not isinstance(truth_obj, dict):
        raise ValueError(
            f"truth.json at {truth_path} must be a JSON object")
    if not truth_obj:
        raise ValueError(
            f"truth.json at {truth_path} must be non-empty")

    # Build the deduplicated file set: explicit refs[] ∪ refs/*.txt glob.
    file_paths: set[Path] = {corpus_path, truth_path}
    try:
        corpus_obj = json.loads(corpus_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"canary_corpus.json at {corpus_path} is not valid JSON: {e}"
        ) from e
    refs_dir = canary_set_path / "refs"
    explicit_refs = corpus_obj.get("refs", []) or []
    for ref_id in explicit_refs:
        candidate = refs_dir / f"{ref_id}.txt"
        if candidate.is_file():
            file_paths.add(candidate)
    if refs_dir.is_dir():
        for p in refs_dir.glob("*.txt"):
            if p.is_file():
                file_paths.add(p)

    # Hash inputs in sorted POSIX-path order.
    hasher = hashlib.sha256()
    for path in sorted(file_paths, key=lambda p: p.relative_to(canary_set_path).as_posix()):
        rel = path.relative_to(canary_set_path).as_posix()
        raw = path.read_bytes()
        if path.suffix == ".json":
            content = _canonicalise_json_bytes(raw)
        else:
            content = _normalise_lf(raw)
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
        hasher.update(b"\0")
    return hasher.hexdigest()
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_fitness_canary.py -v
```

Expected: all 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/canary_corpus.py tests/test_fitness_canary.py
git commit -m "feat(canary): compute_corpus_sha256 (LF + JSON canonicalisation)"
```

---

### Task 49: Pin the baseline corpus seal in CI (`test_baseline_corpus_seal`)

**Files (toolkit):**
- Create: `~/projects/dsar-toolkit/tests/test_baseline_corpus_seal.py`

The shipped `examples/canary_baseline/` is a contract: edits require a version bump (`baseline_version` field in `canary_corpus.json`) and a deliberate seal update. Pinning the SHA in CI catches drift.

- [ ] **Step 1: Compute the current baseline seal**

```bash
cd ~/projects/dsar-toolkit
uv run python -c "
from pathlib import Path
from dsar_pipeline.canary_corpus import compute_corpus_sha256
print(compute_corpus_sha256(Path('examples/canary_baseline')))
"
```

Save the printed hex — call it `<BASELINE_SHA>`. Use it in the test below.

- [ ] **Step 2: Write the test (using the literal `<BASELINE_SHA>` you computed)**

Create `tests/test_baseline_corpus_seal.py`:

```python
"""CI seal pin for the shipped examples/canary_baseline corpus.

If this test fails, you either:
  (a) edited a baseline ref/truth/canary_corpus.json — you need to
      bump `baseline_version` and update PINNED_BASELINE_SHA here, OR
  (b) the corpus changed unintentionally — revert.
"""
from __future__ import annotations

from pathlib import Path

from dsar_pipeline.canary_corpus import compute_corpus_sha256


# Replace this with the hex printed by `python -c "from
# dsar_pipeline.canary_corpus import compute_corpus_sha256; ..."`
# during Task 49 Step 1.
PINNED_BASELINE_SHA = "<BASELINE_SHA>"   # replace at implementation time


def test_baseline_corpus_seal():
    here = Path(__file__).resolve().parent.parent
    baseline = here / "examples" / "canary_baseline"
    assert baseline.is_dir(), f"baseline missing at {baseline}"
    actual = compute_corpus_sha256(baseline)
    assert actual == PINNED_BASELINE_SHA, (
        f"baseline corpus seal drift:\n"
        f"  expected: {PINNED_BASELINE_SHA}\n"
        f"  actual:   {actual}\n"
        f"If the change was intentional, bump canary_corpus.json's "
        f"`baseline_version` AND update PINNED_BASELINE_SHA in "
        f"tests/test_baseline_corpus_seal.py."
    )
```

- [ ] **Step 3: Run; verify PASS (with the literal SHA filled in)**

```bash
uv run pytest tests/test_baseline_corpus_seal.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_baseline_corpus_seal.py
git commit -m "test(canary): pin baseline corpus_sha256 in CI"
```

---

## Phase 5b — Toolkit Metrics + evaluate

### Task 50: Implement `Metrics` dataclass + `compute_metrics(per_ref, min_success_rate)`

**Files (toolkit):**
- Create: `~/projects/dsar-toolkit/src/dsar_pipeline/fitness_canary.py` (initial; metrics-only)
- Modify: `~/projects/dsar-toolkit/tests/test_fitness_canary.py` (append)

Per spec §4.4 (D + E + H):
- `per_ref` shape: `[{"ref": "...", "truth": "biographical|work_context_only|ambiguous", "gate": "biographical|work_context_only|ambiguous|None", "error_state": dict|None}, ...]`.
- **Class counts from FULL `per_ref`** (errored refs included) — counts how many truth-labelled examples the corpus actually has of each class.
- **Rate denominators are SUCCESSFUL only** — agreement/FN/FP exclude errored refs (errors are infrastructure, not model fitness).
- `ambiguous_rate_on_definite_truth` numerator = gate=ambiguous on `truth ∈ {biographical, work_context_only}`; denominator = `succ_definite` (successful refs with definite truth).
- All rate-like fields ∈ [0.0, 1.0] when their denominator > 0; otherwise `None`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_fitness_canary.py`:

```python
from dsar_pipeline.fitness_canary import Metrics, compute_metrics


def test_compute_metrics_balanced_30_perfect():
    """30 refs, 12 bio + 12 WCO + 6 amb, all correct → wilson_upper(FN)≈0.18."""
    per_ref = []
    for i in range(12):
        per_ref.append({"ref": f"b{i}", "truth": "biographical",
                         "gate": "biographical", "error_state": None})
    for i in range(12):
        per_ref.append({"ref": f"w{i}", "truth": "work_context_only",
                         "gate": "work_context_only", "error_state": None})
    for i in range(6):
        per_ref.append({"ref": f"a{i}", "truth": "ambiguous",
                         "gate": "ambiguous", "error_state": None})
    m = compute_metrics(per_ref, min_success_rate=0.85)
    assert m.corpus_size == 30
    assert m.n_biographical_truth == 12
    assert m.n_work_context_only_truth == 12
    assert m.n_ambiguous_truth == 6
    assert m.n_biographical_successful == 12
    assert m.n_work_context_only_successful == 12
    assert m.success_rate == 1.0
    assert m.agreement == 1.0
    assert m.fn_rate == 0.0
    assert m.fp_rate == 0.0
    assert m.ambiguous_rate_on_definite_truth == 0.0
    # Wilson 90% upper bound for 0/24 ≈ 0.118; well below 0.20.
    assert m.fn_rate_wilson_upper is not None
    assert m.fn_rate_wilson_upper < 0.20


def test_compute_metrics_30_with_1_fn():
    """30 refs, balanced; one biographical mis-classified as WCO →
    wilson_upper(FN) ≈ 0.27 → would FAIL the 0.20 threshold."""
    per_ref = []
    # 11 correct bio + 1 FN
    for i in range(11):
        per_ref.append({"ref": f"b{i}", "truth": "biographical",
                         "gate": "biographical", "error_state": None})
    per_ref.append({"ref": "b_fn", "truth": "biographical",
                     "gate": "work_context_only", "error_state": None})
    for i in range(12):
        per_ref.append({"ref": f"w{i}", "truth": "work_context_only",
                         "gate": "work_context_only", "error_state": None})
    for i in range(6):
        per_ref.append({"ref": f"a{i}", "truth": "ambiguous",
                         "gate": "ambiguous", "error_state": None})
    m = compute_metrics(per_ref, min_success_rate=0.85)
    # 1 FN / 12 definite-bio = 0.0833 raw; Wilson upper 90% ≈ 0.27.
    assert m.fn_rate is not None
    assert abs(m.fn_rate - (1 / 12)) < 1e-9
    assert m.fn_rate_wilson_upper > 0.20
    assert m.fn_rate_wilson_upper < 0.35


def test_compute_metrics_50_with_1_fn():
    """50 refs (20 bio + 20 WCO + 10 amb), 1 FN → Wilson upper ≈ 0.18 → PASSES 0.20."""
    per_ref = []
    for i in range(19):
        per_ref.append({"ref": f"b{i}", "truth": "biographical",
                         "gate": "biographical", "error_state": None})
    per_ref.append({"ref": "b_fn", "truth": "biographical",
                     "gate": "work_context_only", "error_state": None})
    for i in range(20):
        per_ref.append({"ref": f"w{i}", "truth": "work_context_only",
                         "gate": "work_context_only", "error_state": None})
    for i in range(10):
        per_ref.append({"ref": f"a{i}", "truth": "ambiguous",
                         "gate": "ambiguous", "error_state": None})
    m = compute_metrics(per_ref, min_success_rate=0.85)
    assert m.fn_rate is not None
    assert abs(m.fn_rate - (1 / 20)) < 1e-9
    assert m.fn_rate_wilson_upper is not None
    assert m.fn_rate_wilson_upper < 0.20


def test_compute_metrics_errored_refs_excluded_from_rates_counted_in_class():
    """Errored refs count in n_*_truth (class size) but NOT in rate denominators."""
    per_ref = []
    # 2 errored biographical truth-labelled refs — count toward
    # n_biographical_truth (full corpus) but excluded from FN denominator.
    for i in range(2):
        per_ref.append({"ref": f"e{i}", "truth": "biographical",
                         "gate": None,
                         "error_state": {"code": "timeout", "message": "..."}})
    # 10 successful biographical, all correct
    for i in range(10):
        per_ref.append({"ref": f"b{i}", "truth": "biographical",
                         "gate": "biographical", "error_state": None})
    # 12 successful work_context_only, all correct
    for i in range(12):
        per_ref.append({"ref": f"w{i}", "truth": "work_context_only",
                         "gate": "work_context_only", "error_state": None})
    m = compute_metrics(per_ref, min_success_rate=0.85)
    assert m.n_biographical_truth == 12        # 10 successful + 2 errored
    assert m.n_biographical_successful == 10
    assert m.fn_rate == 0.0                      # 0/10
    assert m.success_rate == 22 / 24


def test_compute_metrics_ambiguous_rate_uses_definite_truth_denominator():
    """ambiguous_rate denominator = succ_definite (NOT succ_total)."""
    per_ref = []
    # 10 bio with gate=ambiguous (each contributes to numerator)
    for i in range(10):
        per_ref.append({"ref": f"b{i}", "truth": "biographical",
                         "gate": "ambiguous", "error_state": None})
    # 10 WCO with gate=ambiguous (also numerator)
    for i in range(10):
        per_ref.append({"ref": f"w{i}", "truth": "work_context_only",
                         "gate": "ambiguous", "error_state": None})
    # 10 ambiguous truth, gate=ambiguous (NOT counted in numerator —
    # this is correct behaviour for ambiguous truth)
    for i in range(10):
        per_ref.append({"ref": f"a{i}", "truth": "ambiguous",
                         "gate": "ambiguous", "error_state": None})
    m = compute_metrics(per_ref, min_success_rate=0.85)
    # 20 numerator / 20 denominator (definite-truth successful only) = 1.0
    assert m.ambiguous_rate_on_definite_truth == 1.0


def test_compute_metrics_zero_denominator_returns_none():
    """Class with 0 successful refs → that rate is None, not divide-by-zero."""
    per_ref = [
        # All bio errored
        {"ref": "b0", "truth": "biographical", "gate": None,
         "error_state": {"code": "timeout", "message": "..."}},
        # WCO + amb fine
        {"ref": "w0", "truth": "work_context_only",
         "gate": "work_context_only", "error_state": None},
    ]
    m = compute_metrics(per_ref, min_success_rate=0.85)
    assert m.fn_rate is None                  # 0 successful bio
    assert m.fn_rate_wilson_upper is None
```

- [ ] **Step 2: Run; verify ImportError**

```bash
uv run pytest tests/test_fitness_canary.py -v -k "metrics"
```

Expected: ImportError.

- [ ] **Step 3: Implement `Metrics` + `compute_metrics()`**

Create `src/dsar_pipeline/fitness_canary.py`:

```python
"""Model-fitness canary (§4.4 of durant-pipeline-hardening design).

`dsar-fitness-canary --deployment-id <id> [--corpus-path <path>]`:
runs primary GateDurant + (if recheck configured) GateDurantRecheck
against an operator-curated canary corpus and writes a structured
report to ~/.dsar/fitness_reports/<deployment_id>/<timestamp>.json.

Report → conductor pre-flight (orchestrator-side) decides whether
to allow a real case run. See spec §4.4 (F).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from dsar_pipeline.gates._wilson import wilson_lower, wilson_upper


# Truth / gate labels — keep in sync with GateDurant output.
_BIOGRAPHICAL = "biographical"
_WORK_CONTEXT_ONLY = "work_context_only"
_AMBIGUOUS = "ambiguous"
_DEFINITE = {_BIOGRAPHICAL, _WORK_CONTEXT_ONLY}


@dataclass(frozen=True)
class Metrics:
    """Structured metrics — per spec §4.4 (H)."""
    corpus_size: int
    # Class counts from FULL per_ref (includes errored).
    n_biographical_truth: int
    n_work_context_only_truth: int
    n_ambiguous_truth: int
    # Class counts among SUCCESSFUL (rate denominators).
    n_biographical_successful: int
    n_work_context_only_successful: int
    n_ambiguous_successful: int
    n_errored: int
    # Rates — None when denominator is 0.
    success_rate: Optional[float]
    agreement: Optional[float]
    agreement_wilson_lower: Optional[float]
    fn_rate: Optional[float]
    fn_rate_wilson_upper: Optional[float]
    fp_rate: Optional[float]
    fp_rate_wilson_upper: Optional[float]
    ambiguous_rate_on_definite_truth: Optional[float]


def _safe_rate(num: int, denom: int) -> Optional[float]:
    return None if denom == 0 else num / denom


def compute_metrics(per_ref: list[dict],
                     min_success_rate: float) -> Metrics:
    """Compute structured metrics from per-ref results. Spec §4.4 (D + E).

    `per_ref` row shape:
        {"ref": str, "truth": str, "gate": str|None, "error_state": dict|None}

    `min_success_rate` is accepted but NOT used to alter the metrics —
    it's passed-through so callers don't need it; the evaluate() step
    consumes it from criteria.
    """
    n_total = len(per_ref)
    # FULL-corpus class counts (errored refs included — exposes corpus
    # composition gaps, separate fail-code from model misclassification).
    n_bio_truth = sum(1 for r in per_ref if r["truth"] == _BIOGRAPHICAL)
    n_wco_truth = sum(1 for r in per_ref if r["truth"] == _WORK_CONTEXT_ONLY)
    n_amb_truth = sum(1 for r in per_ref if r["truth"] == _AMBIGUOUS)

    successful = [r for r in per_ref if r.get("error_state") is None]
    n_errored = n_total - len(successful)

    # Successful-class counts (rate denominators).
    n_bio_succ = sum(1 for r in successful if r["truth"] == _BIOGRAPHICAL)
    n_wco_succ = sum(1 for r in successful if r["truth"] == _WORK_CONTEXT_ONLY)
    n_amb_succ = sum(1 for r in successful if r["truth"] == _AMBIGUOUS)
    n_definite_succ = n_bio_succ + n_wco_succ

    # Agreement: gate == truth on ALL successful refs (any truth label).
    n_agree = sum(1 for r in successful if r["gate"] == r["truth"])
    agreement = _safe_rate(n_agree, len(successful))

    # FN: biographical truth, gate=work_context_only (dangerous: under-disclosure).
    fn_count = sum(
        1 for r in successful
        if r["truth"] == _BIOGRAPHICAL and r["gate"] == _WORK_CONTEXT_ONLY
    )
    fn_rate = _safe_rate(fn_count, n_bio_succ)

    # FP: work_context_only truth, gate=biographical (over-disclosure; noise).
    fp_count = sum(
        1 for r in successful
        if r["truth"] == _WORK_CONTEXT_ONLY and r["gate"] == _BIOGRAPHICAL
    )
    fp_rate = _safe_rate(fp_count, n_wco_succ)

    # Ambiguous on definite-truth refs: definite truth + gate=ambiguous.
    amb_on_definite = sum(
        1 for r in successful
        if r["truth"] in _DEFINITE and r["gate"] == _AMBIGUOUS
    )
    amb_rate = _safe_rate(amb_on_definite, n_definite_succ)

    return Metrics(
        corpus_size=n_total,
        n_biographical_truth=n_bio_truth,
        n_work_context_only_truth=n_wco_truth,
        n_ambiguous_truth=n_amb_truth,
        n_biographical_successful=n_bio_succ,
        n_work_context_only_successful=n_wco_succ,
        n_ambiguous_successful=n_amb_succ,
        n_errored=n_errored,
        success_rate=_safe_rate(len(successful), n_total),
        agreement=agreement,
        agreement_wilson_lower=wilson_lower(n_agree, len(successful)),
        fn_rate=fn_rate,
        fn_rate_wilson_upper=wilson_upper(fn_count, n_bio_succ),
        fp_rate=fp_rate,
        fp_rate_wilson_upper=wilson_upper(fp_count, n_wco_succ),
        ambiguous_rate_on_definite_truth=amb_rate,
    )
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_fitness_canary.py -v -k "metrics"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/fitness_canary.py tests/test_fitness_canary.py
git commit -m "feat(canary): Metrics + compute_metrics with Wilson bounds"
```

---

### Task 51: Implement `FitnessFail` + `evaluate(metrics, criteria)`

**Files (toolkit):**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/fitness_canary.py`
- Modify: `~/projects/dsar-toolkit/tests/test_fitness_canary.py`

Per spec §4.4 (C + D + H). Pass = ALL of:
- corpus size ≥ `required_corpus_min_size`
- `success_rate ≥ min_success_rate`
- each definite class size ≥ `min_class_eligible` (against `n_*_truth` — full corpus)
- `agreement_wilson_lower ≥ min_agreement`
- `fn_rate_wilson_upper ≤ max_fn_rate`
- `fp_rate_wilson_upper ≤ max_fp_rate`
- `ambiguous_rate_on_definite_truth ≤ max_ambiguous_ratio`

`FitnessFail.kind = "corpus"` when a class has too few truth-labelled examples regardless of errors (operator must expand the canary); `FitnessFail.kind = "model"` when the model failed the bound. Separate fail-codes for "corpus lacks X" vs "X refs errored".

- [ ] **Step 1: Write failing tests**

Append to `tests/test_fitness_canary.py`:

```python
from dsar_pipeline.fitness_canary import FitnessFail, evaluate


_DEFAULT_CRITERIA = {
    "min_agreement": 0.80,
    "max_fn_rate": 0.20,
    "max_fp_rate": 0.20,
    "max_ambiguous_ratio": 0.20,
    "min_success_rate": 0.85,
    "required_corpus_min_size": 30,
    "min_class_eligible": 12,
}


def _perfect_30() -> list[dict]:
    per_ref = []
    for i in range(12):
        per_ref.append({"ref": f"b{i}", "truth": "biographical",
                         "gate": "biographical", "error_state": None})
    for i in range(12):
        per_ref.append({"ref": f"w{i}", "truth": "work_context_only",
                         "gate": "work_context_only", "error_state": None})
    for i in range(6):
        per_ref.append({"ref": f"a{i}", "truth": "ambiguous",
                         "gate": "ambiguous", "error_state": None})
    return per_ref


def test_evaluate_balanced_30_perfect_passes():
    m = compute_metrics(_perfect_30(), min_success_rate=0.85)
    passed, fails = evaluate(m, _DEFAULT_CRITERIA)
    assert passed is True
    assert fails == []


def test_evaluate_30_with_1_fn_fails():
    per_ref = _perfect_30()
    # Flip one biographical → work_context_only (FN).
    per_ref[0]["gate"] = "work_context_only"
    m = compute_metrics(per_ref, min_success_rate=0.85)
    passed, fails = evaluate(m, _DEFAULT_CRITERIA)
    assert passed is False
    codes = [f.code for f in fails]
    assert "fn_wilson_upper_above_threshold" in codes
    fn_fail = next(f for f in fails if f.code == "fn_wilson_upper_above_threshold")
    assert fn_fail.kind == "model"


def test_evaluate_50_with_1_fn_passes():
    per_ref = []
    for i in range(19):
        per_ref.append({"ref": f"b{i}", "truth": "biographical",
                         "gate": "biographical", "error_state": None})
    per_ref.append({"ref": "b_fn", "truth": "biographical",
                     "gate": "work_context_only", "error_state": None})
    for i in range(20):
        per_ref.append({"ref": f"w{i}", "truth": "work_context_only",
                         "gate": "work_context_only", "error_state": None})
    for i in range(10):
        per_ref.append({"ref": f"a{i}", "truth": "ambiguous",
                         "gate": "ambiguous", "error_state": None})
    m = compute_metrics(per_ref, min_success_rate=0.85)
    passed, fails = evaluate(m, _DEFAULT_CRITERIA)
    assert passed is True, fails


def test_evaluate_corpus_too_small_fails_with_corpus_kind():
    """Corpus of 10 refs (< required_corpus_min_size=30) fails — corpus kind."""
    per_ref = [
        {"ref": f"b{i}", "truth": "biographical",
         "gate": "biographical", "error_state": None}
        for i in range(5)
    ] + [
        {"ref": f"w{i}", "truth": "work_context_only",
         "gate": "work_context_only", "error_state": None}
        for i in range(5)
    ]
    m = compute_metrics(per_ref, min_success_rate=0.85)
    passed, fails = evaluate(m, _DEFAULT_CRITERIA)
    assert passed is False
    corpus_fails = [f for f in fails if f.kind == "corpus"]
    assert any(f.code == "corpus_size_below_minimum" for f in corpus_fails)


def test_evaluate_class_under_threshold_fails_with_corpus_kind():
    """biographical class has only 5 truth-labelled refs (< min_class_eligible=12)."""
    per_ref = [
        {"ref": f"b{i}", "truth": "biographical",
         "gate": "biographical", "error_state": None}
        for i in range(5)
    ] + [
        {"ref": f"w{i}", "truth": "work_context_only",
         "gate": "work_context_only", "error_state": None}
        for i in range(20)
    ] + [
        {"ref": f"a{i}", "truth": "ambiguous",
         "gate": "ambiguous", "error_state": None}
        for i in range(10)
    ]
    m = compute_metrics(per_ref, min_success_rate=0.85)
    passed, fails = evaluate(m, _DEFAULT_CRITERIA)
    assert passed is False
    codes = [f.code for f in fails]
    assert "corpus_biographical_class_below_minimum" in codes
    f0 = next(f for f in fails if f.code == "corpus_biographical_class_below_minimum")
    assert f0.kind == "corpus"


def test_evaluate_distinguishes_corpus_lack_vs_errors():
    """All biographical refs errored → 'class refs errored' (model issue),
    NOT 'corpus lacks class' (those refs ARE there — they just errored)."""
    per_ref = [
        # 12 errored biographical
        {"ref": f"e{i}", "truth": "biographical", "gate": None,
         "error_state": {"code": "timeout", "message": "x"}}
        for i in range(12)
    ] + [
        {"ref": f"w{i}", "truth": "work_context_only",
         "gate": "work_context_only", "error_state": None}
        for i in range(12)
    ] + [
        {"ref": f"a{i}", "truth": "ambiguous",
         "gate": "ambiguous", "error_state": None}
        for i in range(6)
    ]
    m = compute_metrics(per_ref, min_success_rate=0.85)
    passed, fails = evaluate(m, _DEFAULT_CRITERIA)
    assert passed is False
    codes = [f.code for f in fails]
    # The CLASS exists in the corpus (12 truth-labelled refs); model errored on them.
    assert "biographical_class_all_errored" in codes
    assert "corpus_biographical_class_below_minimum" not in codes


def test_evaluate_success_rate_too_low_fails_with_model_kind():
    """Most refs errored → success_rate < 0.85 → model kind."""
    per_ref = [
        {"ref": f"e{i}", "truth": "biographical", "gate": None,
         "error_state": {"code": "timeout", "message": "x"}}
        for i in range(20)
    ] + [
        {"ref": f"w{i}", "truth": "work_context_only",
         "gate": "work_context_only", "error_state": None}
        for i in range(15)
    ]
    m = compute_metrics(per_ref, min_success_rate=0.85)
    passed, fails = evaluate(m, _DEFAULT_CRITERIA)
    assert passed is False
    f0 = next(f for f in fails if f.code == "success_rate_below_minimum")
    assert f0.kind == "model"
```

- [ ] **Step 2: Run; verify ImportError / NameError**

```bash
uv run pytest tests/test_fitness_canary.py -v -k "evaluate"
```

Expected: failures.

- [ ] **Step 3: Implement `FitnessFail` + `evaluate()`**

Append to `src/dsar_pipeline/fitness_canary.py`:

```python
@dataclass(frozen=True)
class FitnessFail:
    code: str
    kind: str       # "corpus" | "model"
    detail: str


def evaluate(metrics: Metrics,
              criteria: dict) -> tuple[bool, list[FitnessFail]]:
    """Apply pass/fail criteria. Returns (passed, fails).

    Spec §4.4 (C). All Wilson bounds with zero-denominator guards —
    when a bound is None, the corresponding class-size check fires
    instead. Separate fail-codes for "corpus lacks X" vs "X refs errored".
    """
    fails: list[FitnessFail] = []
    min_size = criteria["required_corpus_min_size"]
    min_class = criteria["min_class_eligible"]
    min_succ = criteria["min_success_rate"]
    min_agree = criteria["min_agreement"]
    max_fn = criteria["max_fn_rate"]
    max_fp = criteria["max_fp_rate"]
    max_amb = criteria["max_ambiguous_ratio"]

    # Corpus-kind: size + class composition (against FULL counts).
    if metrics.corpus_size < min_size:
        fails.append(FitnessFail(
            code="corpus_size_below_minimum",
            kind="corpus",
            detail=(f"corpus_size={metrics.corpus_size} < "
                    f"required_corpus_min_size={min_size}; expand the canary set"),
        ))

    # Per definite class: distinguish "corpus lacks X" from "X refs errored".
    for cls_name, truth_count, succ_count in (
        ("biographical", metrics.n_biographical_truth, metrics.n_biographical_successful),
        ("work_context_only", metrics.n_work_context_only_truth, metrics.n_work_context_only_successful),
    ):
        if truth_count < min_class:
            fails.append(FitnessFail(
                code=f"corpus_{cls_name}_class_below_minimum",
                kind="corpus",
                detail=(f"only {truth_count} truth-labelled {cls_name} refs "
                        f"(need ≥ {min_class}); add refs to the canary set"),
            ))
        elif succ_count == 0:
            # Class exists in corpus but model errored on every example.
            fails.append(FitnessFail(
                code=f"{cls_name}_class_all_errored",
                kind="model",
                detail=(f"{truth_count} truth-labelled {cls_name} refs but "
                        f"0 successful runs — model errored on all of them"),
            ))

    # Model-kind: success rate.
    if metrics.success_rate is not None and metrics.success_rate < min_succ:
        fails.append(FitnessFail(
            code="success_rate_below_minimum",
            kind="model",
            detail=(f"success_rate={metrics.success_rate:.3f} < "
                    f"min_success_rate={min_succ}; model/infrastructure errors"),
        ))

    # Model-kind: agreement Wilson lower.
    if metrics.agreement_wilson_lower is not None:
        if metrics.agreement_wilson_lower < min_agree:
            fails.append(FitnessFail(
                code="agreement_wilson_lower_below_threshold",
                kind="model",
                detail=(f"agreement_wilson_lower={metrics.agreement_wilson_lower:.3f} < "
                        f"min_agreement={min_agree}"),
            ))

    # Model-kind: FN Wilson upper (the safety-critical bound).
    if metrics.fn_rate_wilson_upper is not None:
        if metrics.fn_rate_wilson_upper > max_fn:
            fails.append(FitnessFail(
                code="fn_wilson_upper_above_threshold",
                kind="model",
                detail=(f"fn_rate_wilson_upper={metrics.fn_rate_wilson_upper:.3f} > "
                        f"max_fn_rate={max_fn} — under-disclosure risk"),
            ))

    # Model-kind: FP Wilson upper.
    if metrics.fp_rate_wilson_upper is not None:
        if metrics.fp_rate_wilson_upper > max_fp:
            fails.append(FitnessFail(
                code="fp_wilson_upper_above_threshold",
                kind="model",
                detail=(f"fp_rate_wilson_upper={metrics.fp_rate_wilson_upper:.3f} > "
                        f"max_fp_rate={max_fp} — over-disclosure noise"),
            ))

    # Model-kind: ambiguous over-use on definite truth.
    if metrics.ambiguous_rate_on_definite_truth is not None:
        if metrics.ambiguous_rate_on_definite_truth > max_amb:
            fails.append(FitnessFail(
                code="ambiguous_rate_above_threshold",
                kind="model",
                detail=(
                    f"ambiguous_rate_on_definite_truth="
                    f"{metrics.ambiguous_rate_on_definite_truth:.3f} > "
                    f"max_ambiguous_ratio={max_amb} — model overuses ambiguous verdict"
                ),
            ))

    return (len(fails) == 0, fails)
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_fitness_canary.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_pipeline/fitness_canary.py tests/test_fitness_canary.py
git commit -m "feat(canary): evaluate(metrics, criteria) → (passed, fails)"
```

---

## Phase 5c — `dsar-fitness-canary` CLI

### Task 52: Implement `dsar-fitness-canary` CLI + report archival

**Files (toolkit):**
- Modify: `~/projects/dsar-toolkit/src/dsar_pipeline/fitness_canary.py` (add main + helpers)
- Modify: `~/projects/dsar-toolkit/pyproject.toml`
- Modify: `~/projects/dsar-toolkit/tests/test_fitness_canary.py`

Per spec §4.4 (B + F + H). CLI:

```
dsar-fitness-canary --deployment-id <id> [--corpus-path <path>]
                     [--criteria <path>] [--model-alias <alias>]
                     [--report-dir <path>]
```

Behaviour:
1. Resolve canary corpus path (default `~/.dsar/canary_sets/<deployment_id>`).
2. Compute `corpus_sha256` early; abort with non-zero on `ValueError`.
3. Load `truth.json` + `refs/<ref>.txt` for each ref.
4. Resolve primary prompt seal via `PromptLoader.load("durant.system")`.
5. Run `GateDurant` (and `GateDurantRecheck` if criteria/config says recheck-enabled). Errors per-ref captured as `error_state` rows; do NOT abort.
6. Compute metrics + evaluate.
7. Write report to `~/.dsar/fitness_reports/<deployment_id>/<timestamp>.json` (timestamp = UTC ISO with `:` → `_`).
8. Print PASS/FAIL summary; exit 0 on PASS, 1 on FAIL.

- [ ] **Step 1: Write a CLI smoke test**

Append to `tests/test_fitness_canary.py`:

```python
import subprocess


def test_cli_smoke_writes_report(tmp_path, monkeypatch):
    """`dsar-fitness-canary --deployment-id test` runs against a stub corpus
    and writes a structured report to <report-dir>."""
    # Seed a minimal canary set
    corpus = tmp_path / "corpus"
    _seed_corpus(corpus, refs=["r1", "r2"],
                 truth={"r1": "biographical", "r2": "work_context_only"})
    report_dir = tmp_path / "reports"

    # Use the in-process main() with --no-llm (test mode that uses a
    # deterministic per-ref classifier mock — see _classify_fn in CLI)
    # so we don't need a real LLM during CI.
    result = subprocess.run(
        ["dsar-fitness-canary",
         "--deployment-id", "test_deploy",
         "--corpus-path", str(corpus),
         "--report-dir", str(report_dir),
         "--model-alias", "stub@test",
         "--no-llm"],
        capture_output=True,
    )
    # We don't assert exit code here — a stub corpus of 2 refs fails the
    # 30-min-size check (corpus kind). What we assert is that the report
    # file was written with the right shape.
    deploy_dir = report_dir / "test_deploy"
    reports = list(deploy_dir.glob("*.json"))
    assert len(reports) == 1, f"expected 1 report, got {reports} (stderr: {result.stderr})"
    import json as _json
    report = _json.loads(reports[0].read_text(encoding="utf-8"))
    # Shape checks per §4.4 (H)
    assert "report_id" in report
    assert "generated_at" in report
    assert "deployment_id" in report
    assert "model_alias" in report
    assert "primary_prompt_seal_sha256" in report
    assert "live_corpus_sha256" in report
    assert "metrics" in report
    assert "criteria" in report
    assert "passed" in report
    assert isinstance(report["passed"], bool)
    assert "fails" in report
    assert isinstance(report["fails"], list)
    for f in report["fails"]:
        assert set(f.keys()) >= {"code", "kind", "detail"}
        assert f["kind"] in ("corpus", "model")
    assert "per_ref" in report
    assert len(report["per_ref"]) == 2
```

- [ ] **Step 2: Run; verify failure**

```bash
uv run pytest tests/test_fitness_canary.py -v -k "smoke"
```

Expected: `dsar-fitness-canary` not found.

- [ ] **Step 3: Implement `main()` + helpers**

Append to `src/dsar_pipeline/fitness_canary.py`:

```python
import argparse
import json
import os
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


_DEFAULT_CRITERIA = {
    "min_agreement": 0.80,
    "max_fn_rate": 0.20,
    "max_fp_rate": 0.20,
    "max_ambiguous_ratio": 0.20,
    "min_success_rate": 0.85,
    "required_corpus_min_size": 30,
    "min_class_eligible": 12,
}


def _load_truth(canary_set_path: Path) -> dict[str, str]:
    return json.loads((canary_set_path / "truth.json").read_text(encoding="utf-8"))


def _load_ref_text(canary_set_path: Path, ref_id: str) -> str:
    return (canary_set_path / "refs" / f"{ref_id}.txt").read_text(encoding="utf-8")


def _stub_classify(ref_id: str, text: str, truth: str) -> dict:
    """Test-only no-LLM classifier — echoes truth as gate output. Used
    by tests so CI doesn't need network/LLM access. Real CLI invocation
    (without --no-llm) routes through GateDurant."""
    return {"ref": ref_id, "truth": truth, "gate": truth, "error_state": None}


def _real_classify(ref_id: str, text: str, truth: str, *,
                    gate_durant) -> dict:
    """Production path: invoke GateDurant on one ref."""
    try:
        verdict = gate_durant.classify_text(text, ref_id=ref_id)
        return {"ref": ref_id, "truth": truth, "gate": verdict.verdict,
                "error_state": None}
    except Exception as e:    # noqa: BLE001 — last-ditch capture for canary
        return {"ref": ref_id, "truth": truth, "gate": None,
                "error_state": {"code": type(e).__name__, "message": str(e)[:200]}}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _build_report(*, deployment_id: str, model_alias: str,
                   primary_seal: str, recheck_seal: str | None,
                   live_corpus_sha: str, criteria: dict,
                   per_ref: list[dict], metrics: Metrics,
                   passed: bool, fails: list[FitnessFail],
                   prompt_id: str) -> dict:
    return {
        "report_id": str(uuid.uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deployment_id": deployment_id,
        "model_alias": model_alias,
        "primary_prompt_seal_sha256": primary_seal,
        "recheck_prompt_seal_sha256": recheck_seal,
        "prompt_id": prompt_id,
        "live_corpus_sha256": live_corpus_sha,
        "corpus_size": metrics.corpus_size,
        "metrics": asdict(metrics),
        "criteria": dict(criteria),
        "passed": passed,
        "fails": [asdict(f) for f in fails],
        "per_ref": per_ref,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dsar-fitness-canary")
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--corpus-path", type=Path, default=None,
                         help="Default: ~/.dsar/canary_sets/<deployment_id>")
    parser.add_argument("--criteria", type=Path, default=None,
                         help="JSON file overriding criteria; merged into defaults")
    parser.add_argument("--model-alias", default="claude-opus-4-7@anthropic")
    parser.add_argument("--report-dir", type=Path, default=None,
                         help="Default: ~/.dsar/fitness_reports/<deployment_id>")
    parser.add_argument("--no-llm", action="store_true",
                         help="Test-only: use stub classifier (echoes truth)")
    args = parser.parse_args(argv)

    canary_path = args.corpus_path or (
        Path.home() / ".dsar" / "canary_sets" / args.deployment_id)
    if not canary_path.is_dir():
        print(f"ERROR: canary set path not found: {canary_path}",
               file=sys.stderr)
        return 2
    try:
        live_corpus_sha = compute_corpus_sha256(canary_path)
    except ValueError as e:
        print(f"ERROR: canary corpus invalid: {e}", file=sys.stderr)
        return 2

    criteria = dict(_DEFAULT_CRITERIA)
    if args.criteria is not None:
        try:
            criteria.update(json.loads(args.criteria.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: criteria file unreadable: {e}", file=sys.stderr)
            return 2

    # Resolve primary prompt seal (recheck is optional; for the canary
    # CLI we always include both seals in the report so the conductor's
    # tuple-match has the data to decide on).
    from dsar_pipeline.gates.prompt_loader import PromptLoader
    primary_asset = PromptLoader.load("durant.system")
    try:
        recheck_asset = PromptLoader.load("durant.recheck.system")
        recheck_seal = recheck_asset.canonical_seal_sha256
    except Exception:
        recheck_seal = None

    truth = _load_truth(canary_path)

    # Build the classifier function. --no-llm path stays in-process for tests.
    if args.no_llm:
        classify_fn = lambda r, t, truth_lbl: _stub_classify(r, t, truth_lbl)
    else:
        # Production: instantiate GateDurant.
        from dsar_pipeline.gates.gate_durant import GateDurant
        gate_durant = GateDurant(model_alias=args.model_alias)
        classify_fn = lambda r, t, truth_lbl: _real_classify(
            r, t, truth_lbl, gate_durant=gate_durant)

    per_ref: list[dict] = []
    for ref_id in sorted(truth.keys()):
        truth_lbl = truth[ref_id]
        try:
            text = _load_ref_text(canary_path, ref_id)
        except OSError as e:
            per_ref.append({"ref": ref_id, "truth": truth_lbl, "gate": None,
                              "error_state": {"code": "ref_text_unreadable",
                                              "message": str(e)[:200]}})
            continue
        per_ref.append(classify_fn(ref_id, text, truth_lbl))

    metrics = compute_metrics(per_ref, criteria["min_success_rate"])
    passed, fails = evaluate(metrics, criteria)

    report = _build_report(
        deployment_id=args.deployment_id,
        model_alias=args.model_alias,
        primary_seal=primary_asset.canonical_seal_sha256,
        recheck_seal=recheck_seal,
        live_corpus_sha=live_corpus_sha,
        criteria=criteria,
        per_ref=per_ref,
        metrics=metrics,
        passed=passed,
        fails=fails,
        prompt_id="durant.system",
    )

    report_dir = args.report_dir or (
        Path.home() / ".dsar" / "fitness_reports" / args.deployment_id)
    ts_safe = report["generated_at"].replace(":", "_").replace("+", "_")
    out_path = report_dir / f"{ts_safe}.json"
    _atomic_write_json(out_path, report)

    print(f"Report: {out_path}")
    if passed:
        print(f"PASS — corpus_size={metrics.corpus_size}, "
               f"agreement_wilson_lower={metrics.agreement_wilson_lower}, "
               f"fn_rate_wilson_upper={metrics.fn_rate_wilson_upper}")
        return 0
    print("FAIL:")
    for f in fails:
        print(f"  {f.kind}: {f.code} — {f.detail}")
    return 1
```

- [ ] **Step 4: Add entry-point in `pyproject.toml`**

Edit `~/projects/dsar-toolkit/pyproject.toml` `[project.scripts]`. Add:

```toml
dsar-fitness-canary = "dsar_pipeline.fitness_canary:main"
```

Then re-install editable:

```bash
cd ~/projects/dsar-toolkit
uv pip install -e .
```

- [ ] **Step 5: Run smoke test; verify report shape**

```bash
uv run pytest tests/test_fitness_canary.py::test_cli_smoke_writes_report -v
```

Expected: PASS.

- [ ] **Step 6: Run end-to-end against the shipped baseline (no-LLM)**

```bash
dsar-fitness-canary --deployment-id test_baseline \
  --corpus-path ~/projects/dsar-toolkit/examples/canary_baseline \
  --report-dir /tmp/canary_reports_baseline_test \
  --model-alias stub@test \
  --no-llm
```

Expected output (using stub classifier, truth==gate):
- `Report: /tmp/canary_reports_baseline_test/test_baseline/<ts>.json`
- `FAIL:` — corpus_size=6 < 30, biographical truth class size = 3 < 12, etc. The shipped baseline is intentionally small for unit-test simplicity; operators expand it locally to ≥30 refs.

- [ ] **Step 7: Commit**

```bash
git add src/dsar_pipeline/fitness_canary.py pyproject.toml tests/test_fitness_canary.py
git commit -m "feat(canary): dsar-fitness-canary CLI + structured report"
```

---

## Phase 5d — Orchestrator: CaseConfig + verify.py + preflight

### Task 53: Add 4 fitness_check fields to `CaseConfig`

**Files (orchestrator):**
- Modify: `~/projects/dsar-orchestrator/src/dsar_orchestrator/config.py`
- Modify: `~/projects/dsar-orchestrator/tests/test_config.py`

Per spec §10.2.

- [ ] **Step 1: Write failing test**

Append to `tests/test_config.py`:

```python
def test_case_config_fitness_check_fields_default(tmp_path):
    """CaseConfig has fitness_check_* fields with safe defaults."""
    case_dir = tmp_path / "case_default"
    case_dir.mkdir()
    (case_dir / "case_config.json").write_text(
        '{"case_no": "TEST", "case_scope": "x"}', encoding="utf-8")
    from dsar_orchestrator.config import load_case_config
    cfg = load_case_config("TEST", case_root=case_dir)
    assert cfg.fitness_check_enabled is True
    assert cfg.fitness_check_canary_path is None
    assert cfg.fitness_check_max_report_age_days == 30
    assert cfg.force_skip_fitness_reason == ""


def test_case_config_fitness_check_fields_from_yaml(tmp_path):
    """All 4 fitness_check_* fields read from case_config.json."""
    case_dir = tmp_path / "case_custom"
    case_dir.mkdir()
    (case_dir / "case_config.json").write_text(
        '{"case_no": "TEST", "case_scope": "x", '
        '"fitness_check_enabled": false, '
        '"fitness_check_canary_path": "/tmp/canary", '
        '"fitness_check_max_report_age_days": 7, '
        '"force_skip_fitness_reason": "operator pilot run"}',
        encoding="utf-8")
    from dsar_orchestrator.config import load_case_config
    cfg = load_case_config("TEST", case_root=case_dir)
    assert cfg.fitness_check_enabled is False
    from pathlib import Path as _P
    assert cfg.fitness_check_canary_path == _P("/tmp/canary")
    assert cfg.fitness_check_max_report_age_days == 7
    assert cfg.force_skip_fitness_reason == "operator pilot run"
```

- [ ] **Step 2: Run; verify failures**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_config.py -v -k "fitness"
```

Expected: AttributeError.

- [ ] **Step 3: Extend `CaseConfig` dataclass**

In `src/dsar_orchestrator/config.py`, add the 4 fields immediately after the existing `resolve_flags_as` field in the dataclass:

```python
    # Phase 5 — model-fitness canary pre-flight (spec §4.4 + §10.2).
    #
    # YAML schema (all keys optional; defaults preserve current behaviour):
    #
    #   {
    #     ...,
    #     "fitness_check_enabled": true,          # default true; gate is ON
    #     "fitness_check_canary_path": null,      # default ~/.dsar/canary_sets/<deployment_id>
    #     "fitness_check_max_report_age_days": 30,
    #     "force_skip_fitness_reason": ""          # non-blank string bypasses + audits
    #   }
    #
    # The pre-flight halts the run if a matching fresh+passing fitness
    # report does not exist. Operators can:
    #   - opt out per-case via `fitness_check_enabled: false`
    #   - bypass with audit via `force_skip_fitness_reason: "<reason>"` (non-blank)
    #   - run the canary inline via the CLI's `--auto-fitness` flag
    fitness_check_enabled: bool = True
    fitness_check_canary_path: Path | None = None
    fitness_check_max_report_age_days: int = 30
    force_skip_fitness_reason: str = ""
```

And in the `load_case_config()` body, after the existing `resolve_flags_as=...` row in the `CaseConfig(...)` ctor, add:

```python
        fitness_check_enabled=bool(raw.get("fitness_check_enabled", True)),
        fitness_check_canary_path=(
            Path(raw["fitness_check_canary_path"]).expanduser()
            if raw.get("fitness_check_canary_path") else None
        ),
        fitness_check_max_report_age_days=int(
            raw.get("fitness_check_max_report_age_days", 30)),
        force_skip_fitness_reason=str(raw.get("force_skip_fitness_reason", "")),
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_config.py -v -k "fitness"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/config.py tests/test_config.py
git commit -m "feat(config): fitness_check_* fields on CaseConfig"
```

---

### Task 54: Implement `verify.py` (`verify_prompt_versions`, `verify_fitness_report`)

**Files (orchestrator):**
- Create: `~/projects/dsar-orchestrator/src/dsar_orchestrator/verify.py`
- Create: `~/projects/dsar-orchestrator/tests/test_verify.py`

Per spec §4.1 (G) + §10.2.

`verify_prompt_versions(case_dir, *, strict=False)`:
1. Import toolkit's `dsar_pipeline.gates.prompt_loader`. Read `_PROMPTS_DIR / "_registry.json"`.
2. For each row in `<case_dir>/working/durant_verdicts.jsonl` AND `<case_dir>/working/durant_underdisclosure_recheck.jsonl` (if present):
   - Find a registry entry whose `seal_sha256 == row.prompt_canonical_seal_sha256`.
   - Cross-check `row.prompt_id` matches the registry entry's prompt_id.
   - Load `_archive/<prompt_id>/<version>.md.gz` → parse → replay `applied_strips` → recompute effective sha → compare to `row.prompt_effective_sha256`.
3. Return `VerifyResult(ok=bool, exit_code=int, warnings=list[str], errors=list[str])`.
   - `exit_code = 0` on success.
   - `exit_code = 1` if a row references a registered but older-than-current version + NOT strict (warning).
   - `exit_code = 2` if any hash drift OR (older version + `strict=True`).

`verify_fitness_report(case_dir)`:
1. Load `<case_dir>/case_config.json` → derive `deployment_id` from `cfg.case_path / "case_config.json"` `fitness_check` section.
2. Look in `~/.dsar/fitness_reports/<deployment_id>/` for a passing report within `max_report_age_days`.
3. Same `VerifyResult` return shape.

- [ ] **Step 1: Write failing tests**

Create `tests/test_verify.py`:

```python
"""Tests for src/dsar_orchestrator/verify.py (spec §4.1 G + §4.4)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest


def _seed_prompt_registry(tmp_path: Path) -> tuple[Path, str, str]:
    """Build a minimal in-tmp prompt registry + archive. Returns
    (prompts_dir, canonical_seal, effective_sha)."""
    prompts = tmp_path / "prompts"
    archive = prompts / "_archive" / "durant.system"
    archive.mkdir(parents=True)
    body = "Test body of durant.system prompt.\n"
    meta = {"prompt_id": "durant.system", "version": "1.0.0",
            "droppable_blocks": []}
    from dsar_pipeline.gates.prompt_loader import compute_seal
    import hashlib
    seal = compute_seal(meta, body)
    effective = hashlib.sha256(body.encode("utf-8")).hexdigest()
    asset_text = (
        f"---\nprompt_id: \"durant.system\"\nversion: \"1.0.0\"\n"
        f"seal_sha256: \"{seal}\"\ndroppable_blocks: []\n---\n{body}"
    )
    (prompts / "durant.system.md").write_text(asset_text, encoding="utf-8")
    # Archive
    with gzip.open(archive / "1.0.0.md.gz", "wb", mtime=0) as gz:
        gz.write(asset_text.encode("utf-8"))
    (prompts / "_registry.json").write_text(
        json.dumps({
            "durant.system": [{
                "version": "1.0.0", "seal_sha256": seal,
                "archived_at": "2026-05-26",
            }],
        }), encoding="utf-8")
    return prompts, seal, effective


def _seed_case(case_dir: Path, primary_rows: list[dict],
                recheck_rows: list[dict] | None = None) -> None:
    (case_dir / "working").mkdir(parents=True, exist_ok=True)
    with open(case_dir / "working" / "durant_verdicts.jsonl", "w",
                encoding="utf-8") as f:
        for r in primary_rows:
            f.write(json.dumps(r) + "\n")
    if recheck_rows is not None:
        with open(case_dir / "working" /
                    "durant_underdisclosure_recheck.jsonl", "w",
                    encoding="utf-8") as f:
            for r in recheck_rows:
                f.write(json.dumps(r) + "\n")


def test_verify_prompt_versions_fresh_case_returns_ok(tmp_path, monkeypatch):
    prompts, seal, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr(
        "dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(case, primary_rows=[{
        "doc_ref": "r1",
        "prompt_id": "durant.system",
        "prompt_canonical_seal_sha256": seal,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": effective,
    }])
    from dsar_orchestrator.verify import verify_prompt_versions
    result = verify_prompt_versions(case)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.errors == []


def test_verify_prompt_versions_planted_drift_exits_2(tmp_path, monkeypatch):
    """If the audit row's effective_sha256 doesn't match the archive's
    replayed body → exit 2."""
    prompts, seal, _ = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr(
        "dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(case, primary_rows=[{
        "doc_ref": "r1",
        "prompt_id": "durant.system",
        "prompt_canonical_seal_sha256": seal,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": "0" * 64,    # planted drift
    }])
    from dsar_orchestrator.verify import verify_prompt_versions
    result = verify_prompt_versions(case)
    assert result.ok is False
    assert result.exit_code == 2
    assert any("effective_sha256" in e for e in result.errors)


def test_verify_prompt_versions_unknown_seal_exits_2(tmp_path, monkeypatch):
    prompts, _, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr(
        "dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(case, primary_rows=[{
        "doc_ref": "r1",
        "prompt_id": "durant.system",
        "prompt_canonical_seal_sha256": "f" * 64,    # not in registry
        "prompt_applied_strips": [],
        "prompt_effective_sha256": effective,
    }])
    from dsar_orchestrator.verify import verify_prompt_versions
    result = verify_prompt_versions(case)
    assert result.ok is False
    assert result.exit_code == 2


def test_verify_prompt_versions_id_mismatch_exits_2(tmp_path, monkeypatch):
    """Audit row's prompt_id != registry's prompt_id for the matching seal."""
    prompts, seal, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr(
        "dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(case, primary_rows=[{
        "doc_ref": "r1",
        "prompt_id": "WRONG.id",
        "prompt_canonical_seal_sha256": seal,
        "prompt_applied_strips": [],
        "prompt_effective_sha256": effective,
    }])
    from dsar_orchestrator.verify import verify_prompt_versions
    result = verify_prompt_versions(case)
    assert result.ok is False
    assert result.exit_code == 2


def test_verify_prompt_versions_older_version_warn_unless_strict(tmp_path, monkeypatch):
    """Registry has v1.0.0 AND v1.1.0; case audit refs v1.0.0. Without
    --strict → warning + exit 0. With --strict → exit 2."""
    prompts, seal_v1, effective_v1 = _seed_prompt_registry(tmp_path)
    # Sign a v1.1.0 too and add to registry (different body → different seal).
    from dsar_pipeline.gates.prompt_loader import compute_seal
    body_v11 = "Body of durant.system v1.1.0.\n"
    meta_v11 = {"prompt_id": "durant.system", "version": "1.1.0",
                 "droppable_blocks": []}
    seal_v11 = compute_seal(meta_v11, body_v11)
    asset_text_v11 = (
        f"---\nprompt_id: \"durant.system\"\nversion: \"1.1.0\"\n"
        f"seal_sha256: \"{seal_v11}\"\ndroppable_blocks: []\n---\n{body_v11}"
    )
    (prompts / "durant.system.md").write_text(asset_text_v11, encoding="utf-8")
    archive = prompts / "_archive" / "durant.system"
    with gzip.open(archive / "1.1.0.md.gz", "wb", mtime=0) as gz:
        gz.write(asset_text_v11.encode("utf-8"))
    registry = json.loads((prompts / "_registry.json").read_text())
    registry["durant.system"].append({
        "version": "1.1.0", "seal_sha256": seal_v11,
        "archived_at": "2026-05-27",
    })
    (prompts / "_registry.json").write_text(
        json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(
        "dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(case, primary_rows=[{
        "doc_ref": "r1",
        "prompt_id": "durant.system",
        "prompt_canonical_seal_sha256": seal_v1,    # old
        "prompt_applied_strips": [],
        "prompt_effective_sha256": effective_v1,
    }])
    from dsar_orchestrator.verify import verify_prompt_versions
    result = verify_prompt_versions(case, strict=False)
    assert result.exit_code == 0
    assert result.warnings   # at least one
    result_strict = verify_prompt_versions(case, strict=True)
    assert result_strict.exit_code == 2


def test_verify_prompt_versions_includes_recheck_jsonl(tmp_path, monkeypatch):
    """Recheck JSONL rows are verified too."""
    prompts, seal, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr(
        "dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(case,
                 primary_rows=[{
                     "doc_ref": "r1",
                     "prompt_id": "durant.system",
                     "prompt_canonical_seal_sha256": seal,
                     "prompt_applied_strips": [],
                     "prompt_effective_sha256": effective,
                 }],
                 recheck_rows=[{
                     "doc_ref": "r1",
                     "prompt_id": "durant.system",
                     "prompt_canonical_seal_sha256": "f" * 64,    # planted
                     "prompt_applied_strips": [],
                     "prompt_effective_sha256": effective,
                 }])
    from dsar_orchestrator.verify import verify_prompt_versions
    result = verify_prompt_versions(case)
    assert result.exit_code == 2


def test_verify_fitness_report_passes_when_fresh_passing(tmp_path, monkeypatch):
    """A recent passing report under <report-dir> → OK."""
    case = tmp_path / "case"
    (case).mkdir()
    (case / "case_config.json").write_text(json.dumps({
        "case_no": "TEST",
        "case_scope": "x",
        "fitness_check_deployment_id": "test_deploy",
        "fitness_check_max_report_age_days": 30,
    }), encoding="utf-8")
    report_root = tmp_path / "reports"
    deploy = report_root / "test_deploy"
    deploy.mkdir(parents=True)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    (deploy / f"{now.replace(':', '_')}.json").write_text(
        json.dumps({
            "report_id": "abc",
            "generated_at": now,
            "deployment_id": "test_deploy",
            "passed": True,
            "fails": [],
        }), encoding="utf-8")
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    from dsar_orchestrator.verify import verify_fitness_report
    result = verify_fitness_report(case)
    assert result.ok is True
    assert result.exit_code == 0


def test_verify_fitness_report_fails_when_no_report(tmp_path, monkeypatch):
    case = tmp_path / "case"
    (case).mkdir()
    (case / "case_config.json").write_text(json.dumps({
        "case_no": "TEST",
        "case_scope": "x",
        "fitness_check_deployment_id": "test_deploy_missing",
    }), encoding="utf-8")
    report_root = tmp_path / "reports_empty"
    report_root.mkdir()
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    from dsar_orchestrator.verify import verify_fitness_report
    result = verify_fitness_report(case)
    assert result.ok is False
    assert result.exit_code != 0
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_verify.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `verify.py`**

Create `src/dsar_orchestrator/verify.py`:

```python
"""Audit-row verification — spec §4.1 (G) + §4.4.

Two verifiers, both invoked by `dsar-conductor verify --check ...`:

  * `verify_prompt_versions(case_dir, *, strict=False)` — for each row
    in durant_verdicts.jsonl + recheck JSONL, look up the canonical
    seal in the installed toolkit's _registry.json, load the archived
    asset, replay applied_strips, recompute the effective sha, compare
    to the audit row's recorded effective_sha256. Catches both
    accidental drift (different toolkit version used at run time vs
    audit time) and tampering.

  * `verify_fitness_report(case_dir)` — confirms a matching fresh +
    passing fitness report exists under
    ~/.dsar/fitness_reports/<deployment_id>/ (or override via
    DSAR_FITNESS_REPORT_ROOT env).
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class VerifyResult:
    ok: bool
    exit_code: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _iter_jsonl_rows(path: Path):
    """Yield decoded JSON objects from a JSONL file. Skips blank lines."""
    if not path.is_file():
        return
    with open(path, encoding="utf-8") as f:
        for ln_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield ln_no, json.loads(raw)
            except json.JSONDecodeError as e:
                # Surface as an error; corrupt JSONL is verify-fatal.
                yield ln_no, {"_decode_error": str(e)}


def _read_archived_asset(archive_path: Path) -> tuple[dict, str]:
    """Load a gzipped archived asset and return (meta, body)."""
    raw_gz = archive_path.read_bytes()
    text = gzip.decompress(raw_gz).decode("utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{archive_path}: no leading ---")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{archive_path}: frontmatter not terminated")
    import yaml
    fm_text = text[4:end]
    body = text[end + len("\n---\n"):]
    meta = yaml.safe_load(fm_text)
    return meta, body


def _replay_effective(body: str, applied_strips: list[str],
                       droppable: set[str]) -> str:
    """Replay the strip+normalise pipeline from prompt_loader against
    an archived body. Imports from toolkit so the rules stay in sync."""
    import hashlib
    from dsar_pipeline.gates.prompt_loader import (
        _normalise_whitespace, _strip_block,
    )
    processed = body
    for sid in applied_strips:
        if sid not in droppable:
            raise ValueError(
                f"applied_strips contains non-droppable id {sid!r}")
        processed = _strip_block(processed, sid)
    processed = _normalise_whitespace(processed)
    return hashlib.sha256(processed.encode("utf-8")).hexdigest()


def verify_prompt_versions(case_dir: Path, *,
                             strict: bool = False) -> VerifyResult:
    """Walk durant_verdicts.jsonl + recheck JSONL and cross-check every
    row's prompt hashes against the installed toolkit's registry."""
    result = VerifyResult(ok=True, exit_code=0)

    # Late toolkit import — orchestrator can install standalone.
    try:
        from dsar_pipeline.gates import prompt_loader as pl
    except ImportError:
        result.ok = False
        result.exit_code = 2
        result.errors.append(
            "dsar-toolkit not installed — `pip install -e ~/projects/dsar-toolkit`")
        return result

    registry_path = pl._PROMPTS_DIR / "_registry.json"
    if not registry_path.is_file():
        result.ok = False
        result.exit_code = 2
        result.errors.append(f"prompt registry missing: {registry_path}")
        return result
    registry: dict[str, list[dict]] = json.loads(
        registry_path.read_text(encoding="utf-8"))

    # Build seal → (prompt_id, version, archive_path) index.
    seal_index: dict[str, tuple[str, str, Path]] = {}
    current_version_by_id: dict[str, str] = {}
    for prompt_id, entries in registry.items():
        for entry in entries:
            seal = entry["seal_sha256"]
            archive_path = (pl._PROMPTS_DIR / "_archive" /
                              prompt_id / f"{entry['version']}.md.gz")
            seal_index[seal] = (prompt_id, entry["version"], archive_path)
        if entries:
            current_version_by_id[prompt_id] = entries[-1]["version"]

    rows_to_check: list[tuple[str, int, dict]] = []
    primary_jsonl = case_dir / "working" / "durant_verdicts.jsonl"
    for ln_no, row in _iter_jsonl_rows(primary_jsonl):
        rows_to_check.append(("durant_verdicts.jsonl", ln_no, row))
    recheck_jsonl = case_dir / "working" / "durant_underdisclosure_recheck.jsonl"
    for ln_no, row in _iter_jsonl_rows(recheck_jsonl):
        rows_to_check.append(("durant_underdisclosure_recheck.jsonl", ln_no, row))

    if not rows_to_check:
        result.warnings.append("no audit rows found to verify")

    for source, ln_no, row in rows_to_check:
        if "_decode_error" in row:
            result.errors.append(
                f"{source}:{ln_no}: malformed JSON: {row['_decode_error']}")
            continue
        seal = row.get("prompt_canonical_seal_sha256")
        if not seal:
            result.errors.append(
                f"{source}:{ln_no}: missing prompt_canonical_seal_sha256")
            continue
        if seal not in seal_index:
            result.errors.append(
                f"{source}:{ln_no}: canonical seal {seal} not in registry")
            continue
        prompt_id, version, archive_path = seal_index[seal]
        if row.get("prompt_id") != prompt_id:
            result.errors.append(
                f"{source}:{ln_no}: prompt_id mismatch — "
                f"row={row.get('prompt_id')!r} registry={prompt_id!r}")
            continue
        try:
            meta, body = _read_archived_asset(archive_path)
        except (OSError, ValueError) as e:
            result.errors.append(
                f"{source}:{ln_no}: cannot read archive {archive_path}: {e}")
            continue
        droppable = set(meta.get("droppable_blocks", []) or [])
        applied = list(row.get("prompt_applied_strips", []) or [])
        try:
            replayed = _replay_effective(body, applied, droppable)
        except ValueError as e:
            result.errors.append(f"{source}:{ln_no}: replay error: {e}")
            continue
        expected_eff = row.get("prompt_effective_sha256")
        if replayed != expected_eff:
            result.errors.append(
                f"{source}:{ln_no}: effective_sha256 drift — "
                f"row={expected_eff} replayed={replayed}")
            continue
        # Older-version check
        current = current_version_by_id.get(prompt_id)
        if current is not None and version != current:
            msg = (f"{source}:{ln_no}: row uses {prompt_id} v{version}; "
                    f"current registered is v{current}")
            if strict:
                result.errors.append(msg)
            else:
                result.warnings.append(msg)

    if result.errors:
        result.ok = False
        result.exit_code = 2
    elif result.warnings and strict:
        result.ok = False
        result.exit_code = 2
    elif result.warnings:
        result.ok = True
        result.exit_code = 0
    return result


def _read_case_config(case_dir: Path) -> dict[str, Any]:
    path = case_dir / "case_config.json"
    if not path.is_file():
        raise FileNotFoundError(f"case_config.json missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_fitness_report(case_dir: Path) -> VerifyResult:
    """Confirms a fresh + passing fitness report exists for this case's
    deployment_id under DSAR_FITNESS_REPORT_ROOT or
    ~/.dsar/fitness_reports/<deployment_id>/.
    """
    result = VerifyResult(ok=True, exit_code=0)
    try:
        cfg_raw = _read_case_config(case_dir)
    except FileNotFoundError as e:
        result.ok = False
        result.exit_code = 1
        result.errors.append(str(e))
        return result

    deployment_id = (
        cfg_raw.get("fitness_check_deployment_id")
        or cfg_raw.get("deployment_id")
        or ""
    )
    if not deployment_id:
        result.ok = False
        result.exit_code = 1
        result.errors.append(
            "case_config.json missing fitness_check_deployment_id")
        return result

    max_age = int(cfg_raw.get("fitness_check_max_report_age_days", 30))
    report_root = Path(
        os.environ.get("DSAR_FITNESS_REPORT_ROOT",
                        str(Path.home() / ".dsar" / "fitness_reports"))
    )
    deploy_dir = report_root / deployment_id
    if not deploy_dir.is_dir():
        result.ok = False
        result.exit_code = 1
        result.errors.append(
            f"no fitness reports directory at {deploy_dir} for "
            f"deployment_id={deployment_id}")
        return result

    now = datetime.now(timezone.utc)
    fresh_passing: list[tuple[str, str]] = []   # (path, generated_at)
    for rp in sorted(deploy_dir.glob("*.json")):
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            result.warnings.append(f"unreadable report {rp}: {e}")
            continue
        if not r.get("passed", False):
            continue
        gen_at = r.get("generated_at", "")
        try:
            gen_dt = datetime.fromisoformat(gen_at)
        except ValueError:
            continue
        age_days = (now - gen_dt).total_seconds() / 86400.0
        if age_days > max_age:
            continue
        fresh_passing.append((str(rp), gen_at))

    if not fresh_passing:
        result.ok = False
        result.exit_code = 1
        result.errors.append(
            f"no fresh+passing fitness report (≤{max_age}d) under "
            f"{deploy_dir}; run dsar-fitness-canary "
            f"--deployment-id {deployment_id}")
        return result

    return result
```

- [ ] **Step 4: Run all verify tests; verify pass**

```bash
uv run pytest tests/test_verify.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/verify.py tests/test_verify.py
git commit -m "feat(verify): verify_prompt_versions + verify_fitness_report"
```

---

### Task 55: Implement `_run_fitness_preflight()` + hook into `pipeline.run()`

**Files (orchestrator):**
- Modify: `~/projects/dsar-orchestrator/src/dsar_orchestrator/pipeline.py`
- Create: `~/projects/dsar-orchestrator/tests/test_conductor_fitness_preflight.py`

Per spec §4.4 (F) + §10.2. The preflight runs BEFORE STAGE_ORDER[0] when `cfg.fitness_check_enabled`. It:

1. If `cfg.force_skip_fitness_reason` is non-blank → records `case_audit/skip_fitness.json` and returns OK (skip).
2. Otherwise: computes the lookup tuple (`deployment_id`, `model_alias`, `primary_seal`, `recheck_seal`, `live_corpus_sha`, `inference_params_sha`).
3. Resolves canary path (cfg field → default `~/.dsar/canary_sets/<deployment_id>`).
4. Resolves report dir (env `DSAR_FITNESS_REPORT_ROOT` or `~/.dsar/fitness_reports/<deployment_id>`).
5. Calls `find_matching_report(tuple_)`. Halts with `PipelineHalt` on:
   - canary path missing
   - corpus invalid (ValueError from `compute_corpus_sha256`)
   - no matching report found
   - report's `corpus_sha256` ≠ live (explicit drift guard)
   - report older than `max_report_age_days`
   - report's `passed=False`

`--auto-fitness` is implemented by the CLI: when set, the CLI calls `dsar-fitness-canary` BEFORE invoking `pipeline.run()` on missing/stale/failing.

- [ ] **Step 1: Write the preflight tests**

Create `tests/test_conductor_fitness_preflight.py`:

```python
"""Tests for the orchestrator's _run_fitness_preflight (spec §4.4 F)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _seed_canary(corpus_dir: Path) -> None:
    (corpus_dir / "refs").mkdir(parents=True)
    (corpus_dir / "canary_corpus.json").write_text(
        '{"version":1,"refs":["r1"]}', encoding="utf-8")
    (corpus_dir / "truth.json").write_text(
        '{"r1":"biographical"}', encoding="utf-8")
    (corpus_dir / "refs" / "r1.txt").write_text("body\n", encoding="utf-8")


def _seed_case(case_dir: Path, **cfg_overrides) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "case_no": "TEST",
        "case_scope": "x",
        "fitness_check_enabled": True,
        "fitness_check_deployment_id": "test_deploy",
        "fitness_check_max_report_age_days": 30,
    }
    base.update(cfg_overrides)
    (case_dir / "case_config.json").write_text(
        json.dumps(base), encoding="utf-8")


def _write_report(report_root: Path, deployment_id: str, *,
                    passed: bool, age_days: float = 0.0,
                    corpus_sha: str | None = None) -> None:
    deploy = report_root / deployment_id
    deploy.mkdir(parents=True, exist_ok=True)
    gen_at = (datetime.now(timezone.utc)
                - timedelta(days=age_days)).isoformat()
    safe_name = gen_at.replace(":", "_")
    body = {
        "report_id": "abc",
        "generated_at": gen_at,
        "deployment_id": deployment_id,
        "passed": passed,
        "fails": [] if passed else [
            {"code": "fn_wilson_upper_above_threshold",
             "kind": "model", "detail": "test"}],
        "live_corpus_sha256": corpus_sha,
    }
    (deploy / f"{safe_name}.json").write_text(
        json.dumps(body), encoding="utf-8")


def test_preflight_halts_when_no_report(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    monkeypatch.setenv("DSAR_CANARY_PATH_OVERRIDE", str(canary))

    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight
    from dsar_orchestrator.audit import PipelineAuditor

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="no fitness report"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_halts_when_report_stale(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case, fitness_check_max_report_age_days=7)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    report_root = tmp_path / "reports"
    _write_report(report_root, "test_deploy", passed=True, age_days=14.0)
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))

    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight
    from dsar_orchestrator.audit import PipelineAuditor

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="stale"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_halts_when_report_failing(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    report_root = tmp_path / "reports"
    _write_report(report_root, "test_deploy", passed=False, age_days=1.0)
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))

    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight
    from dsar_orchestrator.audit import PipelineAuditor

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="fitness failed|fn_wilson"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_halts_on_corpus_drift(tmp_path, monkeypatch):
    """Report's live_corpus_sha256 != current live corpus sha → halt."""
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    # Plant a passing report with a wrong corpus sha.
    report_root = tmp_path / "reports"
    _write_report(report_root, "test_deploy", passed=True,
                   age_days=1.0, corpus_sha="0" * 64)
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))

    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.exceptions import PipelineHalt
    from dsar_orchestrator.pipeline import _run_fitness_preflight
    from dsar_orchestrator.audit import PipelineAuditor

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    with pytest.raises(PipelineHalt, match="drift|corpus_sha"):
        _run_fitness_preflight(cfg, auditor)


def test_preflight_passes_when_fresh_passing_matching(tmp_path, monkeypatch):
    """Happy path: fresh, passing, corpus_sha matches → no exception."""
    case = tmp_path / "case"
    _seed_case(case)
    canary = tmp_path / "canary"
    _seed_canary(canary)
    from dsar_pipeline.canary_corpus import compute_corpus_sha256
    live_sha = compute_corpus_sha256(canary)
    report_root = tmp_path / "reports"
    _write_report(report_root, "test_deploy", passed=True,
                   age_days=1.0, corpus_sha=live_sha)
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))

    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.pipeline import _run_fitness_preflight
    from dsar_orchestrator.audit import PipelineAuditor

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    _run_fitness_preflight(cfg, auditor)  # must not raise


def test_preflight_force_skip_writes_audit_row_and_proceeds(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case, force_skip_fitness_reason="operator pilot run")
    canary = tmp_path / "canary"
    _seed_canary(canary)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))

    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.pipeline import _run_fitness_preflight
    from dsar_orchestrator.audit import PipelineAuditor

    cfg = load_case_config("TEST", case_root=case)
    cfg.fitness_check_canary_path = canary
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    _run_fitness_preflight(cfg, auditor)  # must not raise

    skip_audit = case / "case_audit" / "skip_fitness.json"
    assert skip_audit.is_file()
    rec = json.loads(skip_audit.read_text(encoding="utf-8"))
    assert rec["reason"] == "operator pilot run"
    assert "os_user" in rec
    assert "hostname" in rec
    assert "timestamp" in rec
    assert "fitness_tuple" in rec


def test_preflight_skipped_when_disabled(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _seed_case(case, fitness_check_enabled=False)

    from dsar_orchestrator.config import load_case_config
    from dsar_orchestrator.pipeline import _run_fitness_preflight
    from dsar_orchestrator.audit import PipelineAuditor

    cfg = load_case_config("TEST", case_root=case)
    auditor = PipelineAuditor("TEST", audit_root=tmp_path / "audit")
    _run_fitness_preflight(cfg, auditor)  # must be no-op
```

- [ ] **Step 2: Run; verify failures (NameError / AttributeError)**

```bash
uv run pytest tests/test_conductor_fitness_preflight.py -v
```

Expected: ImportError for `_run_fitness_preflight`.

- [ ] **Step 3: Implement preflight helper**

In `src/dsar_orchestrator/pipeline.py`, add near the top of the file (after the existing imports but before `STAGE_ORDER`):

```python
import getpass
import hashlib
import socket
```

Add a new helper, **before** the `run()` function definition:

```python
def _compute_inference_params_sha256(cfg: CaseConfig) -> str:
    """Canonicalised hash of the inference params that affect Durant
    classification — model alias + temperature + truncation cap. Used
    in the fitness-report lookup tuple."""
    params = {
        "model_alias": getattr(cfg, "model_alias", "claude-opus-4-7@anthropic"),
        "max_text_chars": getattr(cfg, "max_text_chars", 32000),
    }
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_matching_report(*, report_dir: Path, deployment_id: str,
                            model_alias: str, primary_seal: str,
                            recheck_seal: str | None, live_corpus_sha: str,
                            inference_params_sha: str,
                            max_age_days: int) -> tuple[dict | None, str | None]:
    """Search `report_dir` for the most-recent report whose tuple matches.

    Returns (report_dict, fail_reason). On a clean match: (report, None).
    On no match: (None, "<reason>")."""
    deploy_dir = report_dir / deployment_id
    if not deploy_dir.is_dir():
        return None, f"no reports directory at {deploy_dir}"
    candidates: list[tuple[datetime, dict, Path]] = []
    for rp in sorted(deploy_dir.glob("*.json")):
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if r.get("deployment_id") != deployment_id:
            continue
        if r.get("model_alias") != model_alias:
            continue
        if r.get("primary_prompt_seal_sha256") != primary_seal:
            continue
        if r.get("recheck_prompt_seal_sha256") != recheck_seal:
            continue
        if r.get("inference_params_sha256") not in (None, inference_params_sha):
            # Older reports may not have this field; we accept missing
            # (None) but enforce match when present.
            continue
        try:
            gen_dt = datetime.fromisoformat(r["generated_at"])
        except (KeyError, ValueError):
            continue
        candidates.append((gen_dt, r, rp))

    if not candidates:
        return None, "no fitness report matching tuple"

    candidates.sort(reverse=True, key=lambda t: t[0])
    gen_dt, latest, _ = candidates[0]
    age_days = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 86400.0
    if age_days > max_age_days:
        return None, (f"latest report is stale "
                        f"({age_days:.1f}d > {max_age_days}d)")
    if latest.get("live_corpus_sha256") != live_corpus_sha:
        return None, (
            f"corpus_sha256 drift: report="
            f"{(latest.get('live_corpus_sha256') or '')[:16]}… "
            f"live={live_corpus_sha[:16]}…")
    if not latest.get("passed", False):
        fails = latest.get("fails", [])
        detail = "; ".join(
            f"{f.get('kind')}: {f.get('code')}" for f in fails) or "unknown"
        return None, f"fitness failed: {detail}"
    return latest, None


def _write_skip_fitness_audit(case_path: Path, *, reason: str,
                                fitness_tuple: dict) -> None:
    audit_dir = case_path / "case_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "reason": reason,
        "os_user": getpass.getuser(),
        "hostname": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fitness_tuple": fitness_tuple,
        "last_known_report_id": None,
    }
    path = audit_dir / "skip_fitness.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _run_fitness_preflight(cfg: CaseConfig,
                             auditor: PipelineAuditor) -> None:
    """Spec §4.4 (F). Pre-flight gate before STAGE_ORDER[0].

    No-op when cfg.fitness_check_enabled is False. Halts with
    PipelineHalt on missing/stale/failing/drift reports. Force-skip
    via cfg.force_skip_fitness_reason records an audit row + proceeds.
    """
    if not cfg.fitness_check_enabled:
        auditor.note("fitness_preflight", "fitness_check disabled by config")
        return

    with StageBanner(auditor, "fitness_preflight"):
        # Force-skip path: audit + proceed.
        if cfg.force_skip_fitness_reason.strip():
            cfg_raw = json.loads(
                (cfg.case_path / "case_config.json").read_text(encoding="utf-8"))
            deployment_id = (
                cfg_raw.get("fitness_check_deployment_id") or "")
            fitness_tuple = {
                "deployment_id": deployment_id,
                "model_alias": getattr(cfg, "model_alias",
                                        "claude-opus-4-7@anthropic"),
            }
            _write_skip_fitness_audit(
                cfg.case_path,
                reason=cfg.force_skip_fitness_reason.strip(),
                fitness_tuple=fitness_tuple,
            )
            auditor.note(
                "fitness_preflight",
                f"force_skip_fitness: {cfg.force_skip_fitness_reason!r}",
            )
            return

        cfg_raw = json.loads(
            (cfg.case_path / "case_config.json").read_text(encoding="utf-8"))
        deployment_id = cfg_raw.get("fitness_check_deployment_id") or ""
        if not deployment_id:
            raise PipelineHalt(
                f"case={cfg.case_no}: fitness_check_enabled but "
                f"`fitness_check_deployment_id` missing in case_config.json. "
                f"Either set it, set fitness_check_enabled=false, "
                f"or pass --force-skip-fitness \"<reason>\"."
            )

        canary_override = os.environ.get("DSAR_CANARY_PATH_OVERRIDE")
        canary_path = (
            Path(canary_override) if canary_override
            else (cfg.fitness_check_canary_path
                    or Path.home() / ".dsar" / "canary_sets" / deployment_id)
        )
        if not canary_path.is_dir():
            raise PipelineHalt(
                f"case={cfg.case_no}: canary set path not found: {canary_path}. "
                f"Run `dsar-fitness-canary --deployment-id {deployment_id}` "
                f"first or pass --auto-fitness."
            )

        # Compute live corpus sha — surface ValueError as a halt.
        from dsar_pipeline.canary_corpus import compute_corpus_sha256
        try:
            live_corpus_sha = compute_corpus_sha256(canary_path)
        except ValueError as e:
            raise PipelineHalt(
                f"case={cfg.case_no}: canary corpus invalid: {e}") from e

        # Resolve prompt seals.
        try:
            from dsar_pipeline.gates.prompt_loader import PromptLoader
            primary_seal = PromptLoader.load("durant.system").canonical_seal_sha256
            try:
                recheck_seal = (
                    PromptLoader.load("durant.recheck.system").canonical_seal_sha256
                )
            except Exception:
                recheck_seal = None
        except ImportError as e:
            raise PipelineHalt(
                f"case={cfg.case_no}: dsar-toolkit not installed for "
                f"fitness pre-flight: {e}") from e

        model_alias = getattr(cfg, "model_alias", "claude-opus-4-7@anthropic")
        inference_params_sha = _compute_inference_params_sha256(cfg)

        report_root = Path(
            os.environ.get(
                "DSAR_FITNESS_REPORT_ROOT",
                str(Path.home() / ".dsar" / "fitness_reports"),
            )
        )

        report, fail_reason = _find_matching_report(
            report_dir=report_root,
            deployment_id=deployment_id,
            model_alias=model_alias,
            primary_seal=primary_seal,
            recheck_seal=recheck_seal,
            live_corpus_sha=live_corpus_sha,
            inference_params_sha=inference_params_sha,
            max_age_days=cfg.fitness_check_max_report_age_days,
        )
        if report is None:
            # Tailor the leading word so tests can match precisely.
            leading = (
                "no fitness report"
                if "no fitness report" in (fail_reason or "")
                or "no reports directory" in (fail_reason or "")
                else "stale" if "stale" in (fail_reason or "")
                else "drift" if "drift" in (fail_reason or "")
                else "fitness failed"
            )
            raise PipelineHalt(
                f"case={cfg.case_no}: fitness pre-flight halt: "
                f"{leading} ({fail_reason}). "
                f"Run `dsar-fitness-canary --deployment-id {deployment_id}`, "
                f"or pass --auto-fitness on the conductor."
            )

        auditor.note(
            "fitness_preflight",
            f"report_id={report.get('report_id')} "
            f"passed=True age_ok corpus_ok",
        )
```

Wire it into `run()`. Find the block immediately before the existing `# Stage 1 — ingest (serial)` comment inside the `try:` block of `run()` and insert:

```python
        # Phase 5 (spec §4.4): fitness pre-flight. Halts BEFORE any
        # stage runs if the model is not certified fit. Skipped when
        # cfg.fitness_check_enabled is False.
        _run_fitness_preflight(cfg, audit)
```

- [ ] **Step 4: Run preflight tests; verify pass**

```bash
uv run pytest tests/test_conductor_fitness_preflight.py -v
```

Expected: all 7 PASS.

- [ ] **Step 5: Run the full orchestrator test suite to confirm no regressions**

```bash
uv run pytest -q 2>&1 | tail -20
```

Expected: no NEW failures (pre-existing failures from other phases — if any — flagged but not blocking).

- [ ] **Step 6: Commit**

```bash
git add src/dsar_orchestrator/pipeline.py tests/test_conductor_fitness_preflight.py
git commit -m "feat(pipeline): _run_fitness_preflight hook + force-skip audit"
```

---

## Phase 5e — Orchestrator CLI subparsers

### Task 56: Convert `cli.py` to subparsers (default `run`, new `verify`)

**Files (orchestrator):**
- Modify: `~/projects/dsar-orchestrator/src/dsar_orchestrator/cli.py`
- Create: `~/projects/dsar-orchestrator/tests/test_cli_subparsers.py`

Per spec §10.2. Key constraint: `dsar-conductor --case X` (no subcommand) must continue to work — the parser routes a missing subcommand to `run`. `dsar-conductor verify --check {prompt-versions, fitness-report} --case <id> [--strict]` is new. `--auto-fitness` and `--force-skip-fitness "<reason>"` are new flags ON the `run` subcommand.

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_subparsers.py`:

```python
"""Tests for the post-Phase-5 subparser-based CLI (spec §10.2)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest


def _seed_case(case_dir: Path) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case_config.json").write_text(json.dumps({
        "case_no": "TEST", "case_scope": "x",
        "fitness_check_enabled": False,
    }), encoding="utf-8")


def test_default_subcommand_is_run_backcompat(tmp_path):
    """`dsar-conductor --case X` (no subcommand) still works = run."""
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    with mock.patch("dsar_orchestrator.cli.run") as run_mock:
        rc = main(["--case", "TEST", "--case-root", str(case), "--check"])
    assert rc == 0
    run_mock.assert_called_once()
    # The run kwargs should include case_no=TEST.
    _, kwargs = run_mock.call_args
    assert kwargs.get("case_no") == "TEST"


def test_explicit_run_subcommand_works(tmp_path):
    """`dsar-conductor run --case X` works (same as default)."""
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    with mock.patch("dsar_orchestrator.cli.run") as run_mock:
        rc = main(["run", "--case", "TEST",
                    "--case-root", str(case), "--check"])
    assert rc == 0
    run_mock.assert_called_once()


def test_run_subcommand_auto_fitness_flag_parsed(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    with mock.patch("dsar_orchestrator.cli.run") as run_mock, \
         mock.patch("dsar_orchestrator.cli._inline_fitness_canary") \
         as canary_mock:
        rc = main(["run", "--case", "TEST", "--case-root", str(case),
                    "--check", "--auto-fitness"])
    assert rc == 0


def test_run_subcommand_force_skip_fitness_sets_env(tmp_path, monkeypatch):
    """--force-skip-fitness "<reason>" sets a config override on cfg."""
    monkeypatch.delenv("DSAR_FORCE_SKIP_FITNESS_REASON", raising=False)
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    with mock.patch("dsar_orchestrator.cli.run") as run_mock:
        rc = main(["run", "--case", "TEST", "--case-root", str(case),
                    "--check",
                    "--force-skip-fitness", "operator pilot"])
    assert rc == 0
    import os
    assert os.environ.get("DSAR_FORCE_SKIP_FITNESS_REASON") == "operator pilot"


def test_run_subcommand_force_skip_rejects_empty(tmp_path):
    """--force-skip-fitness "" is rejected (non-blank required)."""
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    rc = main(["run", "--case", "TEST", "--case-root", str(case),
                "--check", "--force-skip-fitness", ""])
    assert rc != 0


def test_verify_subcommand_prompt_versions_dispatches(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    with mock.patch("dsar_orchestrator.cli.verify_prompt_versions") as v_mock:
        from dsar_orchestrator.verify import VerifyResult
        v_mock.return_value = VerifyResult(ok=True, exit_code=0)
        rc = main(["verify", "--case", "TEST",
                    "--case-root", str(case),
                    "--check", "prompt-versions"])
    assert rc == 0
    v_mock.assert_called_once()


def test_verify_subcommand_fitness_report_dispatches(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    with mock.patch("dsar_orchestrator.cli.verify_fitness_report") as v_mock:
        from dsar_orchestrator.verify import VerifyResult
        v_mock.return_value = VerifyResult(ok=True, exit_code=0)
        rc = main(["verify", "--case", "TEST",
                    "--case-root", str(case),
                    "--check", "fitness-report"])
    assert rc == 0
    v_mock.assert_called_once()


def test_verify_subcommand_strict_propagates(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    with mock.patch("dsar_orchestrator.cli.verify_prompt_versions") as v_mock:
        from dsar_orchestrator.verify import VerifyResult
        v_mock.return_value = VerifyResult(ok=False, exit_code=2)
        rc = main(["verify", "--case", "TEST",
                    "--case-root", str(case),
                    "--check", "prompt-versions",
                    "--strict"])
    assert rc == 2
    _args, kwargs = v_mock.call_args
    assert kwargs.get("strict") is True
```

- [ ] **Step 2: Run; verify failures**

```bash
uv run pytest tests/test_cli_subparsers.py -v
```

Expected: failures.

- [ ] **Step 3: Rewrite `cli.py` with subparsers**

Replace the contents of `src/dsar_orchestrator/cli.py`:

```python
"""``dsar-conductor`` — operator CLI for the orchestrator.

Argparse subparsers:

  - `run` (default): orchestrate a case run. `dsar-conductor --case X`
    (no subcommand) is preserved as the historic invocation and routes
    to `run` transparently.
  - `verify`: post-hoc audit-row checks (`--check prompt-versions |
    fitness-report`; optional `--strict`).

Flags on `run`:
  - `--auto-fitness`: on a missing/stale/failing fitness report, run
    `dsar-fitness-canary` inline before proceeding (per spec §4.4 F).
  - `--force-skip-fitness "<non-blank reason>"`: bypass + record
    `case_audit/skip_fitness.json`; empty reason is rejected.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dsar_orchestrator import __version__
from dsar_orchestrator.exceptions import DSARPipelineError, PipelineHalt
from dsar_orchestrator.pipeline import ALL_STAGE_NAMES, STAGE_ORDER, run
from dsar_orchestrator.verify import verify_fitness_report, verify_prompt_versions


def _add_run_args(p: argparse.ArgumentParser) -> None:
    """Shared args for the `run` subcommand AND the historic flat form."""
    p.add_argument("--case", required=True, metavar="<case-no>")
    p.add_argument("--case-root", type=Path, default=None,
                     metavar="<path>")
    p.add_argument("--from", dest="from_stage", choices=STAGE_ORDER,
                     default=None)
    p.add_argument("--through", dest="through_stage", choices=STAGE_ORDER,
                     default=None)
    p.add_argument("--only", dest="only_stage", choices=ALL_STAGE_NAMES,
                     default=None, metavar="<stage>")
    p.add_argument("--check", action="store_true",
                     help="Print resume plan; don't run.")
    p.add_argument("--dry-run", action="store_true",
                     help="Same as --check.")
    p.add_argument("--force", action="store_true",
                     help="Disable the resume cascade.")
    p.add_argument("--acknowledge-issues", action="store_true",
                     help="Clear any analyser block flag and proceed.")
    p.add_argument("--resolve-flags-as", choices=("true", "false"),
                     default=None, metavar="<true|false>",
                     help="Auto-resolve detect-stage flags. See #26.")
    # Phase 5 additions:
    p.add_argument("--auto-fitness", action="store_true",
                     help=("On missing/stale/failing fitness report, "
                           "run `dsar-fitness-canary` inline then proceed."))
    p.add_argument("--force-skip-fitness", metavar="<reason>",
                     default=None,
                     help=("Bypass fitness pre-flight + record audit. "
                           "Reason must be non-blank."))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dsar-conductor",
        description="Orchestrate a DSAR case run through the dsar-toolkit modular pipeline.",
    )
    p.add_argument(
        "--version", action="version",
        version=f"dsar-conductor (dsar_orchestrator) {__version__}",
    )

    sub = p.add_subparsers(dest="cmd", required=False)

    # `run` — the default subcommand.
    p_run = sub.add_parser("run", help="Orchestrate a case run (default).")
    _add_run_args(p_run)

    # `verify` — post-hoc audit checks.
    p_verify = sub.add_parser("verify", help="Verify audit rows / fitness report.")
    p_verify.add_argument("--case", required=True, metavar="<case-no>")
    p_verify.add_argument("--case-root", type=Path, default=None,
                            metavar="<path>")
    p_verify.add_argument(
        "--check", required=True,
        choices=("prompt-versions", "fitness-report"),
        help="What to verify.",
    )
    p_verify.add_argument(
        "--strict", action="store_true",
        help="Upgrade warnings (older-but-registered versions) to errors.",
    )

    # Back-compat: also accept the run-subcommand args directly on the
    # top-level parser. argparse can't have both subparsers + top-level
    # `--case` cleanly, so we detect missing subcommand in main() and
    # re-parse with the run subparser.
    return p


def _resolve_case_path(case_no: str, case_root: Path | None) -> Path:
    return case_root or (Path.home() / "dsars" / "cases" / case_no)


def _inline_fitness_canary(case_no: str, case_root: Path) -> int:
    """Best-effort `dsar-fitness-canary` invocation for --auto-fitness.

    Reads the deployment_id from case_config.json; falls back to
    DSAR_DEPLOYMENT_ID env. Returns the subprocess exit code.
    """
    import json as _json
    cfg_path = case_root / "case_config.json"
    deployment_id = ""
    if cfg_path.is_file():
        try:
            cfg_raw = _json.loads(cfg_path.read_text(encoding="utf-8"))
            deployment_id = cfg_raw.get("fitness_check_deployment_id", "")
        except (OSError, _json.JSONDecodeError):
            pass
    if not deployment_id:
        deployment_id = os.environ.get("DSAR_DEPLOYMENT_ID", "")
    if not deployment_id:
        print("--auto-fitness: no fitness_check_deployment_id in case_config.json",
               file=sys.stderr)
        return 2
    proc = subprocess.run(
        ["dsar-fitness-canary", "--deployment-id", deployment_id],
        check=False,
    )
    return proc.returncode


def _dispatch_run(args: argparse.Namespace) -> int:
    if args.only_stage and (args.from_stage or args.through_stage):
        print("--only is mutually exclusive with --from / --through",
               file=sys.stderr)
        return 2
    # Validate --force-skip-fitness: empty / whitespace-only → reject.
    if args.force_skip_fitness is not None:
        if not args.force_skip_fitness.strip():
            print("--force-skip-fitness requires a non-blank reason",
                   file=sys.stderr)
            return 2
        os.environ["DSAR_FORCE_SKIP_FITNESS_REASON"] = args.force_skip_fitness

    if args.resolve_flags_as is not None:
        os.environ["DSAR_RESOLVE_FLAGS_AS"] = args.resolve_flags_as

    # --auto-fitness: try canary inline (caller continues regardless;
    # the pre-flight will re-evaluate after canary writes its report).
    if args.auto_fitness:
        rc = _inline_fitness_canary(
            args.case,
            _resolve_case_path(args.case, args.case_root),
        )
        if rc != 0:
            print(f"auto-fitness canary returned {rc}; pre-flight may still halt.",
                   file=sys.stderr)

    try:
        run(
            case_no=args.case,
            case_root=args.case_root,
            from_stage=args.from_stage,
            through_stage=args.through_stage,
            only_stage=args.only_stage,
            dry_run=args.dry_run,
            check=args.check,
            force=args.force,
            acknowledge_issues=args.acknowledge_issues,
        )
    except PipelineHalt as e:
        print(f"\nPIPELINE HALTED: {e}", file=sys.stderr)
        return 2
    except DSARPipelineError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"\nCONFIG ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def _dispatch_verify(args: argparse.Namespace) -> int:
    case_path = _resolve_case_path(args.case, args.case_root)
    if args.check == "prompt-versions":
        result = verify_prompt_versions(case_path, strict=args.strict)
    elif args.check == "fitness-report":
        result = verify_fitness_report(case_path)
    else:
        print(f"unknown --check {args.check!r}", file=sys.stderr)
        return 2
    for w in result.warnings:
        print(f"WARN: {w}", file=sys.stderr)
    for e in result.errors:
        print(f"ERROR: {e}", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(argv) if argv is not None else sys.argv[1:]

    # Back-compat: missing subcommand → default to `run`.
    # We detect "no subcommand" by checking whether argv[0] is one of
    # our known subcommand names.
    known_subcommands = {"run", "verify"}
    if not argv or (argv[0].startswith("-") and argv[0] != "--version"):
        argv = ["run"] + argv
    elif argv[0] not in known_subcommands and not argv[0].startswith("-"):
        # Could be a positional from --case=X — fall back to run.
        argv = ["run"] + argv

    args = parser.parse_args(argv)

    if args.cmd == "run" or args.cmd is None:
        return _dispatch_run(args)
    if args.cmd == "verify":
        return _dispatch_verify(args)
    parser.error(f"unknown subcommand: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run subparser tests; verify pass**

```bash
uv run pytest tests/test_cli_subparsers.py -v
```

Expected: all 8 PASS.

- [ ] **Step 5: Run the existing CLI tests; verify no regressions**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: pre-existing CLI tests still pass (the `dsar-conductor --case X` invocation routes through `run` by default).

- [ ] **Step 6: Commit**

```bash
git add src/dsar_orchestrator/src/dsar_orchestrator/cli.py tests/test_cli_subparsers.py 2>/dev/null || git add src/dsar_orchestrator/cli.py tests/test_cli_subparsers.py
git commit -m "feat(cli): subparsers; verify subcommand; --auto-fitness; --force-skip-fitness"
```

---

### Task 57: Wire the env-var force-skip into `CaseConfig` so the preflight sees it

**Files (orchestrator):**
- Modify: `~/projects/dsar-orchestrator/src/dsar_orchestrator/config.py`

The CLI flag sets `DSAR_FORCE_SKIP_FITNESS_REASON`; `load_case_config()` needs to read that env var and apply it to `cfg.force_skip_fitness_reason` (env > config precedence, same pattern as `RESOLVE_FLAGS_AS`).

- [ ] **Step 1: Write failing test**

Append to `tests/test_config.py`:

```python
def test_case_config_force_skip_fitness_env_overrides_yaml(tmp_path, monkeypatch):
    case_dir = tmp_path / "case_envskip"
    case_dir.mkdir()
    (case_dir / "case_config.json").write_text(
        '{"case_no": "TEST", "case_scope": "x", '
        '"force_skip_fitness_reason": ""}',
        encoding="utf-8")
    monkeypatch.setenv("DSAR_FORCE_SKIP_FITNESS_REASON", "from-env")
    from dsar_orchestrator.config import load_case_config
    cfg = load_case_config("TEST", case_root=case_dir)
    assert cfg.force_skip_fitness_reason == "from-env"
```

- [ ] **Step 2: Run; verify failure**

```bash
uv run pytest tests/test_config.py::test_case_config_force_skip_fitness_env_overrides_yaml -v
```

- [ ] **Step 3: Implement env-var read**

In `src/dsar_orchestrator/config.py`'s `load_case_config()`, change the line:

```python
        force_skip_fitness_reason=str(raw.get("force_skip_fitness_reason", "")),
```

to:

```python
        force_skip_fitness_reason=str(
            os.environ.get(
                "DSAR_FORCE_SKIP_FITNESS_REASON",
                raw.get("force_skip_fitness_reason", "") or "",
            )
        ),
```

- [ ] **Step 4: Run tests; verify pass**

```bash
uv run pytest tests/test_config.py -v -k "fitness"
```

- [ ] **Step 5: Commit**

```bash
git add src/dsar_orchestrator/config.py tests/test_config.py
git commit -m "feat(config): DSAR_FORCE_SKIP_FITNESS_REASON env var overrides YAML"
```

---

### Task 58: End-to-end smoke (acceptance scenarios)

**Files:** none modified — this is a verification-only task that exercises everything Tasks 47–57 built.

- [ ] **Step 1: Smoke `dsar-fitness-canary --no-llm` against the shipped baseline**

```bash
cd ~/projects/dsar-toolkit
rm -rf /tmp/canary_smoke
dsar-fitness-canary --deployment-id smoke \
  --corpus-path ~/projects/dsar-toolkit/examples/canary_baseline \
  --report-dir /tmp/canary_smoke \
  --model-alias stub@test \
  --no-llm
```

Expected:
- Exits non-zero (corpus_size=6 < 30, class sizes < 12 — these are corpus-kind fails).
- `/tmp/canary_smoke/smoke/<ts>.json` exists with the spec §4.4 (H) shape.
- `fails` array contains `{"code": "corpus_size_below_minimum", "kind": "corpus", ...}` and `{"code": "corpus_biographical_class_below_minimum", ...}`.

```bash
jq '.passed, .corpus_size, [.fails[] | {code, kind}]' /tmp/canary_smoke/smoke/*.json
```

- [ ] **Step 2: Smoke `dsar-conductor verify --check prompt-versions` on a known-clean case**

Create a tiny fixture case with one valid Durant audit row referencing the current signed `durant.system` asset (use `dsar-prompt show` to capture the current canonical_seal):

```bash
SEAL=$(dsar-prompt show durant.system | grep -oE 'canonical_seal_sha256=[0-9a-f]+' | head -1 | cut -d= -f2)
EFFECTIVE=$(dsar-prompt show durant.system | grep -oE 'effective_sha256=[0-9a-f]+' | head -1 | cut -d= -f2)
mkdir -p /tmp/case_verify_clean/working
cat > /tmp/case_verify_clean/working/durant_verdicts.jsonl <<EOF
{"doc_ref": "r1", "prompt_id": "durant.system", "prompt_canonical_seal_sha256": "$SEAL", "prompt_applied_strips": [], "prompt_effective_sha256": "$EFFECTIVE"}
EOF
cat > /tmp/case_verify_clean/case_config.json <<EOF
{"case_no": "VERIFY", "case_scope": "x", "fitness_check_enabled": false}
EOF

cd ~/projects/dsar-orchestrator
dsar-conductor verify --case VERIFY --case-root /tmp/case_verify_clean --check prompt-versions
echo "exit=$?"
```

Expected: exit 0.

- [ ] **Step 3: Smoke planted hash drift → exit 2**

```bash
cp /tmp/case_verify_clean/working/durant_verdicts.jsonl{,.bak}
sed -i.bak 's/"prompt_effective_sha256":[^,}]*/"prompt_effective_sha256":"0000000000000000000000000000000000000000000000000000000000000000"/' /tmp/case_verify_clean/working/durant_verdicts.jsonl
dsar-conductor verify --case VERIFY --case-root /tmp/case_verify_clean --check prompt-versions
echo "exit=$?"
```

Expected: exit 2, stderr mentions `effective_sha256 drift`.

Restore the file:

```bash
mv /tmp/case_verify_clean/working/durant_verdicts.jsonl.bak /tmp/case_verify_clean/working/durant_verdicts.jsonl
```

- [ ] **Step 4: Smoke historic invocation `dsar-conductor --case X` still works**

```bash
dsar-conductor --case VERIFY --case-root /tmp/case_verify_clean --check
echo "exit=$?"
```

Expected: prints the resume plan, exit 0. Confirms the missing-subcommand-default-to-run back-compat.

- [ ] **Step 5: Smoke `--force-skip-fitness "<reason>"` bypass**

```bash
cat > /tmp/case_force_skip/case_config.json <<'EOF'
{"case_no": "SKIP", "case_scope": "x", "fitness_check_enabled": true, "fitness_check_deployment_id": "no_such_deploy"}
EOF
mkdir -p /tmp/case_force_skip/working

# Without the flag, this should halt (no fitness report exists).
# Use --check to avoid running actual stages.
dsar-conductor run --case SKIP --case-root /tmp/case_force_skip --check \
  --force-skip-fitness "operator pilot run"
echo "exit=$?"

# Verify the skip_fitness audit row was written.
test -f /tmp/case_force_skip/case_audit/skip_fitness.json && \
  jq . /tmp/case_force_skip/case_audit/skip_fitness.json
```

Expected: exit 0 (force-skip bypasses); `skip_fitness.json` contains `reason: "operator pilot run"`, `os_user`, `hostname`, `timestamp`, `fitness_tuple`.

- [ ] **Step 6: Final commit (no code change; just a sentinel commit marking Phase 5 complete)**

```bash
cd ~/projects/dsar-orchestrator
git commit --allow-empty -m "chore(phase5): durant-pipeline-hardening Phase 5 acceptance smoke green"
```

---

## Acceptance criteria for Phase 5

Phase 5 is done when ALL of these hold:

- [ ] `dsar-fitness-canary --deployment-id <id>` runs end-to-end against `examples/canary_baseline/` for a configured model (`--no-llm` for CI; real model alias for operator-machine smoke).
- [ ] `dsar-conductor verify --check prompt-versions --case <fixture>` exits 0 on a clean fresh case; exits 2 on planted effective-hash drift.
- [ ] `dsar-conductor verify --check fitness-report --case <fixture>` exits 0 when a fresh+passing report exists; non-zero otherwise.
- [ ] `dsar-conductor --case X` (no subcommand, historic invocation) still works — routes to `run` by default.
- [ ] `dsar-conductor run --case X --auto-fitness` invokes `dsar-fitness-canary` inline before pre-flight on missing/stale reports.
- [ ] `dsar-conductor run --case X --force-skip-fitness "<reason>"` bypasses pre-flight and writes `case_audit/skip_fitness.json` with `{reason, os_user, hostname, timestamp, fitness_tuple, last_known_report_id}`. Empty reason is rejected (exit ≠ 0).
- [ ] Wilson math worked examples from spec §4.4 hold:
  - 30 refs (12 bio + 12 WCO + 6 amb), all correct → PASS (`fn_rate_wilson_upper ≈ 0.118`).
  - 30 refs, 1 FN → FAIL (`fn_rate_wilson_upper ≈ 0.27 > 0.20`).
  - 50 refs, 1 FN → PASS (`fn_rate_wilson_upper ≈ 0.18 < 0.20`).
- [ ] CI test `test_baseline_corpus_seal` passes — the shipped baseline's `compute_corpus_sha256` matches the pinned hex.
- [ ] All test files green:
  - `tests/test_fitness_canary.py` (toolkit) — corpus hash, metrics, evaluate, CLI smoke.
  - `tests/test_baseline_corpus_seal.py` (toolkit).
  - `tests/test_verify.py` (orchestrator) — both verifiers; planted-drift; older-version warn/strict.
  - `tests/test_conductor_fitness_preflight.py` (orchestrator) — 7 scenarios.
  - `tests/test_cli_subparsers.py` (orchestrator) — 8 scenarios.
  - `tests/test_config.py` (orchestrator) — fitness fields + env override.
- [ ] All commits are atomic (one feature per commit; ≥1 commit per task; 12 tasks → 12+ commits).
- [ ] No existing test regressions (Phase 1–4 tests still pass).

## Self-review

**Spec coverage (Phase 5 only — spec §4.4 + §10.2):**

| Spec subsection | Task(s) | Status |
|---|---|---|
| §4.4 (A) Canary corpus convention | 47 | ✓ |
| §4.4 (A) Baseline examples/canary_baseline shipped | 47 | ✓ |
| §4.4 (G) `compute_corpus_sha256()` — JSON canon, LF, dedup, validation | 48 | ✓ |
| §4.4 (G) CI seal pin `test_baseline_corpus_seal` | 49 | ✓ |
| §4.4 (D) `Metrics` dataclass + `compute_metrics()` — full-corpus class counts, succ-only rate denominators, ambiguous_rate denominator | 50 | ✓ |
| §4.4 (E) Errored refs excluded from rates, counted in class | 50 | ✓ |
| §4.4 (C + D) `evaluate()` — Wilson bounds, zero-denom guard, separate corpus/model fail-codes | 51 | ✓ |
| §4.4 (B) `dsar-fitness-canary` CLI + report archival | 52 | ✓ |
| §4.4 (H) Report shape (report_id, generated_at, full tuple, metrics, criteria, passed, fails, per_ref) | 52 | ✓ |
| §10.2 `CaseConfig` fields (4 new) + YAML schema docstring | 53 | ✓ |
| §4.1 (G) `verify_prompt_versions()` — registry lookup, prompt_id cross-check, archive replay, --strict | 54 | ✓ |
| §10.2 `verify_fitness_report()` — fresh+passing matching | 54 | ✓ |
| §4.4 (F) `_run_fitness_preflight()` — full lookup tuple, halt on each abort case, StageBanner | 55 | ✓ |
| §4.4 (F) `--auto-fitness` opt-in inline canary | 56 | ✓ |
| §4.4 (F) `--force-skip-fitness "<reason>"` + non-blank validation + skip_fitness.json audit row | 55, 56 | ✓ |
| §10.2 `cli.py` flat→subparser conversion + `run` as default | 56 | ✓ |
| `DSAR_FORCE_SKIP_FITNESS_REASON` env-var override on CaseConfig | 57 | ✓ |
| End-to-end acceptance smoke (5 scenarios) | 58 | ✓ |

**Out of scope for Phase 5 (covered in later phases or by spec exclusions):**

- §4.7 `durant-test.md` updates + `tools/check_durant_doc.py` CI lint — Phase 6.
- Auto-tuning of prompts/models from canary failures (spec §4.4 "OUT").
- Multi-corpus per deployment / cross-deployment fitness sharing (spec §4.4 "OUT").
- PKI signing of fitness reports (spec §3 threat model).
- Stratified-sampling enforcement (spec §4.4 "OUT").
- Operator-calibration portal itself (spec §2 — consumed by §4.2, not §4.4).

**Placeholder scan:** None. Every `Step` has full code; every command has full args; the only deliberate placeholder is `PINNED_BASELINE_SHA = "<BASELINE_SHA>"` in Task 49, which the operator fills in from the printed output of Task 49 Step 1.

**Type consistency:**
- `Metrics` field set used identically across `compute_metrics()` (Task 50), `evaluate()` (Task 51), `_build_report()` (Task 52). ✓
- `FitnessFail(code, kind, detail)` shape consistent in `evaluate()` and report's `fails[]`. ✓
- `VerifyResult(ok, exit_code, errors, warnings)` shape consistent across `verify_prompt_versions()`, `verify_fitness_report()`, and CLI dispatch. ✓
- `compute_corpus_sha256(canary_set_path)` signature stable across the CLI (Task 52), pre-flight (Task 55), and CI seal pin (Task 49). ✓
- `_find_matching_report()` lookup-tuple field set stable: `deployment_id`, `model_alias`, `primary_seal`, `recheck_seal`, `live_corpus_sha`, `inference_params_sha`. ✓

**Decisions deviating from spec (intentional):**

- **Subparser default-to-run via argv preprocessing rather than the more elegant "subparsers required=False + manual dispatch when args.cmd is None".** Both approaches work; argv preprocessing keeps `_dispatch_run` ignorant of the back-compat path. Acceptable because the alternative would also require the same `argv[0] not in known_subcommands` detection somewhere.
- **`_inline_fitness_canary()` is best-effort** — its non-zero return does not abort the run dispatch, because the pre-flight will re-evaluate after the canary writes its report. This matches spec §4.4 (F)'s "inline-runs `dsar-fitness-canary` then proceeds on pass" semantics: the proceed gate is the pre-flight, not the inline canary's exit code.
- **`fitness_check_deployment_id` lives in case_config.json as a TOP-LEVEL key**, not under a nested `fitness_check` block. The spec body shows nested (`case_cfg.fitness_check.deployment_id`); we flatten to match the existing CaseConfig conventions (every other field is top-level). Documented in the dataclass docstring.
- **`_compute_inference_params_sha256()` covers `model_alias` + `max_text_chars` only.** Spec §4.4 (F) says "inference_params_sha256" without enumerating fields. Phase 5 includes the minimum subset that demonstrably affects Durant output; future fields (e.g., temperature once `RoleRouter` exposes it) can be added as a config-additive change since `_find_matching_report()` already accepts None for older reports.
- **`--auto-fitness` shells out to `dsar-fitness-canary` rather than importing `dsar_pipeline.fitness_canary.main`.** Keeps the orchestrator's import surface narrow; if the canary lives behind a vendored zipapp on an air-gapped operator workstation, the CLI invocation works either way.

**Dependencies for downstream phases (Phase 6 spec §4.7):**

- Phase 6's `durant-test.md` lint depends on the new `dsar-conductor verify` subcommand being documented in the doc and listed in `required_terms` of `durant-doc-lint.yaml`. No code dependency.

---

*End of Phase 5 plan. Continue with Phase 6 plan (covers spec §4.7 — `durant-test.md` updates + `tools/check_durant_doc.py` CI lint).*
