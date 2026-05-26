# Durant Pipeline Hardening — Phase 6 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Scope of this plan: Phase 6 only.** Phases 1–5 already landed the toolkit + orchestrator code per §§4.1–4.6. This plan covers spec §4.7 — the reference doc (`docs/durant-test.md`) update + CI lint infrastructure. By design choice (see "Phasing note" below), **Phase 6 owns ALL the `durant-test.md` edits** that §4.7 (A) maps per-spec: Phases 1–5 focused on code; this phase brings the reference doc in sync with deployed behaviour and adds the CI lint that prevents future drift.
>
> Predecessor phase plans (all assumed merged before Phase 6 begins):
>
> - Phase 1 (`2026-05-26-durant-pipeline-hardening-phase1.md`) — §4.1 prompt asset + loader + §4.3 truncation primitives.
> - Phase 2 — §4.3 token-belt completion + §4.5 role-field sanitiser + GateDurant integration.
> - Phase 3 — §4.2 recheck stage + `dsar-recheck` CLI + scope_check_stage integration.
> - Phase 4 — §4.6 Agent22 synthesis + scope_verdicts.jsonl evidence-block extension.
> - Phase 5 — §4.4 fitness canary + orchestrator pre-flight + `dsar-conductor verify` subcommand.

**Goal:** By end of Phase 6:
1. `tools/check_durant_doc.py` exists in the orchestrator repo, parses `docs/durant-test.md` with `markdown-it-py`, masks code regions while preserving newline-count, runs all three checks (path linter, stale-phrase guard, required-heading/term guard) against `body_no_code`, and exits with distinct codes (0=pass / 1=lint failure / 2=config error).
2. `docs/durant-doc-lint.yaml` exists with `stale_phrases`, `required_headings`, `required_terms`, and `bare_filename_allowlist` rules.
3. `docs/durant-test.md` is fully rewritten to match the post-hardening pipeline: new §3.1 Truncation strategy, updated §4 diagram (PromptLoader + recheck + truncation), §5 outputs (new audit fields + files), §6 Toolkit canonical (PromptLoader / RecheckStage / GateDurantRecheck / Agent22), §7 replaced with the `dsar-prompt` CLI / vendored zipapp pattern + runtime hash verification, §8 first-class recheck stage + calibration-gated + mutual-exclusion contract, new §9.0 Model-fitness canary (Wilson-bounded), renumbered §9.1 Calibration, §10 major rewrite (original drift mitigated; new residuals enumerated), §11 cross-references add new toolkit files + canary corpus path, §12 glossary adds new terms.
4. `markdown-it-py` is added to the orchestrator's `pyproject.toml` (under a dedicated `lint` optional-dep group so the runtime/`toolkit` extras stay clean).
5. A GitHub Actions workflow `docs-lint.yml` runs `tools/check_durant_doc.py` on every PR touching `docs/**` or `src/dsar_orchestrator/**`.
6. `tests/test_check_durant_doc.py` exercises line-number accuracy after code-block masking, heading detection after code blocks, multiple stale-phrase occurrences reported, false-positive-free indented code blocks, YAML config error → exit 2, missing-file → exit 2.

**Architecture:** All new files live in `dsar-orchestrator`:
- `tools/check_durant_doc.py` — Python lint script (no `src/dsar_orchestrator/` placement since it's a dev-time tool, not part of the conductor's runtime API).
- `docs/durant-doc-lint.yaml` — rules (separation of policy from script).
- `docs/durant-test.md` — fully rewritten body.
- `.github/workflows/docs-lint.yml` — CI runner.
- `tests/test_check_durant_doc.py` — unit + integration coverage of the lint script.

**Tech stack:** Python 3.10+ (orchestrator's `requires-python`); `markdown-it-py >= 3.0` (new); `PyYAML` (new — orchestrator currently has no YAML dep, but the lint script needs to parse the rules file; pulled in via the `lint` optional-dep group). Existing toolkit deps untouched.

**Phasing note (Phase 6 owns all doc edits).** Spec §4.7 (A) defines an *incremental* doc-update process where Phases 1–5 PRs each touch the relevant `durant-test.md` sections inline. In practice we're handling this in two halves:
- Phases 1–5 commits focus on **code only**; they do not edit `durant-test.md`.
- Phase 6 owns the **complete catch-up rewrite** of `docs/durant-test.md` (all sections from §4.7 (A)) plus the CI lint infrastructure.
This is a deliberate deviation from the spec's "no big-bang final editorial PR" guidance. Rationale: it keeps the per-phase code PRs cleanly scoped to the toolkit (Phases 1–4) vs orchestrator (Phase 5), and avoids merge-conflict churn in `durant-test.md` between phases — the lint script lands at the same time as the rewritten doc, so they validate each other. The trade-off (doc-temporarily-inaccurate between Phases 1–5 and Phase 6 landing) is acceptable because operators are not yet using the new pipeline shape until Phase 5 is in.

---

## File structure

### dsar-orchestrator (creates 4 new files; modifies 2 existing)

```
dsar-orchestrator/
├── tools/
│   └── check_durant_doc.py                # CREATE — lint script
├── docs/
│   ├── durant-doc-lint.yaml               # CREATE — rules
│   └── durant-test.md                     # MODIFY — full rewrite per §4.7 (A)
├── .github/
│   └── workflows/
│       └── docs-lint.yml                  # CREATE — CI runner
├── tests/
│   └── test_check_durant_doc.py           # CREATE — lint coverage
└── pyproject.toml                         # MODIFY — add lint optional-dep group
```

No toolkit changes. No `src/dsar_orchestrator/` changes (the lint script is a dev-time tool, not part of the conductor's runtime).

---

## Phase 6 — Reference doc + CI lint (orchestrator only)

### Task 59: Add the `lint` optional-dependency group to `pyproject.toml`

**Files:**
- Modify: `~/projects/dsar-orchestrator/pyproject.toml`

This is preparatory — the lint script in Task 60 needs `markdown-it-py` and `PyYAML` at runtime. We add them as a separate optional-dep group (not under `dev`) so contributors don't have to install the full test stack to run the lint locally.

- [ ] **Step 1: Read the current `pyproject.toml`**

```bash
cd ~/projects/dsar-orchestrator
cat pyproject.toml
```

Confirm the `[project.optional-dependencies]` block currently has `toolkit` and `dev` groups only.

- [ ] **Step 2: Add the `lint` group**

Edit `pyproject.toml` `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
toolkit = [
    "dsar-pipeline >= 0.2.0",
]
dev = [
    "pytest >= 8.0",
    "pytest-cov >= 5.0",
    "import-linter >= 2.0",
    "ruff >= 0.4",
]
lint = [
    # Used by tools/check_durant_doc.py — keep here (not under `dev`) so
    # CI can install only the lint extras without pulling in the full
    # test stack. See docs/superpowers/specs/2026-05-26-durant-pipeline-
    # hardening-design-v1.md §4.7 (B).
    "markdown-it-py >= 3.0",
    "pyyaml >= 6.0",
]
```

- [ ] **Step 3: Install the new extras and verify import**

```bash
cd ~/projects/dsar-orchestrator
uv pip install -e ".[lint]"
python -c "import markdown_it, yaml; print(markdown_it.__version__, yaml.__version__)"
```

Expected: prints versions, no `ImportError`.

- [ ] **Step 4: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add pyproject.toml
git commit -m "build(orchestrator): add [lint] optional-dep group (markdown-it-py, pyyaml)"
```

---

### Task 60: Create `docs/durant-doc-lint.yaml` rules file

**Files:**
- Create: `~/projects/dsar-orchestrator/docs/durant-doc-lint.yaml`

This is the **policy** half of the lint (the script in Task 61 is the **mechanism**). Per spec §4.7 (B), keep policy externalised so a doc-edit doesn't need a script edit.

- [ ] **Step 1: Verify the parent directory exists**

```bash
ls ~/projects/dsar-orchestrator/docs/
```

Expected: `audit_schemas durant-test.md operator-guide.md superpowers`.

- [ ] **Step 2: Write the YAML rules file**

Create `docs/durant-doc-lint.yaml`:

```yaml
# Rules for tools/check_durant_doc.py.
# Spec: docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md §4.7 (B).
#
# Three checks all operate on `body_no_code` (the doc text with fenced + indented
# code blocks masked to whitespace, newline-count preserved):
#   1. path_linter      — any path-looking token must resolve or be in the bare allowlist
#   2. stale_phrases    — phrases obsoleted by post-hardening behaviour
#   3. required_headings + required_terms — structural completeness

schema_version: 1

# Phrases that MUST NOT appear in body_no_code. Each entry reports its line number
# and ALL occurrences (re.finditer, not just first match). `reason` and
# `obsolete_since_spec` are surfaced in the failure message so reviewers know
# why the doc is failing.
stale_phrases:
  - phrase: "single LLM call. No multi-pass refinement"
    reason: "Post-§4.2 the under-disclosure recheck is a first-class toolkit stage; the durant pass is no longer a single-call design."
    obsolete_since_spec: "4.2"
  - phrase: "text[:max_text_chars]"
    reason: "Post-§4.3 truncation uses truncate_with_token_check(); blind tail-cut is no longer the implementation."
    obsolete_since_spec: "4.3"
  - phrase: "DURANT_SYSTEM_PROMPT duplicated in bypass script"
    reason: "Post-§4.1 bypass scripts consume the prompt via `dsar-prompt` CLI (or vendored zipapp); the inline-constant duplication pattern is retired."
    obsolete_since_spec: "4.1"
  - phrase: "Verbatim from dsar_pipeline/gates/gate_durant.py"
    reason: "Provenance comment is no longer the drift-detection mechanism; runtime hash verification via the CLI footer is."
    obsolete_since_spec: "4.1"
  - phrase: "default to `biographical` under uncertainty"
    # NOT stale — keep this entry commented out as a worked example of "tempting
    # to add but should NOT be flagged". Default-to-biographical is the
    # asymmetric-error-cost principle and survives §§4.1–4.6 unchanged.

# Headings that MUST be present in the doc (case-insensitive match against
# parser-extracted heading tokens; whitespace-immune). Format: substring of the
# heading inline text after stripping markdown.
required_headings:
  - "What the Durant test is"             # §1 — legal foundation, never drops
  - "The decision rule"                   # §2 — verdict taxonomy
  - "Inputs"                              # §3
  - "Truncation strategy"                 # §3.1 — NEW post-§4.3
  - "Programmatic approach"               # §4
  - "Outputs"                             # §5
  - "Toolkit canonical implementation"    # §6
  - "Bypass-script consumption"           # §7 — REPLACES "Local-broker bypass pattern"
  - "Under-disclosure recheck"            # §8
  - "Model-fitness canary"                # §9.0 — NEW post-§4.4
  - "Calibration"                         # §9.1 — renumbered
  - "Known issues"                        # §10
  - "Cross-references"                    # §11
  - "Glossary"                            # §12

# Terms that MUST appear somewhere in body_no_code. Substring match. These are
# the load-bearing new vocabulary introduced by §§4.1–4.6 — if the doc rewrite
# accidentally drops one, the lint catches it.
required_terms:
  - "canonical_seal_sha256"
  - "effective_sha256"
  - "PromptLoader"
  - "dsar-prompt"
  - "RecheckStage"
  - "GateDurantRecheck"
  - "recheck_decision.json"
  - "recheck_summary.json"
  - "synthesis_summary.json"
  - "durant_verdicts.jsonl"
  - "fitness_canary"
  - "fn_rate_ci95"
  - "effective_durant"
  - "role_context"
  - "Wilson"                              # canary uses Wilson bounds (§4.4)
  - "truncate_with_token_check"

# Bare filenames (no path component) only flagged as broken if NOT in this
# allowlist. Operator-prose like "README.md" is OK to reference bare.
bare_filename_allowlist:
  - README.md
  - CLAUDE.md
  - LICENSE
  - durant-test.md
```

- [ ] **Step 3: Validate the YAML parses**

```bash
cd ~/projects/dsar-orchestrator
python -c "import yaml; data = yaml.safe_load(open('docs/durant-doc-lint.yaml')); print(sorted(data.keys()))"
```

Expected: `['bare_filename_allowlist', 'required_headings', 'required_terms', 'schema_version', 'stale_phrases']`.

- [ ] **Step 4: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add docs/durant-doc-lint.yaml
git commit -m "docs(lint): durant-doc-lint.yaml — stale_phrases + required_* + allowlist"
```

---

### Task 61: Implement `tools/check_durant_doc.py` — script skeleton + config loader

**Files:**
- Create: `~/projects/dsar-orchestrator/tools/check_durant_doc.py`
- Create: `~/projects/dsar-orchestrator/tests/test_check_durant_doc.py`

We're building the script TDD-style across Tasks 61–64. Task 61 lands the skeleton: argparse entry, YAML config loader, exit-code-2 paths for config errors. Tasks 62, 63, 64 add the three checks one at a time, each with its own failing-test-first cycle.

- [ ] **Step 1: Verify `tools/` directory layout**

```bash
ls ~/projects/dsar-orchestrator/
```

Expected: `tools/` does NOT yet exist. Create it:

```bash
mkdir -p ~/projects/dsar-orchestrator/tools
```

- [ ] **Step 2: Write failing tests for the skeleton (exit-code-2 paths)**

Create `tests/test_check_durant_doc.py`:

```python
"""Tests for tools/check_durant_doc.py — the durant-test.md CI lint.

Spec: docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md
§4.7 (B). Exit codes: 0 = pass, 1 = lint failure, 2 = config error.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_durant_doc.py"
assert TOOL.exists(), f"missing {TOOL}"


def run_lint(doc_path: Path, config_path: Path) -> subprocess.CompletedProcess:
    """Invoke the lint script via the current Python interpreter."""
    return subprocess.run(
        [sys.executable, str(TOOL),
         "--doc", str(doc_path),
         "--config", str(config_path)],
        capture_output=True, text=True,
    )


def _minimal_valid_config() -> str:
    """Smallest valid YAML config (no rules → trivial pass)."""
    return (
        "schema_version: 1\n"
        "stale_phrases: []\n"
        "required_headings: []\n"
        "required_terms: []\n"
        "bare_filename_allowlist: []\n"
    )


def test_missing_doc_exits_2(tmp_path):
    """--doc path that doesn't exist → exit 2 (config-error band)."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_minimal_valid_config())
    result = run_lint(tmp_path / "nonexistent.md", cfg)
    assert result.returncode == 2, result.stderr
    assert "not found" in (result.stderr + result.stdout).lower()


def test_missing_config_exits_2(tmp_path):
    """--config path that doesn't exist → exit 2."""
    doc = tmp_path / "doc.md"
    doc.write_text("# H1\n\nbody\n")
    result = run_lint(doc, tmp_path / "nonexistent.yaml")
    assert result.returncode == 2, result.stderr


def test_malformed_yaml_config_exits_2(tmp_path):
    """Malformed YAML → exit 2 (not 1)."""
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("schema_version: 1\nstale_phrases: [unterminated\n")
    doc = tmp_path / "doc.md"
    doc.write_text("# H1\n")
    result = run_lint(doc, cfg)
    assert result.returncode == 2, result.stderr


def test_empty_doc_passes_with_no_required_rules(tmp_path):
    """Smallest valid doc + no rules → exit 0."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(_minimal_valid_config())
    doc = tmp_path / "empty.md"
    doc.write_text("# Title\n\nBody.\n")
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout
```

- [ ] **Step 3: Run; verify failures (script doesn't exist yet)**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v 2>&1 | tail -20
```

Expected: `AssertionError: missing .../tools/check_durant_doc.py` (the module-level assert).

- [ ] **Step 4: Implement the skeleton**

Create `tools/check_durant_doc.py`:

```python
#!/usr/bin/env python3
"""docs/durant-test.md CI lint.

Spec: docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md §4.7 (B).

Exit codes:
    0 — all checks pass
    1 — one or more lint failures
    2 — config error (missing file, malformed YAML, unreadable doc)

Usage:
    python tools/check_durant_doc.py --doc docs/durant-test.md --config docs/durant-doc-lint.yaml

The script parses the doc with markdown-it-py, masks code regions (fenced +
indented + inline) while preserving newline count, then runs three checks
against `body_no_code`:
    1. path_linter      — every path-looking token must resolve
    2. stale_phrases    — listed phrases must NOT appear
    3. required_headings + required_terms — must appear
"""
from __future__ import annotations

import argparse
import bisect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from markdown_it import MarkdownIt


# ---------------------------------------------------------------------------
# Exit-code helpers
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_LINT_FAILURE = 1
EXIT_CONFIG_ERROR = 2


class ConfigError(Exception):
    """Raised for missing files, malformed YAML, schema problems.
    Maps to exit code 2."""


@dataclass
class LintFinding:
    """One lint failure. `line` is 1-indexed line number into the original doc."""

    kind: str          # "stale_phrase" | "missing_heading" | "missing_term" | "broken_path"
    message: str
    line: int | None = None


@dataclass
class LintResult:
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> dict[str, Any]:
    """Read + validate the lint YAML config.

    Raises ConfigError for missing file, malformed YAML, schema mismatch."""
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {config_path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top-level must be a mapping")
    if raw.get("schema_version") != 1:
        raise ConfigError(
            f"{config_path}: schema_version must be 1; got "
            f"{raw.get('schema_version')!r}")
    # Provide defaults for any absent rule list; downstream code can iterate
    # safely whether the operator left a section out or not.
    return {
        "stale_phrases": raw.get("stale_phrases", []) or [],
        "required_headings": raw.get("required_headings", []) or [],
        "required_terms": raw.get("required_terms", []) or [],
        "bare_filename_allowlist": set(raw.get("bare_filename_allowlist", []) or []),
    }


def load_doc(doc_path: Path) -> str:
    """Read the doc as UTF-8 text. Raises ConfigError on missing/unreadable."""
    if not doc_path.is_file():
        raise ConfigError(f"doc not found: {doc_path}")
    try:
        return doc_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {doc_path}: {e}") from e


# ---------------------------------------------------------------------------
# Stubs — full implementations in Tasks 62–64
# ---------------------------------------------------------------------------

def parse_and_mask(text: str) -> tuple[str, list[dict], list[int]]:
    """Parse `text` with markdown-it-py.

    Returns (body_no_code, headings, line_starts). Implementation lands in Task 62.
    """
    raise NotImplementedError("Task 62")


def check_stale_phrases(body_no_code: str, rules: list[dict],
                        line_starts: list[int]) -> list[LintFinding]:
    raise NotImplementedError("Task 63")


def check_required_headings(headings: list[dict],
                            required: list[str]) -> list[LintFinding]:
    raise NotImplementedError("Task 63")


def check_required_terms(body_no_code: str,
                         required: list[str]) -> list[LintFinding]:
    raise NotImplementedError("Task 63")


def check_paths(body_no_code: str, doc_path: Path,
                bare_allowlist: set[str],
                line_starts: list[int]) -> list[LintFinding]:
    raise NotImplementedError("Task 64")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_lint(doc_path: Path, config_path: Path) -> LintResult:
    """Top-level orchestrator. Caller maps LintResult → exit code."""
    config = load_config(config_path)
    text = load_doc(doc_path)
    body_no_code, headings, line_starts = parse_and_mask(text)
    result = LintResult()
    result.findings.extend(check_stale_phrases(
        body_no_code, config["stale_phrases"], line_starts))
    result.findings.extend(check_required_headings(
        headings, config["required_headings"]))
    result.findings.extend(check_required_terms(
        body_no_code, config["required_terms"]))
    result.findings.extend(check_paths(
        body_no_code, doc_path, config["bare_filename_allowlist"], line_starts))
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="check_durant_doc",
        description="Lint docs/durant-test.md for staleness + structural completeness.")
    p.add_argument("--doc", type=Path, required=True,
                   help="Path to the doc to lint (typically docs/durant-test.md).")
    p.add_argument("--config", type=Path, required=True,
                   help="Path to the lint rules YAML.")
    args = p.parse_args(argv)
    try:
        result = run_lint(args.doc, args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if result.ok:
        print(f"OK: {args.doc} passes all checks.")
        return EXIT_OK
    print(f"LINT FAILURE: {args.doc}", file=sys.stderr)
    for f in result.findings:
        loc = f"line {f.line}: " if f.line is not None else ""
        print(f"  [{f.kind}] {loc}{f.message}", file=sys.stderr)
    return EXIT_LINT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
```

Make the script executable:

```bash
chmod +x ~/projects/dsar-orchestrator/tools/check_durant_doc.py
```

- [ ] **Step 5: Re-run the 4 skeleton tests; expect partial pass**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v 2>&1 | tail -20
```

Expected:
- `test_missing_doc_exits_2` — PASS (ConfigError caught early).
- `test_missing_config_exits_2` — PASS.
- `test_malformed_yaml_config_exits_2` — PASS.
- `test_empty_doc_passes_with_no_required_rules` — FAIL with `NotImplementedError: Task 62` (because `parse_and_mask` is a stub). This is the cycle's red — Task 62 turns it green.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add tools/check_durant_doc.py tests/test_check_durant_doc.py
git commit -m "feat(lint): check_durant_doc.py skeleton + config loader (exit code 2)"
```

---

### Task 62: Implement `parse_and_mask()` — markdown-it-py code-region masking

**Files:**
- Modify: `~/projects/dsar-orchestrator/tools/check_durant_doc.py`
- Modify: `~/projects/dsar-orchestrator/tests/test_check_durant_doc.py`

This is the load-bearing core of the lint: parse the doc once with `markdown-it-py`, walk tokens to (a) locate fenced + indented code-block source spans and (b) collect headings with their map (line range). Mask code-region characters with spaces, preserving `\n` so line numbers stay aligned with the original. Compute `line_starts` (offset-of-each-line-start) once for `bisect`-based O(log n) offset→line conversion.

- [ ] **Step 1: Write failing tests for masking + line-number accuracy**

Append to `tests/test_check_durant_doc.py`:

```python
# ---------------------------------------------------------------------------
# parse_and_mask coverage — Task 62
# ---------------------------------------------------------------------------

# Direct import for unit-testing the internal function. Cleaner than always
# going through subprocess.
import importlib.util


def _import_tool_module():
    spec = importlib.util.spec_from_file_location("check_durant_doc", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_and_mask_fenced_block_masked_to_spaces():
    """Content inside ``` … ``` becomes spaces; newline count preserved."""
    mod = _import_tool_module()
    text = (
        "# Title\n"
        "\n"
        "Some prose with `inline code` here.\n"
        "\n"
        "```python\n"
        "STALE_PHRASE = 'should not trip lint'\n"
        "```\n"
        "\n"
        "More prose.\n"
    )
    body_no_code, headings, line_starts = mod.parse_and_mask(text)
    # Original total newline count preserved.
    assert body_no_code.count("\n") == text.count("\n"), \
        "newline count must be preserved for line-number accuracy"
    # The string "STALE_PHRASE" appeared only inside the fence → must be gone.
    assert "STALE_PHRASE" not in body_no_code
    # Prose outside fences is intact.
    assert "Some prose" in body_no_code
    assert "More prose." in body_no_code


def test_parse_and_mask_line_numbers_after_code_block():
    """A token on line N before the code block, and on line M after, must
    map to the SAME N and M in body_no_code (offsets shift only WITHIN
    the fence)."""
    mod = _import_tool_module()
    text = (
        "Line 1 prose.\n"        # line 1
        "\n"                       # line 2
        "```\n"                   # line 3 — open fence
        "fenced line A\n"         # line 4
        "fenced line B\n"         # line 5
        "```\n"                   # line 6 — close fence
        "\n"                       # line 7
        "Target token here.\n"    # line 8
    )
    body_no_code, _, line_starts = mod.parse_and_mask(text)
    idx = body_no_code.index("Target token")
    # Convert offset to 1-indexed line via bisect.
    import bisect
    line = bisect.bisect_right(line_starts, idx)
    assert line == 8, f"expected target on line 8, got {line}"


def test_parse_and_mask_indented_code_block_masked():
    """4-space-indented code blocks (CommonMark spec) are also masked."""
    mod = _import_tool_module()
    text = (
        "Prose.\n"
        "\n"
        "    STALE_INDENTED = 'still a code block'\n"
        "    more_indented = 42\n"
        "\n"
        "More prose.\n"
    )
    body_no_code, _, _ = mod.parse_and_mask(text)
    assert "STALE_INDENTED" not in body_no_code
    assert "Prose." in body_no_code
    assert "More prose." in body_no_code


def test_parse_and_mask_inline_code_masked():
    """Inline `code` spans are masked too — otherwise a stale_phrase
    quoted inline (e.g. as an example of what NOT to write) would trip
    the lint falsely."""
    mod = _import_tool_module()
    text = "Use `truncate_with_token_check()` not `text[:max]` in the gate.\n"
    body_no_code, _, _ = mod.parse_and_mask(text)
    # Inline code is masked, but surrounding text is kept.
    assert "Use " in body_no_code
    # The contents of the inline-code spans are gone:
    assert "truncate_with_token_check" not in body_no_code
    assert "text[:max]" not in body_no_code


def test_parse_and_mask_headings_extracted():
    """Headings come back as a list of dicts with `text` + `line`."""
    mod = _import_tool_module()
    text = (
        "# Top heading\n"
        "\n"
        "Body.\n"
        "\n"
        "## Second heading\n"
        "\n"
        "More.\n"
        "\n"
        "### Third  with  spaces\n"
    )
    _, headings, _ = mod.parse_and_mask(text)
    texts = [h["text"] for h in headings]
    assert "Top heading" in texts
    assert "Second heading" in texts
    # Whitespace immune (any of these is fine; we check substring later).
    assert any("Third" in t for t in texts)


def test_parse_and_mask_heading_inside_fence_ignored():
    """A `# Heading` *inside* a fenced code block must NOT be promoted to a
    real heading — that's the false-positive that `markdown-it-py` parsing
    (rather than regex) eliminates."""
    mod = _import_tool_module()
    text = (
        "# Real heading\n"
        "\n"
        "```markdown\n"
        "# This is fake heading inside a fence\n"
        "```\n"
        "\n"
        "Body.\n"
    )
    _, headings, _ = mod.parse_and_mask(text)
    texts = [h["text"] for h in headings]
    assert any("Real heading" in t for t in texts)
    assert not any("fake heading" in t for t in texts), \
        "headings inside fences must not appear in the parsed-headings list"
```

- [ ] **Step 2: Run; verify failures**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v -k "parse_and_mask" 2>&1 | tail -20
```

Expected: 6 failures, all `NotImplementedError: Task 62`.

- [ ] **Step 3: Implement `parse_and_mask`**

Replace the stub in `tools/check_durant_doc.py`:

```python
def _compute_line_starts(text: str) -> list[int]:
    """Return a sorted list of offsets at which each line starts.

    Index `i` in the list corresponds to the start offset of (1-indexed) line `i+1`.
    Used with bisect to map offset → 1-indexed line in O(log n).
    """
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _mask_span(buf: list[str], start: int, end: int) -> None:
    """Replace buf[start:end] with whitespace, preserving newlines."""
    for i in range(start, min(end, len(buf))):
        if buf[i] != "\n":
            buf[i] = " "


def parse_and_mask(text: str) -> tuple[str, list[dict], list[int]]:
    """Parse `text` with markdown-it-py; mask all code regions to whitespace
    while preserving newline count.

    Returns:
        body_no_code: text with fenced + indented + inline code masked.
        headings: list of {"text": str, "line": int (1-indexed)} for each ATX/
                  setext heading at the *top level* of the doc (i.e. not inside
                  a fence — the parser handles that distinction natively).
        line_starts: result of _compute_line_starts(text), reused by callers
                     for bisect-based offset→line lookups.

    Newline preservation is the load-bearing invariant: stale-phrase /
    broken-path findings carry a `line` that must match the original doc.
    Replacing every non-newline char in a code region with a space achieves
    that without re-parsing.
    """
    md = MarkdownIt("commonmark")
    tokens = md.parse(text)

    # Build a per-character buffer once. Per-token spans are then masked into
    # this buffer; final result is "".join(buf).
    buf = list(text)
    line_starts = _compute_line_starts(text)
    headings: list[dict] = []

    # First pass: walk top-level tokens for block-level code + headings.
    # `token.map` is (line_start_0indexed, line_end_exclusive_0indexed).
    for tok in tokens:
        if tok.type in ("fence", "code_block") and tok.map is not None:
            start_line, end_line = tok.map
            start_off = line_starts[start_line]
            # end_line is exclusive; clamp at len(line_starts).
            if end_line < len(line_starts):
                end_off = line_starts[end_line]
            else:
                end_off = len(text)
            _mask_span(buf, start_off, end_off)

    # Second pass: inline code. markdown-it-py inline tokens (`code_inline`)
    # live under `inline` parent tokens; the parent carries `map` (line range)
    # but children carry only `content` (no character offset). We approximate
    # by locating each `code_inline` content within its parent's line span.
    # Acknowledged residual per spec §4.7 (B): inline tokens don't carry map;
    # this is the "acceptable starting point — walk parent tokens and locate
    # spans within content".
    for tok in tokens:
        if tok.type != "inline" or tok.children is None or tok.map is None:
            continue
        start_line, end_line = tok.map
        # Build the parent's substring of the buffer to search within.
        para_start_off = line_starts[start_line]
        if end_line < len(line_starts):
            para_end_off = line_starts[end_line]
        else:
            para_end_off = len(text)
        para_text = text[para_start_off:para_end_off]
        # Walk children in order; for each `code_inline`, find its content in
        # para_text starting from a moving cursor (handles multiple spans).
        cursor = 0
        for child in tok.children:
            if child.type != "code_inline":
                continue
            content = child.content
            if not content:
                continue
            # Search for `<backtick>...content...<backtick>` (single backtick;
            # extend to N backticks if needed). The CommonMark spec allows
            # any number of backticks; for simplicity match `content` itself.
            hit = para_text.find(content, cursor)
            if hit < 0:
                # Content not found verbatim — likely whitespace-collapsed by
                # the inline parser. Skip silently; the residual cost is one
                # potentially-un-masked inline span, which is acceptable for
                # the durant-test.md doc's content.
                continue
            span_start = para_start_off + hit
            span_end = span_start + len(content)
            _mask_span(buf, span_start, span_end)
            cursor = hit + len(content)

    # Third pass: collect headings. `heading_open` token carries `tag` (h1/h2/…)
    # and `map`; the following `inline` token carries `content` (the heading
    # text post-markdown-parsing).
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.map is not None and i + 1 < len(tokens):
            inline_tok = tokens[i + 1]
            if inline_tok.type == "inline":
                heading_text = (inline_tok.content or "").strip()
                # 1-indexed line.
                heading_line = tok.map[0] + 1
                headings.append({
                    "text": heading_text,
                    "level": int(tok.tag.lstrip("h") or "0"),
                    "line": heading_line,
                })

    body_no_code = "".join(buf)
    # Sanity invariant: newline count must be unchanged.
    assert body_no_code.count("\n") == text.count("\n"), \
        "BUG: parse_and_mask altered newline count"
    return body_no_code, headings, line_starts
```

- [ ] **Step 4: Run; verify pass + skeleton test green**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v 2>&1 | tail -25
```

Expected: 6 `parse_and_mask` tests PASS. `test_empty_doc_passes_with_no_required_rules` now fails differently — `NotImplementedError: Task 63` (the next stub).

- [ ] **Step 5: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add tools/check_durant_doc.py tests/test_check_durant_doc.py
git commit -m "feat(lint): parse_and_mask with markdown-it-py (newline-preserving)"
```

---

### Task 63: Implement stale-phrase + required-heading + required-term checks

**Files:**
- Modify: `~/projects/dsar-orchestrator/tools/check_durant_doc.py`
- Modify: `~/projects/dsar-orchestrator/tests/test_check_durant_doc.py`

Three checks land together because they all read from `(body_no_code, headings, line_starts)` and share no implementation surface. TDD per check.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_check_durant_doc.py`:

```python
# ---------------------------------------------------------------------------
# Stale-phrase + required checks — Task 63
# ---------------------------------------------------------------------------

def _write_rules_yaml(path: Path, **overrides) -> Path:
    """Helper: write a YAML config; override individual sections."""
    defaults = {
        "stale_phrases": [],
        "required_headings": [],
        "required_terms": [],
        "bare_filename_allowlist": [],
    }
    defaults.update(overrides)
    import yaml as _yaml
    payload = {"schema_version": 1, **defaults}
    path.write_text(_yaml.safe_dump(payload, sort_keys=False))
    return path


def test_stale_phrase_detected_with_correct_line_number(tmp_path):
    """A stale phrase on line 5 of the doc must be reported as line 5."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Title\n"           # line 1
        "\n"                   # line 2
        "Intro prose.\n"      # line 3
        "\n"                   # line 4
        "Single LLM call. No multi-pass refinement is used.\n"  # line 5
    )
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", stale_phrases=[
        {"phrase": "single LLM call. No multi-pass refinement",
         "reason": "obsoleted by §4.2"},
    ])
    result = run_lint(doc, cfg)
    assert result.returncode == 1, result.stderr + result.stdout
    assert "line 5" in result.stderr, result.stderr


def test_multiple_stale_phrase_occurrences_all_reported(tmp_path):
    """If a stale phrase appears twice, BOTH line numbers are in the output
    (re.finditer, not just .search)."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Line 1 STALE here.\n"   # line 1 — match 1
        "Line 2 ok.\n"            # line 2
        "Line 3 STALE again.\n"   # line 3 — match 2
    )
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", stale_phrases=[
        {"phrase": "STALE", "reason": "test"},
    ])
    result = run_lint(doc, cfg)
    assert result.returncode == 1
    assert "line 1" in result.stderr
    assert "line 3" in result.stderr


def test_stale_phrase_inside_fence_not_flagged(tmp_path):
    """A stale phrase that appears ONLY inside a fenced code block must NOT
    trigger the lint — that's the whole reason we mask code regions."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Intro.\n"
        "\n"
        "```python\n"
        "comment = 'single LLM call. No multi-pass refinement'\n"
        "```\n"
        "\n"
        "More prose with no stale phrase.\n"
    )
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", stale_phrases=[
        {"phrase": "single LLM call. No multi-pass refinement",
         "reason": "obsoleted"},
    ])
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout


def test_stale_phrase_inside_indented_block_not_flagged(tmp_path):
    """Same as above for 4-space-indented code blocks. False-positive guard."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Prose.\n"
        "\n"
        "    code = 'STALE'\n"
        "\n"
        "More prose.\n"
    )
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", stale_phrases=[
        {"phrase": "STALE", "reason": "test"},
    ])
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout


def test_required_heading_missing_reported(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Only heading\n\nbody.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml",
                            required_headings=["Mandatory section"])
    result = run_lint(doc, cfg)
    assert result.returncode == 1
    assert "Mandatory section" in result.stderr


def test_required_heading_case_insensitive(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Inputs\n\nbody.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml",
                            required_headings=["inputs"])
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout


def test_required_term_missing_reported(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Title\n\nNo special vocabulary here.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml",
                            required_terms=["PromptLoader"])
    result = run_lint(doc, cfg)
    assert result.returncode == 1
    assert "PromptLoader" in result.stderr


def test_required_term_inside_fence_does_not_satisfy(tmp_path):
    """A required term that only appears in masked code is NOT considered
    satisfied — prose must mention it. (This guards against operators
    putting a vocabulary check in a code-block-only example.)"""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Title\n"
        "\n"
        "```python\n"
        "from x import PromptLoader\n"
        "```\n"
    )
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml",
                            required_terms=["PromptLoader"])
    result = run_lint(doc, cfg)
    assert result.returncode == 1, result.stderr + result.stdout
```

- [ ] **Step 2: Run; verify failures**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v -k "stale or required" 2>&1 | tail -20
```

Expected: failures with `NotImplementedError: Task 63`.

- [ ] **Step 3: Implement the three checks**

Replace the stubs in `tools/check_durant_doc.py`:

```python
def _line_for_offset(line_starts: list[int], offset: int) -> int:
    """1-indexed line number for `offset`. O(log n) via bisect."""
    # bisect_right returns insertion point: for offset == line_starts[i],
    # we want line (i+1) (1-indexed). bisect_right gives the right answer.
    return bisect.bisect_right(line_starts, offset)


def check_stale_phrases(body_no_code: str, rules: list[dict],
                        line_starts: list[int]) -> list[LintFinding]:
    """Each rule is a dict with `phrase`, `reason`, optional `obsolete_since_spec`.

    Uses `re.finditer` so multiple occurrences each get their own finding (per
    spec §4.7 (B): "all occurrences"). Case-sensitive match — phrases in the
    config are the literal forms the doc must not contain.
    """
    findings: list[LintFinding] = []
    for rule in rules:
        if not isinstance(rule, dict) or "phrase" not in rule:
            # Skip silently — config-validation could be tighter but the YAML
            # schema is loose by design (forward-compatible).
            continue
        phrase = rule["phrase"]
        reason = rule.get("reason", "(no reason given)")
        obsolete = rule.get("obsolete_since_spec")
        pattern = re.compile(re.escape(phrase))
        for m in pattern.finditer(body_no_code):
            line = _line_for_offset(line_starts, m.start())
            tail = f" (obsolete since §{obsolete})" if obsolete else ""
            findings.append(LintFinding(
                kind="stale_phrase",
                line=line,
                message=f"stale phrase {phrase!r}: {reason}{tail}",
            ))
    return findings


def check_required_headings(headings: list[dict],
                            required: list[str]) -> list[LintFinding]:
    """Substring match against parser-extracted heading inline text.

    Case-insensitive. Whitespace-immune (any internal whitespace in the
    heading still matches as long as `required` text appears as a substring
    of the normalised lowercased heading).
    """
    findings: list[LintFinding] = []
    # Normalise heading texts once for case-insensitive substring search.
    heading_texts_lower = [h["text"].lower() for h in headings]
    for needle in required:
        needle_lc = needle.strip().lower()
        if not needle_lc:
            continue
        if not any(needle_lc in h for h in heading_texts_lower):
            findings.append(LintFinding(
                kind="missing_heading",
                message=f"required heading not found: {needle!r}",
            ))
    return findings


def check_required_terms(body_no_code: str,
                         required: list[str]) -> list[LintFinding]:
    """Substring match against `body_no_code`. Case-sensitive — required terms
    are typically identifiers (PromptLoader, RecheckStage, etc.) and case
    matters for grep / cross-references.
    """
    findings: list[LintFinding] = []
    for term in required:
        if not term:
            continue
        if term not in body_no_code:
            findings.append(LintFinding(
                kind="missing_term",
                message=f"required term not found in prose (body_no_code): {term!r}",
            ))
    return findings
```

- [ ] **Step 4: Run; verify pass**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v 2>&1 | tail -25
```

Expected: all `stale_*`, `required_*`, and the skeleton tests PASS. Path-check tests in Task 64 still fail with `NotImplementedError: Task 64`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add tools/check_durant_doc.py tests/test_check_durant_doc.py
git commit -m "feat(lint): stale_phrases + required_headings + required_terms checks"
```

---

### Task 64: Implement the path linter (`check_paths`)

**Files:**
- Modify: `~/projects/dsar-orchestrator/tools/check_durant_doc.py`
- Modify: `~/projects/dsar-orchestrator/tests/test_check_durant_doc.py`

The path linter scans `body_no_code` for path-looking tokens (anything matching a conservative path-regex) and verifies each resolves either (a) relative to the doc's repo root, (b) relative to the toolkit (`~/projects/dsar-toolkit/`), or (c) is in `bare_filename_allowlist`. The "scan body_no_code (not the raw doc)" is critical: paths quoted inside code blocks (e.g. "this is the OLD path") must not trip the lint.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_check_durant_doc.py`:

```python
# ---------------------------------------------------------------------------
# Path linter — Task 64
# ---------------------------------------------------------------------------

def test_path_to_existing_orchestrator_file_passes(tmp_path):
    """A path that resolves inside this repo (e.g. tools/check_durant_doc.py)
    is fine. Use a path that we know exists post-Task 61."""
    doc = tmp_path / "doc.md"
    doc.write_text("See `tools/check_durant_doc.py` for the lint.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml")
    # Run from the repo root (cwd inside the test).
    result = subprocess.run(
        [sys.executable, str(TOOL),
         "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_path_inside_fence_not_flagged(tmp_path):
    """A non-existent path quoted inside a fence is fine — operators often
    show 'do NOT write this' examples in fences."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Prose.\n"
        "\n"
        "```\n"
        "src/does_not_exist/foo.py\n"
        "```\n"
    )
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml")
    result = subprocess.run(
        [sys.executable, str(TOOL),
         "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_bare_filename_only_checked_against_allowlist(tmp_path):
    """A bare filename like README.md is fine IF in the allowlist; a bare
    filename NOT in the allowlist is ignored (not a broken path — prose
    naturally references doc names without a path)."""
    doc = tmp_path / "doc.md"
    doc.write_text("See README.md for setup.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml",
                            bare_filename_allowlist=["README.md"])
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout


def test_path_resolved_against_toolkit_root(tmp_path):
    """A `dsar-toolkit/...` path resolves against ~/projects/dsar-toolkit/
    if installed locally; otherwise treated as informational (warning, not fail).
    The current check is lenient: any path-looking token that resolves to a
    real file passes; otherwise the linter emits a finding only if the path
    LOOKS local-orchestrator-relative (starts with src/, tools/, docs/, tests/,
    bin/, .github/).
    """
    doc = tmp_path / "doc.md"
    # docs/durant-test.md must exist in the real orchestrator repo.
    doc.write_text("Reference: docs/durant-test.md.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml")
    result = subprocess.run(
        [sys.executable, str(TOOL),
         "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_path_orchestrator_relative_nonexistent_fails(tmp_path):
    """A path that LOOKS local-orchestrator-relative (src/foo/bar.py) but
    doesn't resolve → broken_path finding."""
    doc = tmp_path / "doc.md"
    doc.write_text("Broken ref: src/dsar_orchestrator/this_file_does_not_exist.py\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml")
    result = subprocess.run(
        [sys.executable, str(TOOL),
         "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True,
    )
    assert result.returncode == 1, result.stderr
    assert "this_file_does_not_exist" in result.stderr
```

- [ ] **Step 2: Run; verify failures**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v -k "path" 2>&1 | tail -20
```

Expected: failures with `NotImplementedError: Task 64`.

- [ ] **Step 3: Implement `check_paths`**

Replace the stub in `tools/check_durant_doc.py`:

```python
# Conservative path regex: matches path-looking tokens with at least one /.
# Examples that match: src/foo/bar.py, docs/durant-test.md, tools/x.py.
# Tokens without a / (e.g. bare README.md) handled by a separate match.
_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])"             # left boundary: no alphanumeric/underscore/slash
    r"(?P<path>"
    r"(?:[A-Za-z_][A-Za-z0-9_.-]*)"   # first segment
    r"(?:/[A-Za-z_][A-Za-z0-9_.-]*)+" # at least one slash + segment
    r"\.(?P<ext>py|md|json|yaml|yml|toml|jsonl|sh|txt|gz)"
    r")"
    r"(?![A-Za-z0-9_/])"              # right boundary
)

_BARE_FILENAME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]*\.(?:md|py|json|yaml|yml|toml))"
    r"(?![A-Za-z0-9_/])"
)

# Prefixes that suggest "this path is meant to resolve inside the orchestrator
# repo". Used to decide whether a non-resolving path is a finding (yes) or a
# rhetorical reference (no — informational).
_ORCHESTRATOR_PREFIXES = (
    "src/", "tools/", "docs/", "tests/", "bin/", ".github/",
)

# Prefixes that suggest "this is the toolkit". Resolved against
# ~/projects/dsar-toolkit/ if present; otherwise treated as informational
# (the orchestrator CI environment may not have the toolkit cloned).
_TOOLKIT_PREFIXES = (
    "dsar-toolkit/", "src/dsar_pipeline/",
)


def _resolve_repo_root(doc_path: Path) -> Path:
    """Walk up from the doc until we find pyproject.toml; that's the repo root.
    Falls back to doc_path.parent if no marker found."""
    candidate = doc_path.resolve().parent
    for parent in [candidate, *candidate.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return candidate


def check_paths(body_no_code: str, doc_path: Path,
                bare_allowlist: set[str],
                line_starts: list[int]) -> list[LintFinding]:
    """Path linter.

    Behaviour:
      - Tokens matching `_PATH_PATTERN` (path-with-slash + known extension):
          • If they resolve against the repo root → OK.
          • If they resolve against the toolkit root → OK (informational).
          • If they LOOK orchestrator-relative (start with src/, tools/, etc.)
            and don't resolve → broken_path finding.
          • Otherwise → silently ignored (rhetorical / external reference).
      - Tokens matching `_BARE_FILENAME_PATTERN` (no slash): flagged ONLY if
        in `bare_allowlist` but file doesn't exist at repo root. Operators
        often reference doc names ("see CLAUDE.md") and we don't want
        false-positives on every bare token. Allowlist is the explicit
        "I expect this to resolve" signal.
    """
    findings: list[LintFinding] = []
    repo_root = _resolve_repo_root(doc_path)
    toolkit_root = Path.home() / "projects" / "dsar-toolkit"

    seen: set[tuple[str, int]] = set()        # de-dup (path, line) pairs
    for m in _PATH_PATTERN.finditer(body_no_code):
        path = m.group("path")
        line = _line_for_offset(line_starts, m.start())
        if (path, line) in seen:
            continue
        seen.add((path, line))

        # Try repo-root resolution first.
        if (repo_root / path).exists():
            continue
        # Then toolkit-root for known prefixes.
        if any(path.startswith(pfx) for pfx in _TOOLKIT_PREFIXES):
            if (toolkit_root / path).exists():
                continue
            # Toolkit-looking path that doesn't resolve — treat as informational,
            # not a failure, since the toolkit may not be cloned in the CI env.
            continue
        # If it looks orchestrator-relative, it MUST resolve.
        if any(path.startswith(pfx) for pfx in _ORCHESTRATOR_PREFIXES):
            findings.append(LintFinding(
                kind="broken_path",
                line=line,
                message=f"path looks orchestrator-relative but does not resolve: {path!r}",
            ))
            continue
        # Otherwise: rhetorical / external. Silently OK.

    # Bare-filename allowlist check: an allowlisted bare filename that
    # references a file the repo no longer has is a finding.
    seen_bare: set[tuple[str, int]] = set()
    for m in _BARE_FILENAME_PATTERN.finditer(body_no_code):
        name = m.group("name")
        line = _line_for_offset(line_starts, m.start())
        if (name, line) in seen_bare:
            continue
        seen_bare.add((name, line))
        if name not in bare_allowlist:
            continue
        # In allowlist → must resolve at repo root.
        if not (repo_root / name).is_file():
            findings.append(LintFinding(
                kind="broken_path",
                line=line,
                message=f"allowlisted bare filename does not exist at repo root: {name!r}",
            ))

    return findings
```

- [ ] **Step 4: Run; verify pass**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v 2>&1 | tail -30
```

Expected: ALL tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add tools/check_durant_doc.py tests/test_check_durant_doc.py
git commit -m "feat(lint): path linter with orchestrator-prefix heuristic"
```

---

### Task 65: Rewrite `docs/durant-test.md` per spec §4.7 (A)

**Files:**
- Modify: `~/projects/dsar-orchestrator/docs/durant-test.md`

This is the load-bearing doc update — the lint script's lint targets are now real, and the doc must reflect the post-hardening pipeline. The edits are organised into sub-steps (one per `durant-test.md` section), each with a concrete diff. Apply them in order so partial states never produce a doc the lint can't run against.

The full sequence is **one task with many sub-steps but a single final commit** — the doc edit is conceptually atomic ("describe the post-hardening pipeline"). Splitting it into per-section commits would leave the lint script in a long-running failure state (red CI) which defeats the point.

- [ ] **Step 1: Read the current `durant-test.md`**

```bash
cd ~/projects/dsar-orchestrator
wc -l docs/durant-test.md
```

Expected: 335 lines (pre-Phase 6).

- [ ] **Step 2: §3 Inputs — add `role` / `role_context` row + truncation-mode note**

Apply this diff to `docs/durant-test.md` §3:

```diff
 ## 3. Inputs

 Each Durant test invocation consumes:

 | Input | Source | Shape |
 |---|---|---|
-| Document text | `working/<ref>.txt` (extracted by ingest_v3) | UTF-8 string, truncated to 6,000–8,000 chars |
+| Document text | `working/<ref>.txt` (extracted by ingest_v3) | UTF-8 string, truncated per §3.1 (model-aware cap; defaults 8,000 chars for local MLX, 32,000 for cloud Opus) |
 | Data subject identity | `working/data_subject.json` | `full_name`, `aliases[]`, `email`, `additional_emails[]` |
+| Data subject role | `working/data_subject.json` (optional) | `role` (≤100 chars), `role_context` (≤500 chars) — domain visibility hint for the LLM. Sanitised on read (NFKC + invisible-char strip + anti-confusion filter). |
 | Doc ref | `working/register.json` entry | unique ref string (case-specific scheme) |
```

Also update the prose paragraph immediately after the table:

```diff
 The data subject summary is one line built from `data_subject.json`,
 e.g.:

 ```
 name='<full_name>'; aliases=[<aliases>];
 primary_email='<email>'; additional_emails=[<additional>]
 ```

+When `role` is set, the USER prompt also includes a "Subject's
+organisational role" section + a "How to apply the role" guidance
+block. The role context gives the LLM domain visibility but does NOT
+automatically make role-domain documents biographical — see §4 for
+the precise prompt template.
+
 The LLM never sees the operator-curated `subject_protected_phrases` for
 the Durant test itself — those phrases gate **redaction**, not scope.
```

- [ ] **Step 3: §3.1 (NEW) Truncation strategy**

Insert this entire section between §3 and §4 in `docs/durant-test.md`:

```markdown
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
  "truncation_mode": "head_tail" | "structure_aware_email_2msg" | "none",
  "original_char_count": 27432,
  "truncated_char_count": 7943,
  "subject_mentions_in_elided": 12,
  "token_safety_iterations": 0
}
```

The previous implementation (`text[:max_text_chars]`) blindly dropped
the tail and silently lost subject mentions there. The current
strategy retains both ends and surfaces lost subject signal to the
operator.
```

- [ ] **Step 4: §4 Programmatic approach — update diagram + prose**

Apply this diff to §4:

```diff
 ## 4. Programmatic approach

 ```
                                   ┌─────────────────────────┐
                                   │  working/<ref>.txt      │
                                   │  (extracted document)   │
                                   └──────────┬──────────────┘
                                              │
+                                              ▼
+┌─────────────────────────────────────────────────────────┐
+│ truncate_with_token_check(text, …)  (see §3.1)          │
+│  - head_tail (default) or structure_aware (opt-in)      │
+│  - token-aware safety belt if tokenizer available       │
+│  - audit-only subject-mention scan over elided range    │
+└──────────┬──────────────────────────────────────────────┘
                                              │
                                              ▼
 ┌──────────────────────┐         ┌─────────────────────────┐
 │ data_subject.json    │────────►│ build_user_prompt()     │
-│ (full_name, aliases, │         │  - subject summary      │
+│ (full_name, aliases, │         │  - subject summary      │
+│  email, role,        │         │  - role + how-to-apply  │
+│  role_context, …)    │         │    block if role set    │
-│  email, ...)         │         │  - doc ref              │
+│                      │         │  - doc ref              │
                                  │  - truncated text       │
                                  └──────────┬──────────────┘
                                             │
                                             ▼
+┌─────────────────────────────────────────────────────────┐
+│ PromptLoader.load("durant.system")                       │
+│  - reads gates/prompts/durant.system.md                  │
+│  - verifies canonical_seal_sha256                        │
+│  - applies any droppable strips (e.g. placeholder-tokens │
+│    when the deployment skips de-identification)          │
+│  - returns body + effective_sha256                       │
+└──────────┬──────────────────────────────────────────────┘
+                                             │
+                                             ▼
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
-│ Append row to working/durant_verdicts.jsonl              │
+│ Append row to working/durant_verdicts.jsonl              │
 │ {case_id, doc_ref, durant_verdict, rationale,            │
-│  model, prompt_version, elapsed_sec,                     │
+│  model, prompt_id, prompt_canonical_seal_sha256,         │
+│  prompt_applied_strips, prompt_effective_sha256,         │
+│  truncation_mode, original_char_count,                   │
+│  truncated_char_count, subject_mentions_in_elided,       │
+│  token_safety_iterations, elapsed_sec,                   │
 │  error_state? (model_unreachable | schema_validation     │
 │                _failed | empty_response)}                │
 └──────────────────────┬───────────────────────────────────┘
+                       │
+                       ▼  (only for primary verdict == work_context_only)
+┌──────────────────────────────────────────────────────────┐
+│ RecheckStage (toolkit) — calibration-gated by default    │
+│  Runs GateDurantRecheck with PromptLoader.load(          │
+│   "durant.recheck.system") if mode_effective="always".   │
+│  Writes working/recheck_decision.json (canonical "stage  │
+│  ran" marker) + working/durant_underdisclosure_recheck   │
+│  .jsonl + working/recheck_summary.json (cost telemetry). │
+└──────────────────────┬───────────────────────────────────┘
+                       │
+                       ▼
+┌──────────────────────────────────────────────────────────┐
+│ Agent22ScopeCheck.synthesise_verdict(...)                │
+│   Reads durant_verdicts.jsonl + recheck JSONL +          │
+│   recheck_decision.json + temporal verdicts. Computes    │
+│   `effective_durant` and synthesises `scope_verdict`     │
+│   per §4.6 of the hardening spec. Writes per-ref         │
+│   scope_verdicts.jsonl + working/synthesis_summary.json. │
+└──────────────────────────────────────────────────────────┘
 ```

 Key properties of the runtime:

-- **Per-doc, single LLM call.** No multi-pass refinement, no chain-of-thought
-  extraction. The model returns one JSON object.
+- **Two-pass design with calibration gate.** Every doc gets a primary Durant
+  call. Refs that the primary classified `work_context_only` are *conditionally*
+  re-examined by `GateDurantRecheck` (the under-disclosure safety net, §8).
+  The recheck SKIPS only when the operator's calibration cache is 95% confident
+  the false-negative rate is below the configured `fn_threshold`. Default is
+  "run the recheck" because under-disclosure is the worse error.
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
+- **Prompt integrity.** The system prompt is loaded via `PromptLoader`,
+  which verifies the asset's `canonical_seal_sha256` matches the body+metadata
+  on every load. A tampered or out-of-sync asset raises `PromptIntegrityError`
+  immediately — the run aborts before any LLM cost is incurred.
```

- [ ] **Step 5: §5 Outputs — extend the row example + new files list**

Apply this diff to §5:

```diff
 ## 5. Outputs

 Primary output: `working/durant_verdicts.jsonl` — one row per ingested
 document, append-only.

 ```jsonl
-{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"biographical","rationale":"…short sentence citing evidence…","model":"<alias>@<host>","prompt_version":"<version>","elapsed_sec":1.5}
-{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"work_context_only","rationale":"…","model":"<alias>@<host>","prompt_version":"<version>","elapsed_sec":1.3}
+{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"biographical","rationale":"…short sentence citing evidence…","model":"<alias>@<host>","prompt_id":"durant.system","prompt_canonical_seal_sha256":"<hex>","prompt_applied_strips":[],"prompt_effective_sha256":"<hex>","truncation_mode":"head_tail","original_char_count":27432,"truncated_char_count":7943,"subject_mentions_in_elided":3,"token_safety_iterations":0,"elapsed_sec":1.5}
+{"case_id":"<case_id>","doc_ref":"<ref>","durant_verdict":"work_context_only","rationale":"…","model":"<alias>@<host>","prompt_id":"durant.system","prompt_canonical_seal_sha256":"<hex>","prompt_applied_strips":[],"prompt_effective_sha256":"<hex>","truncation_mode":"head_tail","original_char_count":1432,"truncated_char_count":1432,"subject_mentions_in_elided":0,"token_safety_iterations":0,"elapsed_sec":1.3}
 ```

+Recheck outputs (only present when the calibration gate ran the safety net):
+
+- **`working/recheck_decision.json`** — single-object JSON; the canonical "did the
+  recheck stage run?" marker (read this rather than the JSONL because the
+  JSONL is empty when the gate skipped). Records `mode_effective`,
+  `mode_requested`, `reason` (e.g. `ci_upper_above_threshold`),
+  `calibration_entry_used`, `fn_threshold`, `decided_at`.
+- **`working/durant_underdisclosure_recheck.jsonl`** — per-ref recheck verdicts
+  (one row per recheck'd `work_context_only` ref). Each row carries
+  `recheck_verdict` (`reclassify_to_biographical` | `reclassify_to_ambiguous` |
+  `confirmed_work_context_only`) or `error_state` (mutually exclusive).
+- **`working/recheck_summary.json`** — cost + count telemetry: `docs_examined`,
+  `docs_reclassified_to_biographical`, `docs_reclassified_to_ambiguous`,
+  `docs_confirmed_wco`, `errors`, `estimated_cost_usd`, `elapsed_sec_total`.
+
 Downstream consumers:

 - **`working/scope_verdicts.jsonl`** is derived from `durant_verdicts.jsonl`
-  via a one-pass synthesis script (mapping `biographical → present`,
-  `work_context_only → not_present`, `ambiguous → ambiguous`).
+  by `Agent22ScopeCheck.synthesise_verdict` (5-arg form). Each row's
+  `evidence` block carries `durant_verdict`, `recheck_verdict`,
+  `error_state`, `recheck_mode_effective`, `effective_durant`, and
+  `temporal_verdict`. See §6 + §8 for the synthesis semantics.
+- **`working/synthesis_summary.json`** records the per-batch counts:
+  `recheck_promoted` (WCO→bio), `recheck_escalated` (WCO→amb),
+  `recheck_confirmed`, `recheck_errored`, `recheck_missing_anomaly`,
+  `primary_wco_recheck_disabled`. Operators consult this to verify
+  the safety net is doing useful work for the current
+  prompt/model/data combination.
 - **Redaction (toolkit stage 7)** filters to `scope_verdict = present` —
   PII tagging + redaction only runs on the biographical set, saving 4–6×
   LLM time vs running on the full ingested corpus.
 - **Operator review (toolkit stage 11)** consults `durant_verdicts.jsonl`
   rationale fields when the operator is deciding ambiguous cases.

 Secondary outputs:

 - `<engagement>/audit/agent-durant-progress.jsonl` — periodic progress rows
   (every 50 docs) with rate, errors, ETA estimate.
```

- [ ] **Step 6: §6 Toolkit canonical implementation — update file table**

Apply this diff to §6:

```diff
 ## 6. Toolkit canonical implementation

 Lives in the `dsar-toolkit` repo:

 | File | Purpose |
 |---|---|
-| `src/dsar_pipeline/gates/gate_durant.py` | `GateDurant(BaseGateAgent)` class. Holds `DURANT_SYSTEM_PROMPT`, runs per-ref. Defaults to `claude-opus-4-7` via the toolkit's `RoleRouter`. |
+| `src/dsar_pipeline/gates/gate_durant.py` | `GateDurant(BaseGateAgent)`. Loads system prompt via `PromptLoader.load("durant.system")` (no inline constant). Runs `truncate_with_token_check` before composing the user prompt. Defaults to `claude-opus-4-7` via the toolkit's `RoleRouter`. |
+| `src/dsar_pipeline/gates/gate_durant_recheck.py` | `GateDurantRecheck(BaseGateAgent)`. Used by `RecheckStage` for the inverse-question safety-net pass. Loads `durant.recheck.system` via the same loader. Does NOT see the primary verdict's rationale (confirmation-bias mitigation). |
+| `src/dsar_pipeline/gates/prompt_loader.py` | `PromptLoader.load(prompt_id, strip_sections=…)` + `compute_seal()` + `sign_asset()` + `build_registry()`. Backs the `dsar-prompt` CLI. |
+| `src/dsar_pipeline/gates/text_truncation.py` | `truncate()` + `truncate_with_token_check()` + `count_subject_mentions_in_elided()` helpers (see §3.1). |
+| `src/dsar_pipeline/gates/prompts/durant.system.md` | The canonical primary-pass system prompt. YAML frontmatter carries `prompt_id`, `version`, `seal_sha256`, `droppable_blocks`. The `placeholder-tokens` block is droppable for deployments that skip de-identification. |
+| `src/dsar_pipeline/gates/prompts/durant.recheck.system.md` | The recheck pass's inverse-question system prompt. Same loader semantics. |
+| `src/dsar_pipeline/gates/prompts/_registry.json` + `_archive/<id>/<version>.md.gz` | Append-only version history of every signed asset. Used by `dsar-conductor verify --check prompt-versions` to detect runs against retired prompts. |
+| `src/dsar_pipeline/recheck_stage.py` | `RecheckStage(BaseStage)`. Calibration-gated orchestration: reads `~/.dsar/calibration_registry.json`, decides `mode_effective`, writes `recheck_decision.json` unconditionally. `dsar-recheck` CLI. |
 | `src/dsar_pipeline/scope_check_stage.py` | `ScopeCheckStage(BaseStage)` orchestrates `gate_durant` + `gate_temporal_scope` over a register, writes per-ref `scope_verdicts.jsonl`. Exposes the `dsar-scope-check` CLI. Now also invokes `RecheckStage` after the primary durant pass when the case YAML configures it. |
-| `src/dsar_pipeline/agents/agent22_scope_check.py` | JSONL-contract adapter; consumes per-ref gate outputs and emits the final `scope_verdict`. Synthesis rule lives in module-level `_synthesise_verdict(durant, temporal)`. |
+| `src/dsar_pipeline/agents/agent22_scope_check.py` | JSONL-contract adapter; consumes per-ref gate outputs + recheck JSONL + `recheck_decision.json` + temporal verdicts, and emits the final `scope_verdict`. Synthesis lives in module-level `synthesise_verdict(primary, recheck, recheck_err, recheck_mode, temporal)` returning `(scope, rationale, effective_durant)`. Writes `synthesis_summary.json`. |
+| `src/dsar_pipeline/fitness_canary.py` | Implements the `dsar-fitness-canary` CLI. Runs both gates against an operator-curated corpus and writes a Wilson-bounded fitness report (§9.0). |
+| `src/dsar_pipeline/config/model_context.json` | Per-model `max_text_chars` + `target_input_tokens` caps. |
+| `src/dsar_pipeline/config/pricing.json` | Per-model in/out token pricing (USD per 1k). Feeds `recheck_summary.estimated_cost_usd`. |
+| `examples/canary_baseline/` | Toolkit-shipped baseline canary corpus (≥6 Durant-classic patterns). CI verifies its seal against a pinned value. |

 The toolkit version handles:
-- LLM model routing via Agent 18 `RoleRouter` (default `scope_check` role)
+- LLM model routing via Agent 18 `RoleRouter` (default `scope_check` role) — now also exposes `has_token_counter_for(model_alias)` + `count_tokens(model_alias, text)` for the truncation safety belt
 - Pre-LLM de-identification (`[PERSON_0]`, `[EMAIL_3]`, etc.) — the model
   sees placeholder tokens instead of raw names, so the prompt has a
   dedicated *"About placeholder tokens in this prompt"* section
+  (droppable via `strip_sections=("placeholder-tokens",)` when the deployment
+  does NOT pre-deidentify)
 - Cost tracking (`cost_estimate_usd = 0.01` per ref)
 - Tier 2 gate classification for the toolkit's gate framework

 It does NOT handle:
 - Direct routing to a local MLX broker (the model alias config is
   Anthropic-cloud oriented)
 - The on-prem "no de-identification preprocessor" path
+  — but the per-engagement script (§7) can request the droppable
+  `placeholder-tokens` strip via the `dsar-prompt` CLI to get a
+  loader-issued, hash-verified prompt suitable for that deployment.
```

- [ ] **Step 7: §7 — REPLACE entire section**

Replace the entire `## 7. Local-broker bypass pattern` section with this new section:

```markdown
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
time" pattern (with a `# Verbatim from dsar_pipeline/gates/gate_durant
.py` provenance comment) is retired. Drift is now caught at runtime by
the seal check and post-hoc by the conductor verify subcommand.
```

- [ ] **Step 8: §8 Under-disclosure recheck — update for toolkit stage + mutual-exclusion**

Apply this diff to §8:

```diff
 ## 8. Under-disclosure recheck (the safety net)

-Layered on top of the primary Durant pass: a second LLM pass that
-re-examines every `work_context_only` verdict by asking the *inverse*
-question.
+Layered on top of the primary Durant pass: a calibration-gated second
+LLM pass (`RecheckStage` + `GateDurantRecheck` in the toolkit) that
+re-examines `work_context_only` verdicts by asking the *inverse*
+question. The stage is first-class — it has its own
+`stage_label="durant_recheck"`, its own per-case YAML block, its own
+telemetry file, and its own `dsar-recheck` CLI. (It used to live only
+in per-engagement bypass scripts.)

-Output: `working/durant_underdisclosure_recheck.jsonl`
+**Calibration gating.** The recheck does not run unconditionally. Per
+the case YAML's `recheck:` block (mode `auto` | `always` | `never`),
+the stage consults `~/.dsar/calibration_registry.json` for an entry
+matching `(deployment_id, model_alias, primary_seal, recheck_seal)`
+and decides:
+
+- `mode: auto` — runs the recheck UNLESS the cache says we're 95%
+  confident the false-negative rate is below the configured
+  `fn_threshold` (default 0.10). Specifically: only skip when
+  `fn_rate_ci95[1] <= fn_threshold`. Wide CI → recheck runs.
+- `mode: always` — runs unconditionally.
+- `mode: never` — skipped. The case YAML must supply a non-blank
+  `override_reason`; `ConfigError` at stage init otherwise.
+
+The decision (including the reason — `mode_set_explicit`,
+`calibration_cache_miss`, `calibration_stale`,
+`calibration_prompt_seal_drift`, `ci_upper_above_threshold`,
+`ci_upper_below_threshold`) is recorded in
+`working/recheck_decision.json` — the canonical "stage ran" marker.
+Distributed deployments syncing the working directory should key off
+this file, not the (possibly empty) JSONL.
+
+**Outputs:**
+
+- `working/recheck_decision.json` — decision + reason + entry used
+- `working/durant_underdisclosure_recheck.jsonl` — per-ref results
+  (empty when mode_effective="never")
+- `working/recheck_summary.json` — counts + USD cost estimate from
+  `pricing.json`

 Recheck verdicts:

 | Verdict | Meaning | Downstream effect |
 |---|---|---|
 | `confirmed_work_context_only` | Original Durant was right; keep excluded | No change |
 | `reclassify_to_biographical` | Original Durant was wrong; ADD to disclosure | Pull back into the redaction set + operator review |
 | `reclassify_to_ambiguous` | Genuine uncertainty | Operator review |

+**Mutual-exclusion contract.** Every row in the recheck JSONL
+satisfies the invariant `error_state != null ↔ recheck_verdict == null`
+(schema-enforced via `oneOf` in
+`schemas/durant_recheck_row.schema.json`). An errored row carries
+`error_state.code` (one of `model_unreachable`, `schema_validation_
+failed`, `empty_response`, `timeout`, `unknown`), a `message`, and a
+sanitised `raw` field (≤200 chars, credential patterns redacted via
+`_sanitise_raw`). Errored rows are treated as `reclassify_to_ambiguous`
+downstream — under-disclosure safety requires "I'm not sure" to
+escalate, not silently confirm.
+
 Why a separate pass instead of tuning the primary prompt:

 1. **Asymmetric error costs.** Under-disclosure is the worse legal error.
    A second pass with an inverse question framing catches cases the
    primary pass dismissed too quickly.
 2. **Confirmation-bias mitigation.** The recheck deliberately does NOT
    see the original Durant rationale; it re-evaluates the document
    independently and any disagreement surfaces as a reclassification
    candidate. (The original rationale is kept in the per-row audit record
    so the chain of reasoning is reconstructible — just not in the prompt.)
-3. **Error defaults flag-rather-than-confirm.** If the recheck call
-   errors (network failure, schema validation failure), the default is
-   `reclassify_to_ambiguous` — the doc surfaces to the operator instead
-   of silently staying excluded. For an under-disclosure SAFETY check,
-   "I'm not sure" must escalate, not silently agree.
+3. **Error defaults flag-rather-than-confirm.** The mutual-exclusion
+   contract above plus Agent22's `effective_durant("recheck_errored:
+   <code>")` mapping ensure errored rechecks become ambiguous scope
+   verdicts. For an under-disclosure SAFETY check, "I'm not sure" must
+   escalate, not silently agree.

 In practice the recheck reclassifies a meaningful fraction (observed
 ~60% in one real case) of supposedly-excluded docs as candidate-biographical
 or ambiguous. That is the safety net working: the primary pass's
 false-negative rate was high enough that the operator would otherwise
 never have seen those documents.
```

- [ ] **Step 9: §9.0 (NEW) Model-fitness canary**

Insert this new section immediately before the renumbered §9.1 (the existing `## 9. Calibration`):

```markdown
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
```

- [ ] **Step 10: §9 → §9.1 renumber + clarification**

Apply this diff to the existing `## 9. Calibration` heading and the prose that follows:

```diff
-## 9. Calibration
+## 9.1 Calibration (post-hoc)
+
+§9.0's canary is the *pre-flight* fitness gate (does the deployment
+meet minimum quality before we touch a real case?). Calibration is the
+complementary *post-hoc* check on a finished real case: how did the
+verdicts compare against a stratified human review?

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
-is the empirical accuracy estimate for the deployment's data + model
-combo. If agreement is low in either direction, the right answer is to
-rerun the primary Durant pass with a refined prompt or a heavier model
-before committing to operator review on the full set.
+is the empirical accuracy estimate for the deployment's data + model
+combo. The portal also writes back to
+`~/.dsar/calibration_registry.json` (the same file §8's recheck reads),
+populating `fn_rate`, `fn_rate_ci95`, `sample_size`, `calibrated_at`,
+and the two prompt seal hashes. From the next case onwards, this entry
+gates whether the recheck runs or skips.
+
+If post-hoc agreement is low in either direction, the right answer is
+to refine the prompt (bump version + re-sign) or change the model and
+rerun §9.0 to validate the new tuple before committing to operator
+review on the full set.
```

- [ ] **Step 11: §10 Known issues — major rewrite**

Replace the existing §10 table entirely with this new version:

```diff
 ## 10. Known issues / drift risks

 | Issue | Impact | Mitigation |
 |---|---|---|
-| Toolkit `GateDurant` defaults to `claude-opus-4-7`; not routable to local broker | Toolkit canonical implementation unusable on on-prem Macs as-shipped | Local-broker bypass script. v0.5.0 task: make model routing pluggable. |
-| `DURANT_SYSTEM_PROMPT` duplicated in bypass script | Toolkit prompt refinements don't propagate | Verbatim-source comment marks provenance; drift is detectable via diff at release time. |
-| `mlx_lm.server` mid-run crashes return HTTP 500 | Sustained passes can have very high error rates after a crash | Retry-with-backoff (2 → 8 → 30 → 60 s) absorbs transient failures; resume-cleanup re-attempts errored rows on rerun. |
-| Loading other models (e.g. `code-qwen25` for code review, `chat` for gate decisions) evicts the primary model mid-pass | Sustained durant pass fails after model eviction | Operator discipline: no other broker calls during long passes; or pin models if the broker supports it. |
-| Small models produce JSON with the right keys but occasionally not the right shape | One-off bad rows | Strict allowed-values coercion; bad responses default to `ambiguous`; rationale field captures the raw model output for audit. |
-| Combined "durant + general classification" in one prompt loses Durant accuracy on small models | Single-call multi-task design is tempting but unreliable | Two-pass design — Durant alone, then general classification on the durant-included subset only. |
+The originally-documented drifts (prompt-copy duplication; tail-cut truncation
+dropping biographical signal; unconditional recheck cost; no upfront fitness
+gate; role-missing-from-data-subject; recheck-not-propagated-to-scope) were
+mitigated by §§4.1–4.6 of the durant-pipeline-hardening spec. The new residuals
+below are the documented limitations of the post-hardening design.
+
+| Issue | Impact | Mitigation |
+|---|---|---|
+| `mlx_lm.server` mid-run crashes return HTTP 500 | Sustained passes can have very high error rates after a crash | Retry-with-backoff (2 → 8 → 30 → 60 s) absorbs transient failures; resume-cleanup re-attempts errored rows on rerun. |
+| Loading other models evicts the primary model mid-pass | Sustained durant pass fails after model eviction | Operator discipline: no other broker calls during long passes; or pin models if the broker supports it. |
+| Small models produce JSON with the right keys but occasionally not the right shape | One-off bad rows | Strict allowed-values coercion; bad responses default to `ambiguous`; rationale field captures the raw model output for audit. |
+| `data_subject.role_context` sanitiser is anti-confusion, not security | Homoglyph substitution (Cyrillic ѕ vs Latin s), linguistic paraphrase of "ignore previous instructions", markdown-structural injection in `role_context` — none are caught by the regex/Unicode filters | Documented residual; threat model is accidental drift, not adversarial input. Operator-curated `role_context` is the only entry point. |
+| NFKC normalisation in the role-field sanitiser may merge visually-distinct characters (e.g. ﬁ → fi) | Rare false rejection if the merged form trips the injection-pattern regex; could also rarely change the prompt's effective length post-cap | Operator rephrases when sanitiser rejects; truncation is generous enough that small length shifts are absorbed. |
+| `_iter_jsonl_safe` (Agent22's recheck-index builder) is a streaming generator — mid-stream `OSError` yields a partial index | Local FS: vanishingly rare. Network FS: partial recheck-by-ref index could silently treat refs as "missing recheck" → `ambiguous` scope. | Acceptable under operator-trust threat model; documented for distributed deployments. The unified `_build_index_first_wins` logs OSError + falls back to empty dict, so the failure is operator-visible. |
+| Bypass-script vendored zipapp must stay in sync with toolkit's `_registry.json` | If the engagement folder ships a stale zipapp, runs use a retired prompt version | `dsar-conductor verify --check prompt-versions` exits non-zero on retired-but-registered versions (or warns without `--strict`); zipapp's `VENDOR_MANIFEST.json` records the toolkit version. |
+| `dsar-fitness-canary` baseline corpus is 30 refs minimum, which limits Wilson-bound tightness | A model that just clears `fn_rate_wilson_upper <= 0.20` on a 30-ref corpus still has substantial uncertainty in the true FN rate | Operators are encouraged to expand the per-deployment canary beyond the shipped baseline; spec §4.4 documents `n_biographical_truth` / `n_biographical_successful` separately so under-sized classes are visible. |
+| Calibration cache is per-machine only | A second operator on a different workstation pays the calibration cost again | Cross-deployment calibration sharing is explicit out-of-scope for the hardening spec (§2). Operator practice: distribute a known-good `calibration_registry.json` via the engagement's encrypted bundle. |
+| Combined "durant + general classification" in one prompt loses Durant accuracy on small models | Single-call multi-task design is tempting but unreliable | Two-pass design preserved — Durant alone, then general classification on the durant-included subset only. |
```

- [ ] **Step 12: §11 Cross-references — extend**

Apply this diff to §11:

```diff
 ## 11. Cross-references

 Toolkit (`harkers/dsar-toolkit`):

 - `src/dsar_pipeline/gates/gate_durant.py` — canonical gate implementation
+- `src/dsar_pipeline/gates/gate_durant_recheck.py` — recheck (safety-net) gate
+- `src/dsar_pipeline/gates/prompt_loader.py` — `PromptLoader.load`, `compute_seal`, `sign_asset`, `build_registry`
+- `src/dsar_pipeline/gates/text_truncation.py` — `truncate`, `truncate_with_token_check`, `count_subject_mentions_in_elided`
+- `src/dsar_pipeline/gates/prompts/durant.system.md` — primary-pass system prompt asset (signed)
+- `src/dsar_pipeline/gates/prompts/durant.recheck.system.md` — recheck system prompt asset (signed)
+- `src/dsar_pipeline/gates/prompts/_registry.json` — append-only prompt-version archive index
+- `src/dsar_pipeline/recheck_stage.py` — calibration-gated `RecheckStage`; backs `dsar-recheck` CLI
+- `src/dsar_pipeline/fitness_canary.py` — `dsar-fitness-canary` CLI
 - `src/dsar_pipeline/agents/agent22_scope_check.py` — JSONL-contract wrapper
 - `src/dsar_pipeline/scope_check_stage.py` — stage driver + CLI
+- `src/dsar_pipeline/config/model_context.json` — per-model truncation caps
+- `src/dsar_pipeline/config/pricing.json` — per-model USD/1k tokens for recheck cost telemetry
+- `examples/canary_baseline/` — toolkit-shipped baseline canary corpus

 Conductor (this repo):

 - `docs/durant-test.md` — this document
+- `docs/durant-doc-lint.yaml` — lint rules consumed by `tools/check_durant_doc.py`
+- `tools/check_durant_doc.py` — CI lint for this doc (§4.7)
+- `src/dsar_orchestrator/verify.py` — backs `dsar-conductor verify --check {prompt-versions,fitness-report}`
 - Per-engagement bypass scripts live in the engagement folder
   (`<engagement>/audit/agent-durant.py` etc.) — never in this repo, per
   the per-engagement data-isolation rule.

+Operator-machine state:
+
+- `~/.dsar/canary_sets/<deployment_id>/` — per-deployment fitness corpus
+- `~/.dsar/fitness_reports/<deployment_id>/<ts>.json` — fitness reports
+- `~/.dsar/calibration_registry.json` — post-hoc calibration cache; read by `RecheckStage`
+
```

- [ ] **Step 13: §12 Glossary — add new terms**

Apply this diff to §12:

```diff
 ## 12. Glossary

 - **`work_context_only`** — Durant's term for "subject is incidental, not the focus". Excluded from disclosure under Art 15.
 - **`biographical`** — Durant's term for "doc IS about the subject". Included in disclosure.
 - **`ambiguous`** — neither pass could decide cleanly. Escalates to operator.
 - **`subject_protected_phrases`** — operator-curated do-not-redact terms (the subject's own business identifiers). Separate from Durant scope; consulted by the redactor + verifier, NOT by the Durant gate.
 - **`scope_verdict`** — the synthesised verdict downstream agents consume. One-to-one mapping from `durant_verdict` when no temporal gate applies (no date window specified in `case_context.json`).
+- **`canonical_seal_sha256`** — SHA-256 over a prompt asset's full canonical frontmatter (sans `seal_sha256` itself) + body. Stored in the asset's frontmatter; verified on every `PromptLoader.load`. Tampering or accidental drift raises `PromptIntegrityError` immediately.
+- **`effective_sha256`** — SHA-256 over the prompt body *after* applied strips + whitespace normalisation. This is the hash recorded per-row in `durant_verdicts.jsonl` (and the recheck JSONL) so audit rows track the exact text the LLM saw.
+- **`fitness_canary`** — the §9.0 pre-flight check. Operator-curated corpus + truth labels; `dsar-fitness-canary` produces a Wilson-bounded report; `dsar-conductor run` consults a matching report and aborts the case if absent / stale / failing.
+- **`recheck_decision`** — the JSON file (`working/recheck_decision.json`) recording the recheck stage's `mode_effective` + `reason` + `calibration_entry_used`. Written *unconditionally* whether the recheck ran or skipped — operators read this to know "did the safety net engage?".
+- **`effective_durant`** — Agent22 synthesis intermediate: the per-doc Durant outcome AFTER the recheck override is applied. One of `present | not_present | ambiguous`. Carried into `scope_verdicts.jsonl`'s evidence block alongside the raw `durant_verdict` and `recheck_verdict`.
+- **`role_context`** — optional 500-char field in `data_subject.json` describing the subject's organisational responsibilities. Used to disambiguate role-domain documents (a doc *about* HR policy is biographical for an HR Director, work_context_only for an IT Admin). Sanitised on read; never injected raw into prompts.
```

- [ ] **Step 14: Run the lint script against the rewritten doc**

```bash
cd ~/projects/dsar-orchestrator
python tools/check_durant_doc.py --doc docs/durant-test.md --config docs/durant-doc-lint.yaml
```

Expected: `OK: docs/durant-test.md passes all checks.` (exit 0).

If anything fails, iterate: the lint output names the section / phrase / heading / term at fault.

- [ ] **Step 15: Run the full test suite to confirm no regression**

```bash
cd ~/projects/dsar-orchestrator
uv run pytest tests/test_check_durant_doc.py -v
```

Expected: all `test_check_durant_doc.py` tests PASS.

- [ ] **Step 16: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add docs/durant-test.md
git commit -m "docs(durant-test): sync with post-hardening pipeline (§§4.1–4.6)"
```

---

### Task 65b: Add the GitHub Actions workflow

**Files:**
- Create: `~/projects/dsar-orchestrator/.github/workflows/docs-lint.yml`

Per spec §4.7 (B): "Wired into orchestrator CI on every PR touching `docs/` or `src/dsar_orchestrator/`."

- [ ] **Step 1: Verify the parent directory exists**

```bash
ls ~/projects/dsar-orchestrator/.github/workflows/ 2>/dev/null || echo "missing"
```

If missing, create:

```bash
mkdir -p ~/projects/dsar-orchestrator/.github/workflows/
```

- [ ] **Step 2: Write the workflow**

Create `.github/workflows/docs-lint.yml`:

```yaml
# Durant reference-doc lint.
# Spec: docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md §4.7.
#
# Triggers on PRs that touch docs/ or src/dsar_orchestrator/ (the latter because
# code changes can invalidate the prose: e.g. renaming a stage means
# durant-test.md must be updated).
name: durant-doc-lint

on:
  pull_request:
    paths:
      - "docs/**"
      - "src/dsar_orchestrator/**"
      - "tools/check_durant_doc.py"
      - ".github/workflows/docs-lint.yml"
  push:
    branches: [main]
    paths:
      - "docs/**"
      - "src/dsar_orchestrator/**"
      - "tools/check_durant_doc.py"

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install lint extras
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[lint]"
      - name: Run check_durant_doc.py
        run: |
          python tools/check_durant_doc.py \
            --doc docs/durant-test.md \
            --config docs/durant-doc-lint.yaml
```

- [ ] **Step 3: Sanity check the YAML**

```bash
cd ~/projects/dsar-orchestrator
python -c "import yaml; yaml.safe_load(open('.github/workflows/docs-lint.yml'))" \
  && echo "yaml OK"
```

Expected: `yaml OK`.

- [ ] **Step 4: Commit**

```bash
cd ~/projects/dsar-orchestrator
git add .github/workflows/docs-lint.yml
git commit -m "ci(docs-lint): run check_durant_doc.py on PRs touching docs/ or src/"
```

---

## Acceptance criteria for Phase 6

Phase 6 is done when ALL of these hold:

- [ ] `tools/check_durant_doc.py` exists and exits 0 against the rewritten `docs/durant-test.md`.
- [ ] `docs/durant-doc-lint.yaml` exists with `stale_phrases`, `required_headings`, `required_terms`, `bare_filename_allowlist`.
- [ ] `docs/durant-test.md` contains all §4.7 (A) section edits: §3 role row, §3.1 truncation strategy, §4 updated diagram, §5 new audit fields + files, §6 PromptLoader / RecheckStage / GateDurantRecheck / Agent22, §7 replaced (dsar-prompt CLI + vendored zipapp), §8 calibration-gated + mutual-exclusion, §9.0 fitness canary, §9.1 renumbered calibration, §10 rewritten, §11 cross-references extended, §12 glossary extended.
- [ ] `tests/test_check_durant_doc.py` is green: skeleton exit-code-2 paths, `parse_and_mask` (fenced + indented + inline + heading-inside-fence + line-number-after-code-block), stale-phrase line-number accuracy + multiple-occurrences, false-positive-free indented code, required-heading case-insensitive, required-term not satisfied by code-only, path linter (resolves + fence-ignored + bare-allowlist + orchestrator-prefix-must-resolve).
- [ ] `markdown-it-py >= 3.0` and `pyyaml >= 6.0` are listed under `pyproject.toml`'s `lint` optional-dep group.
- [ ] `.github/workflows/docs-lint.yml` runs `python tools/check_durant_doc.py --doc docs/durant-test.md --config docs/durant-doc-lint.yaml` on PRs touching `docs/` or `src/dsar_orchestrator/`.
- [ ] The lint script catches all four required failure classes:
  - "single LLM call. No multi-pass refinement" (configured in `stale_phrases`).
  - Broken `src/dsar_orchestrator/this_file_does_not_exist.py`-style paths.
  - Missing required headings (e.g. §3.1, §9.0).
  - Missing required terms (e.g. `canonical_seal_sha256`).
- [ ] All commits are atomic (one feature per commit: Task 59 / 60 / 61 / 62 / 63 / 64 / 65 / 65b = 8 commits).
- [ ] Lint script exits with the spec-required codes: 0 / 1 / 2.

## Self-review

**Spec coverage (Phase 6 only — spec §4.7):**

| Spec subsection | Task(s) | Status |
|---|---|---|
| §4.7 (A) Incremental update process — per-spec doc touchpoints table | 65 | Implemented (Phase 6 owns ALL doc edits; see "Phasing note" above for the deliberate deviation from "no big-bang final PR") |
| §4.7 (B) CommonMark parser via markdown-it-py | 62 | ✓ |
| §4.7 (B) Mask code regions preserving newlines | 62 | ✓ (`_mask_span`) |
| §4.7 (B) Single parse of the document | 62 | ✓ (tokens walked thrice but parsed once) |
| §4.7 (B) Bisect for O(log n) line lookups | 63 | ✓ (`_line_for_offset`) |
| §4.7 (B) All three checks operate on body_no_code | 63, 64 | ✓ |
| §4.7 (B) Rules in `docs/durant-doc-lint.yaml` | 60 | ✓ |
| §4.7 (B) Distinct exit codes 0/1/2 | 61, 62, 63, 64 | ✓ |
| §4.7 (B) Wired into orchestrator CI | 65b | ✓ |
| §4.7 (B) Inline-code masking acknowledged residual | 62 | Implemented as "walk parent tokens, locate spans within content" per spec body; multiple-backtick edge case noted as residual |
| §4.7 (C) Conformance — removed paths still referenced | 64 | ✓ |
| §4.7 (C) Conformance — legacy phrase guard | 60, 63 | ✓ |
| §4.7 (C) Missing required headings (§3.1, §9.0) | 60, 63, 65 | ✓ |
| §4.7 (C) Missing required terms (canonical_seal_sha256, effective_durant) | 60, 63, 65 | ✓ |
| §4.7 (D) Merge-conflict mitigation | n/a | Side-stepped: Phase 6 owns all doc edits in one PR (see Phasing note) |

**Out of scope for Phase 6 (covered elsewhere or deliberately deferred):**
- Per-engagement script migration guide (spec §10.3) — separate doc, future work.
- Toolkit-side cookbook for adding new signed prompts — out per §4.7 SCOPE.
- Auto-update of the lint config from spec changes — manual operator update.
- Caching of `markdown-it-py` parse output for large-doc performance — spec §4.7 closing note defers to code-review-jury; not needed for the current 350-line doc.

**Placeholder scan:** None. Every Step has full code; every diff is concrete; every command has full args.

**Type consistency:**
- `LintFinding.kind` values used identically across `check_stale_phrases` / `check_required_headings` / `check_required_terms` / `check_paths` (`stale_phrase`, `missing_heading`, `missing_term`, `broken_path`) ✓
- `parse_and_mask` return tuple `(body_no_code: str, headings: list[dict], line_starts: list[int])` consistent across Tasks 62, 63, 64 ✓
- `headings` element shape `{"text": str, "level": int, "line": int}` consistent ✓
- `LintResult.findings` flat list (not grouped by kind) — callers iterate uniformly ✓
- Exit code constants `EXIT_OK=0 / EXIT_LINT_FAILURE=1 / EXIT_CONFIG_ERROR=2` referenced from `main()` only ✓

**Decisions deviating from spec (intentional):**
- **Phase 6 owns ALL `durant-test.md` edits.** Spec §4.7 (A) prescribes per-PR inline edits during Phases 1–5. We deviated for the reasons in the Phasing note (cleaner per-phase code PRs; lint + doc rewrite validate each other in a single landing). This trades the "no big-bang editorial PR" goal against simpler phase-boundary scoping and is documented as a deliberate operator choice.
- **`lint` is a separate optional-dep group**, not added to `dev`. Rationale: CI can install only the lint extras for fast doc-lint runs without pulling the full test stack. Documented in `pyproject.toml`.
- **Path linter's "orchestrator-prefix heuristic"** is more lenient than spec §4.7 (C) implies. The spec says "removed paths still referenced in doc" — we interpret that conservatively: only flag paths that look like they SHOULD resolve inside the orchestrator (`src/`, `tools/`, `docs/`, `tests/`, `bin/`, `.github/` prefixes). Toolkit-prefix paths that don't resolve are informational because the CI environment may not have the toolkit cloned. Rhetorical references with neither prefix are silently ignored. This is a tighter false-positive surface than a naive "every path must resolve" rule.
- **Bare filename behaviour is allowlist-gated.** Spec §4.7 (B) says "`bare_filename_allowlist`: bare filenames only checked when in allowlist." Our implementation: filenames in the allowlist MUST resolve at repo root (else finding); filenames NOT in the allowlist are silently ignored. This matches the spec's intent — the allowlist is the "I expect this to exist" signal, not a "don't check this" exception list.
- **Heading match is `substring lowercased`**, not exact-match. Spec §4.7 (B) says "structural match against parser-extracted headings (case-insensitive); whitespace-immune." Our reading: a `required_headings` entry like `"Truncation strategy"` matches any heading whose lowercased text contains `"truncation strategy"`. Whitespace immunity emerges naturally — the parser already collapses heading internal whitespace per CommonMark, and substring search is whitespace-tolerant if the config doesn't include leading/trailing spaces.

---

*End of Phase 6 plan. After Phase 6 lands, all seven §§4.1–4.7 deliverables of the durant-pipeline-hardening spec are implemented. Subsequent follow-up items (e.g. `biographical_refs.json` → `durant_verdicts.jsonl` migration; `scope_check.txt` migration to the loader; toolkit `run_for_case(case_path)` retiring the `scope_classify` adapter) are spec-§10.6 out-of-scope items, each warranting a separate spec.*
