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

    kind: str  # "stale_phrase" | "missing_heading" | "missing_term" | "broken_path"
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
            f"{config_path}: schema_version must be 1; got {raw.get('schema_version')!r}"
        )
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
# Parsing + masking
# ---------------------------------------------------------------------------


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
                headings.append(
                    {
                        "text": heading_text,
                        "level": int(tok.tag.lstrip("h") or "0"),
                        "line": heading_line,
                    }
                )

    body_no_code = "".join(buf)
    # Sanity invariant: newline count must be unchanged.
    assert body_no_code.count("\n") == text.count("\n"), "BUG: parse_and_mask altered newline count"
    return body_no_code, headings, line_starts


# ---------------------------------------------------------------------------
# Stale-phrase + required-heading + required-term checks
# ---------------------------------------------------------------------------


def _line_for_offset(line_starts: list[int], offset: int) -> int:
    """1-indexed line number for `offset`. O(log n) via bisect."""
    # bisect_right returns insertion point: for offset == line_starts[i],
    # we want line (i+1) (1-indexed). bisect_right gives the right answer.
    return bisect.bisect_right(line_starts, offset)


def check_stale_phrases(
    body_no_code: str, rules: list[dict], line_starts: list[int]
) -> list[LintFinding]:
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
            findings.append(
                LintFinding(
                    kind="stale_phrase",
                    line=line,
                    message=f"stale phrase {phrase!r}: {reason}{tail}",
                )
            )
    return findings


def check_required_headings(headings: list[dict], required: list[str]) -> list[LintFinding]:
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
            findings.append(
                LintFinding(
                    kind="missing_heading",
                    message=f"required heading not found: {needle!r}",
                )
            )
    return findings


def check_required_terms(body_no_code: str, required: list[str]) -> list[LintFinding]:
    """Substring match against `body_no_code`. Case-sensitive — required terms
    are typically identifiers (PromptLoader, RecheckStage, etc.) and case
    matters for grep / cross-references.
    """
    findings: list[LintFinding] = []
    for term in required:
        if not term:
            continue
        if term not in body_no_code:
            findings.append(
                LintFinding(
                    kind="missing_term",
                    message=f"required term not found in prose (body_no_code): {term!r}",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Path linter
# ---------------------------------------------------------------------------

# Conservative path regex: matches path-looking tokens with at least one /.
# Examples that match: src/foo/bar.py, docs/durant-test.md, tools/x.py.
# Tokens without a / (e.g. bare README.md) handled by a separate match.
_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])"  # left boundary: no alphanumeric/underscore/slash
    r"(?P<path>"
    r"(?:[A-Za-z_][A-Za-z0-9_.-]*)"  # first segment
    r"(?:/[A-Za-z_][A-Za-z0-9_.-]*)+"  # at least one slash + segment
    r"\.(?P<ext>py|md|json|yaml|yml|toml|jsonl|sh|txt|gz)"
    r")"
    r"(?![A-Za-z0-9_/])"  # right boundary
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
    "src/",
    "tools/",
    "docs/",
    "tests/",
    "bin/",
    ".github/",
)

# Prefixes that suggest "this is the toolkit". Resolved against
# ~/projects/dsar-toolkit/ if present; otherwise treated as informational
# (the orchestrator CI environment may not have the toolkit cloned).
_TOOLKIT_PREFIXES = (
    "dsar-toolkit/",
    "src/dsar_pipeline/",
)


def _resolve_repo_root(doc_path: Path) -> Path:
    """Walk up from the doc until we find pyproject.toml; that's the repo root.
    If no marker is found via the doc path (e.g. the doc is in a tmpdir during
    testing), fall back to walking up from cwd. Otherwise return doc.parent."""
    candidate = doc_path.resolve().parent
    for parent in [candidate, *candidate.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    # Fall back: walk up from cwd. This lets `check_paths` resolve paths
    # against the orchestrator repo when the doc lives outside the repo (e.g.
    # in a tmpdir during testing) but cwd is the repo root.
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return candidate


def check_paths(
    body_no_code: str, doc_path: Path, bare_allowlist: set[str], line_starts: list[int]
) -> list[LintFinding]:
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

    seen: set[tuple[str, int]] = set()  # de-dup (path, line) pairs
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
            findings.append(
                LintFinding(
                    kind="broken_path",
                    line=line,
                    message=f"path looks orchestrator-relative but does not resolve: {path!r}",
                )
            )
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
            findings.append(
                LintFinding(
                    kind="broken_path",
                    line=line,
                    message=f"allowlisted bare filename does not exist at repo root: {name!r}",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_lint(doc_path: Path, config_path: Path) -> LintResult:
    """Top-level orchestrator. Caller maps LintResult → exit code."""
    config = load_config(config_path)
    text = load_doc(doc_path)
    body_no_code, headings, line_starts = parse_and_mask(text)
    result = LintResult()
    result.findings.extend(check_stale_phrases(body_no_code, config["stale_phrases"], line_starts))
    result.findings.extend(check_required_headings(headings, config["required_headings"]))
    result.findings.extend(check_required_terms(body_no_code, config["required_terms"]))
    result.findings.extend(
        check_paths(body_no_code, doc_path, config["bare_filename_allowlist"], line_starts)
    )
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="check_durant_doc",
        description="Lint docs/durant-test.md for staleness + structural completeness.",
    )
    p.add_argument(
        "--doc",
        type=Path,
        required=True,
        help="Path to the doc to lint (typically docs/durant-test.md).",
    )
    p.add_argument("--config", type=Path, required=True, help="Path to the lint rules YAML.")
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
