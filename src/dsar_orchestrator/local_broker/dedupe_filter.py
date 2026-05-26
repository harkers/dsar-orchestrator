"""Dedupe-aware filtering helper for downstream pipeline scripts.

After agent03_dedupe runs (toolkit v0.4.5+), it writes
``working/dedupe_findings.jsonl`` marking each ingested ref as either
``canonical`` (process this) or ``duplicate`` (skip — already covered by
the canonical). Downstream LLM-heavy steps (context_classify, durant,
PII tagger, redaction) save ~40% wall-clock by filtering inputs to the
canonical set before processing.

This helper centralises that filter so each script reads dedupe results
the same way. The default behaviour is **filter on** — operators get the
savings without flag-hunting. Pass `--include-duplicates` (or call the
helper with ``include_duplicates=True``) to bypass the filter when a
script needs to re-process every ref (e.g. a re-run after a dedupe bug).

Semantics:
  - Returns ``None`` when ``dedupe_findings.jsonl`` doesn't exist
    (signals "no dedupe has run; process everything").
  - Returns an empty set when the file exists but has zero canonical
    refs — for safety this is treated as "filter off, process all"
    via the ``apply_filter`` helper (because skipping every ref is
    almost always a bug, not intent).
  - Returns a non-empty set when dedupe ran and found canonicals.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("dedupe-filter")


def canonical_refs(case_dir: Path) -> set[str] | None:
    """Read ``<case_dir>/working/dedupe_findings.jsonl`` and return the
    set of refs flagged ``dedupe_verdict='canonical'``.

    Returns:
        None if the file doesn't exist (no dedupe has run).
        Empty set if file exists but has no canonical refs (suspicious;
        caller should treat as "no filter" via ``apply_filter``).
        Otherwise the set of canonical doc_refs.
    """
    path = case_dir / "working" / "dedupe_findings.jsonl"
    if not path.exists():
        return None
    canonical: set[str] = set()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("dedupe_verdict") == "canonical":
                ref = r.get("doc_ref", "")
                if ref:
                    canonical.add(ref)
    return canonical


def apply_filter(
    *,
    case_dir: Path,
    refs: list[str],
    include_duplicates: bool = False,
) -> tuple[list[str], dict]:
    """Apply the canonical-only filter to a list of refs.

    Returns ``(filtered_refs, status_dict)`` where status_dict captures
    the decision for audit / progress logging:

      {"mode": "no_filter" | "canonical_only" | "include_duplicates",
       "input_count": int, "canonical_count": int, "skipped": int,
       "warning": "..." or ""}

    Operator-facing log line via ``log.info(status['summary'])``.
    """
    status = {
        "mode": "no_filter",
        "input_count": len(refs),
        "canonical_count": len(refs),
        "skipped": 0,
        "warning": "",
        "summary": "",
    }
    if include_duplicates:
        status["mode"] = "include_duplicates"
        status["summary"] = (
            f"DEDUPE FILTER OFF (--include-duplicates): processing all {len(refs):,} refs"
        )
        return list(refs), status
    canonical = canonical_refs(case_dir)
    if canonical is None:
        status["summary"] = (
            f"DEDUPE FILTER N/A: no dedupe_findings.jsonl yet — processing all {len(refs):,} refs"
        )
        return list(refs), status
    if not canonical:
        status["warning"] = (
            "dedupe_findings.jsonl exists but has no canonical refs — "
            "treating as no-filter (suspicious; verify dedupe ran correctly)"
        )
        status["summary"] = (
            f"DEDUPE FILTER WARN: dedupe results empty — processing all {len(refs):,} refs"
        )
        return list(refs), status
    filtered = [r for r in refs if r in canonical]
    skipped = len(refs) - len(filtered)
    status["mode"] = "canonical_only"
    status["canonical_count"] = len(canonical)
    status["skipped"] = skipped
    pct = (100.0 * skipped / len(refs)) if refs else 0
    status["summary"] = (
        f"DEDUPE FILTER ON: {len(filtered):,} canonical refs to process "
        f"({skipped:,} duplicates skipped = {pct:.0f}% saved). "
        f"Total canonical in dedupe_findings.jsonl: {len(canonical):,}."
    )
    return filtered, status
