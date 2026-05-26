"""Tests for tools/check_durant_doc.py — the durant-test.md CI lint.

Spec: docs/superpowers/specs/2026-05-26-durant-pipeline-hardening-design-v1.md
§4.7 (B). Exit codes: 0 = pass, 1 = lint failure, 2 = config error.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_durant_doc.py"
assert TOOL.exists(), f"missing {TOOL}"


def run_lint(doc_path: Path, config_path: Path) -> subprocess.CompletedProcess:
    """Invoke the lint script via the current Python interpreter."""
    return subprocess.run(
        [sys.executable, str(TOOL), "--doc", str(doc_path), "--config", str(config_path)],
        capture_output=True,
        text=True,
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


# ---------------------------------------------------------------------------
# Skeleton — Task 61
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# parse_and_mask coverage — Task 62
# ---------------------------------------------------------------------------


def _import_tool_module():
    spec = importlib.util.spec_from_file_location("check_durant_doc", TOOL)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so dataclasses' module-lookup
    # for type-annotation resolution works (Python 3.14+).
    sys.modules["check_durant_doc"] = mod
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
    assert body_no_code.count("\n") == text.count("\n"), (
        "newline count must be preserved for line-number accuracy"
    )
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
        "Line 1 prose.\n"  # line 1
        "\n"  # line 2
        "```\n"  # line 3 — open fence
        "fenced line A\n"  # line 4
        "fenced line B\n"  # line 5
        "```\n"  # line 6 — close fence
        "\n"  # line 7
        "Target token here.\n"  # line 8
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
    text = "# Top heading\n\nBody.\n\n## Second heading\n\nMore.\n\n### Third  with  spaces\n"
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
    text = "# Real heading\n\n```markdown\n# This is fake heading inside a fence\n```\n\nBody.\n"
    _, headings, _ = mod.parse_and_mask(text)
    texts = [h["text"] for h in headings]
    assert any("Real heading" in t for t in texts)
    assert not any("fake heading" in t for t in texts), (
        "headings inside fences must not appear in the parsed-headings list"
    )


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
        "# Title\n"  # line 1
        "\n"  # line 2
        "Intro prose.\n"  # line 3
        "\n"  # line 4
        "Single LLM call. No multi-pass refinement is used.\n"  # line 5
    )
    cfg = _write_rules_yaml(
        tmp_path / "cfg.yaml",
        stale_phrases=[
            {"phrase": "Single LLM call. No multi-pass refinement", "reason": "obsoleted by §4.2"},
        ],
    )
    result = run_lint(doc, cfg)
    assert result.returncode == 1, result.stderr + result.stdout
    assert "line 5" in result.stderr, result.stderr


def test_multiple_stale_phrase_occurrences_all_reported(tmp_path):
    """If a stale phrase appears twice, BOTH line numbers are in the output
    (re.finditer, not just .search)."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Line 1 STALE here.\n"  # line 1 — match 1
        "Line 2 ok.\n"  # line 2
        "Line 3 STALE again.\n"  # line 3 — match 2
    )
    cfg = _write_rules_yaml(
        tmp_path / "cfg.yaml",
        stale_phrases=[
            {"phrase": "STALE", "reason": "test"},
        ],
    )
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
    cfg = _write_rules_yaml(
        tmp_path / "cfg.yaml",
        stale_phrases=[
            {"phrase": "single LLM call. No multi-pass refinement", "reason": "obsoleted"},
        ],
    )
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout


def test_stale_phrase_inside_indented_block_not_flagged(tmp_path):
    """Same as above for 4-space-indented code blocks. False-positive guard."""
    doc = tmp_path / "doc.md"
    doc.write_text("Prose.\n\n    code = 'STALE'\n\nMore prose.\n")
    cfg = _write_rules_yaml(
        tmp_path / "cfg.yaml",
        stale_phrases=[
            {"phrase": "STALE", "reason": "test"},
        ],
    )
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout


def test_required_heading_missing_reported(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Only heading\n\nbody.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", required_headings=["Mandatory section"])
    result = run_lint(doc, cfg)
    assert result.returncode == 1
    assert "Mandatory section" in result.stderr


def test_required_heading_case_insensitive(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Inputs\n\nbody.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", required_headings=["inputs"])
    result = run_lint(doc, cfg)
    assert result.returncode == 0, result.stderr + result.stdout


def test_required_term_missing_reported(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Title\n\nNo special vocabulary here.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", required_terms=["PromptLoader"])
    result = run_lint(doc, cfg)
    assert result.returncode == 1
    assert "PromptLoader" in result.stderr


def test_required_term_inside_fence_does_not_satisfy(tmp_path):
    """A required term that only appears in masked code is NOT considered
    satisfied — prose must mention it. (This guards against operators
    putting a vocabulary check in a code-block-only example.)"""
    doc = tmp_path / "doc.md"
    doc.write_text("# Title\n\n```python\nfrom x import PromptLoader\n```\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", required_terms=["PromptLoader"])
    result = run_lint(doc, cfg)
    assert result.returncode == 1, result.stderr + result.stdout


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
        [sys.executable, str(TOOL), "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_path_inside_fence_not_flagged(tmp_path):
    """A non-existent path quoted inside a fence is fine — operators often
    show 'do NOT write this' examples in fences."""
    doc = tmp_path / "doc.md"
    doc.write_text("Prose.\n\n```\nsrc/does_not_exist/foo.py\n```\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml")
    result = subprocess.run(
        [sys.executable, str(TOOL), "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_bare_filename_only_checked_against_allowlist(tmp_path):
    """A bare filename like README.md is fine IF in the allowlist; a bare
    filename NOT in the allowlist is ignored (not a broken path — prose
    naturally references doc names without a path)."""
    doc = tmp_path / "doc.md"
    doc.write_text("See README.md for setup.\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml", bare_filename_allowlist=["README.md"])
    # Run from the orchestrator repo root so README.md resolves.
    result = subprocess.run(
        [sys.executable, str(TOOL), "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
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
        [sys.executable, str(TOOL), "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_path_orchestrator_relative_nonexistent_fails(tmp_path):
    """A path that LOOKS local-orchestrator-relative (src/foo/bar.py) but
    doesn't resolve → broken_path finding."""
    doc = tmp_path / "doc.md"
    doc.write_text("Broken ref: src/dsar_orchestrator/this_file_does_not_exist.py\n")
    cfg = _write_rules_yaml(tmp_path / "cfg.yaml")
    result = subprocess.run(
        [sys.executable, str(TOOL), "--doc", str(doc), "--config", str(cfg)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert "this_file_does_not_exist" in result.stderr
