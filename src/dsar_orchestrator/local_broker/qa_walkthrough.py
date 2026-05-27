"""Operator QA walkthrough — one-doc-at-a-time approve/decline cycle.

Distinct from ``qa_sample``: that module is a 30-doc stratified sample
(10 high-risk + 10 medium + 10 random) rendered as a single table of
decisions. This walkthrough is a sequential review of N random
redacted docs (default 50), one screen per doc with side-by-side source
text and redacted text, and one-click approve / decline+feedback.

The sample is persisted to ``audit/qa_walkthrough_sample.json`` so a
browser refresh keeps the same N docs in the same order. The verdicts
flow through ``qa_sample.record_qa_decision`` for audit-chain
consistency (single source of truth for QA decisions).
"""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("qa-walkthrough")

DEFAULT_SAMPLE_SIZE = 50
_SAMPLE_FILENAME = "qa_walkthrough_sample.json"


def _sample_path(case_dir: Path) -> Path:
    return case_dir / "audit" / _SAMPLE_FILENAME


def load_redacted_text(case_dir: Path, doc_ref: str) -> tuple[str, str]:
    """Return ``(extracted_text, source_label)`` for what was actually
    redacted and is going into the disclosure pack.

    Reads the artefact in ``<case>/redacted/`` for the given ref.
    .eml — extract the text/plain body via the stdlib email parser
    (handles the toolkit's redact_msg output, which is the dominant
    format in this case at ~849 of 853 docs).
    .pdf — extract text with PyMuPDF.
    .html / .txt — read directly.
    Other formats — return an empty string with a label so the caller
    can show a fallback / "open exported PDF" link instead.

    Importantly distinct from the #109 overlay projection (which maps
    tag spans onto ``working/<ref>.txt``): the overlay shows what the
    tagger said to do, not what the actual redactor did. For QA the
    operator needs to see exactly what's going out.
    """
    red_dir = case_dir / "redacted"
    if not red_dir.exists():
        return "", "no /redacted/ dir"
    # Match by ref prefix (filenames are "<ref>_<original-filename>.<ext>")
    matches = sorted(red_dir.glob(f"{doc_ref}_*"))
    if not matches:
        return "", "no redacted artefact found"
    path = matches[0]
    suffix = path.suffix.lower()

    if suffix == ".eml":
        # The toolkit's redact_msg writes pseudo-email plain text where
        # the From/To/Cc fields contain [R1]-style placeholders, e.g.
        # `From: [R1] <[R1]>`. That's NOT RFC-2822-strict — Python's
        # `email.policy.default` parser drops the malformed addr-specs to
        # `<>`. We want the raw redacted text exactly as it'd ship out, so
        # read it verbatim.
        try:
            return path.read_text(encoding="utf-8", errors="replace"), f"redacted/{path.name}"
        except OSError as exc:
            return f"[error reading redacted .eml: {exc}]", f"redacted/{path.name}"

    if suffix == ".pdf":
        try:
            import fitz  # PyMuPDF

            d = fitz.open(path)
            pages = [p.get_text() for p in d]
            d.close()
            return "\n\n".join(pages), f"redacted/{path.name}"
        except Exception as exc:  # noqa: BLE001
            return f"[error reading redacted .pdf: {exc}]", f"redacted/{path.name}"

    if suffix in (".html", ".htm", ".txt", ".csv"):
        try:
            return path.read_text(encoding="utf-8", errors="replace"), f"redacted/{path.name}"
        except OSError as exc:
            return f"[error reading {path.name}: {exc}]", f"redacted/{path.name}"

    # docx / xlsx / pptx / other — operator should view the exported PDF.
    return "", f"redacted/{path.name} ({suffix}; open exported PDF for full view)"


def _redacted_refs(case_dir: Path) -> list[dict]:
    """Return register entries with ``status == 'redacted'`` or
    ``'exported'`` (post-export the status flips to exported but the
    underlying redacted artefact is what we're QC'ing)."""
    reg_path = case_dir / "working" / "register.json"
    if not reg_path.exists():
        return []
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("qa-walkthrough: register.json unreadable: %s", exc)
        return []
    return [e for e in reg if e.get("status") in ("redacted", "exported")]


def build_sample(
    case_dir: Path, *, size: int = DEFAULT_SAMPLE_SIZE, seed: int | None = None
) -> list[str]:
    """Pick ``size`` random doc_refs from the redacted set + persist
    them to ``audit/qa_walkthrough_sample.json``. Returns the list of
    refs in sample order.

    Idempotency: pass the same ``seed`` to reproduce the same sample.
    Calling without a seed picks a fresh one and persists it alongside
    the sample so the sample is reproducible from the on-disk record.
    """
    eligible = _redacted_refs(case_dir)
    if not eligible:
        return []
    actual_seed = random.randint(0, 2**31 - 1) if seed is None else int(seed)
    rng = random.Random(actual_seed)
    pool = list(eligible)
    rng.shuffle(pool)
    chosen = pool[: min(size, len(pool))]
    refs = [e["ref"] for e in chosen]
    sample = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "size_requested": size,
        "size_actual": len(refs),
        "seed": actual_seed,
        "eligible_pool_size": len(eligible),
        "refs": refs,
    }
    path = _sample_path(case_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sample, indent=2), encoding="utf-8")
    tmp.replace(path)
    return refs


def load_sample(case_dir: Path) -> dict | None:
    """Return the persisted sample dict, or ``None`` if not built."""
    path = _sample_path(case_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("qa-walkthrough: sample at %s unreadable: %s", path, exc)
        return None


def load_decisions(case_dir: Path) -> dict[str, dict]:
    """Return ``{doc_ref: decision_row}`` from
    ``audit/qa_decisions.jsonl``. ``record_qa_decision`` writes there;
    the walkthrough reads back to know what's still pending."""
    path = case_dir / "audit" / "qa_decisions.jsonl"
    if not path.exists():
        return {}
    by_ref: dict[str, dict] = {}
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ref = r.get("doc_ref")
                if ref:
                    by_ref[ref] = r  # last-wins for repeat decisions
    except OSError as exc:
        log.warning("qa-walkthrough: qa_decisions.jsonl unreadable: %s", exc)
    return by_ref


def progress(case_dir: Path) -> dict:
    """Return ``{total, approved, declined, pending, next_pending_idx}``."""
    sample = load_sample(case_dir)
    if not sample:
        return {
            "total": 0,
            "approved": 0,
            "declined": 0,
            "pending": 0,
            "next_pending_idx": None,
        }
    refs = sample.get("refs", [])
    decisions = load_decisions(case_dir)
    approved = 0
    declined = 0
    pending = 0
    next_pending_idx: int | None = None
    for i, ref in enumerate(refs):
        d = decisions.get(ref)
        if not d:
            pending += 1
            if next_pending_idx is None:
                next_pending_idx = i
        elif d.get("decision") == "approve":
            approved += 1
        else:
            declined += 1
    return {
        "total": len(refs),
        "approved": approved,
        "declined": declined,
        "pending": pending,
        "next_pending_idx": next_pending_idx,
    }


def ref_at(case_dir: Path, idx: int) -> str | None:
    """Return the doc_ref at sample position ``idx`` (0-based), or
    ``None`` if out of range / no sample built."""
    sample = load_sample(case_dir)
    if not sample:
        return None
    refs = sample.get("refs", [])
    if idx < 0 or idx >= len(refs):
        return None
    return refs[idx]
