"""#109 — two-panel redaction viewer (console-side overlay projection).

Reads the existing ``<case>/working/<ref>_tags.json`` files produced by
``pii_tagger_mini`` (or the toolkit's ``detect.py``) and projects an
overlay for the operator console's redaction viewer at render time.

No new persisted artefacts; no toolkit changes. The chat's v3 jury
synthesis pinned the badge taxonomy to nine codes (DS / TP / SC / CH /
AE / LC / CC / NR / SEC). The default mapping from the toolkit's
``classification`` field to those codes lives in ``REDACTION_CODE_MAP``;
``redact == 'flag'`` overrides classification and produces ``NR`` so
the operator can spot ambiguous entities in the redacted pane.

Entities with ``redact == False`` (preserve) appear verbatim in the
redacted pane — the toolkit's actual redaction behaviour doesn't replace
them either, so the viewer must match.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path

log = logging.getLogger("redaction-viewer")

# Default classification → v3 badge code. Unmapped classifications fall
# through to "NR" (needs review) so the operator notices and triages.
REDACTION_CODE_MAP: dict[str, str] = {
    "data_subject": "DS",
    "third_party": "TP",
    "organisation": "CC",
    "special_category": "SC",
    "child": "CH",
    "adverse_event": "AE",
    "legal_counsel": "LC",
    "client_confidential": "CC",
    "needs_review": "NR",
    "security": "SEC",
}


def classify_code(entity: dict) -> str:
    """Return the v3 badge code for an entity. ``redact == 'flag'``
    overrides classification and always yields ``NR``."""
    if entity.get("redact") == "flag":
        return "NR"
    cls = entity.get("classification", "") or ""
    return REDACTION_CODE_MAP.get(cls, "NR")


def build_overlay(case_dir: Path, doc_ref: str) -> dict:
    """Read ``<case>/working/<ref>_tags.json`` and return a structured
    overlay sorted by ``start`` ascending. Missing or corrupt tag files
    return an empty overlay with ``exists=False`` rather than raising —
    the viewer route shows a friendly empty pane in that case.
    """
    tag_path = case_dir / "working" / f"{doc_ref}_tags.json"
    if not tag_path.exists():
        return {"doc_ref": doc_ref, "filename": "", "exists": False, "entities": []}
    try:
        tags = json.loads(tag_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("tags file unreadable at %s: %s", tag_path, exc)
        return {"doc_ref": doc_ref, "filename": "", "exists": False, "entities": []}

    entries: list[dict] = []
    for e in tags.get("entities", []) or []:
        if not isinstance(e, dict):
            continue
        if "start" not in e or "end" not in e:
            continue
        try:
            start = int(e["start"])
            end = int(e["end"])
        except (TypeError, ValueError):
            continue
        entries.append(
            {
                "start": start,
                "end": end,
                "text": e.get("text", ""),
                "classification": e.get("classification", ""),
                "redact": e.get("redact"),
                "code": classify_code(e),
            }
        )
    entries.sort(key=lambda x: x["start"])
    return {
        "doc_ref": doc_ref,
        "filename": tags.get("filename", "") or "",
        "exists": True,
        "entities": entries,
    }


def render_original_html(text: str) -> str:
    """Left pane: original text, html-escaped."""
    return html.escape(text)


def render_redacted_html(text: str, overlay: dict) -> str:
    """Right pane: text with ``<span data-code="…" data-start="…">[CODE]</span>``
    overlays in place of redacted-entity text. Entities with
    ``redact == False`` are rendered verbatim (matching the toolkit's
    actual redaction output, which preserves data-subject entities)."""
    out: list[str] = []
    cursor = 0
    text_len = len(text)
    for e in overlay.get("entities", []):
        s, ee = e["start"], e["end"]
        if s < cursor or s >= text_len or ee > text_len or ee <= s:
            continue
        out.append(html.escape(text[cursor:s]))
        if e.get("redact") in (True, "flag"):
            code = e["code"]
            out.append(
                f'<span data-code="{html.escape(code)}" '
                f'data-start="{s}" data-end="{ee}">[{html.escape(code)}]</span>'
            )
        else:
            out.append(html.escape(text[s:ee]))
        cursor = ee
    out.append(html.escape(text[cursor:]))
    return "".join(out)
