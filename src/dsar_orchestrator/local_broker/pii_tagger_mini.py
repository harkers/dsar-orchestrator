"""Local-broker PII tagger for a DSAR case.

Produces ``<case-root>/working/<ref>_tags.json`` in the schema
``dsar_pipeline.detect.py`` emits, so the toolkit's
``agent06_redaction`` (via ``redact_detect._load_tags``) can consume the
output unchanged. Uses the mlx-broker's ``mini`` alias for the LLM
entity pass, augmented with a deterministic regex layer (email / UK
phone / NI number) the LLM tends to miss.

Per-doc pipeline:

  1. Load text from ``<ref>.txt``.
  2. Ask broker ``mini`` for an entity list (text, type, classification).
  3. For each entity text, find all character spans via str.find loop.
  4. Apply rules:
       - ``subject_protected_phrases`` match  → ``data_subject``, redact=False
       - subject identifier match              → ``data_subject``, redact=False
       - LLM said ``third_party``              → ``third_party``, redact=True
       - LLM said ``organisation``             → ``organisation``, redact='flag'
       - uncertain / unknown                   → ``unknown``, redact='flag'
  5. Run a regex pass for email / UK phone / NI number; default to
     ``third_party`` / redact=True unless preserved by the rules above.
  6. Dedupe by (start, end, text).
  7. Write the tag file.

CLI usage:
  dsar-pii-tagger-mini                    # cwd or $DSAR_CASE_ROOT
  dsar-pii-tagger-mini --case-root <path>

Resume-safe: skips refs whose tag file already exists. Honours the
``scope_verdicts.jsonl`` filter (Durant-canonical) if present; falls
back to ``responsiveness_decisions.jsonl::disposition=included``
otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from dsar_orchestrator.local_broker.dedupe_filter import canonical_refs

BROKER = "http://127.0.0.1:8090/v1/chat/completions"
MODEL = "mini"
PROMPT_VERSION = "pii-tagger-mini/v1"
RULESET_VERSION = "local-broker-pii-tagger/v1"
MAX_TEXT_CHARS = 6000
TIMEOUT_SECONDS = 90.0
PROGRESS_INTERVAL = 25
RETRY_DELAYS_S = (2.0, 8.0, 30.0, 60.0)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_UK_RE = re.compile(r"\b(?:\+44|0)\s?(?:\d\s?){9,10}\b")
NINO_RE = re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b")

SYSTEM_PROMPT = (
    "You identify personal-data entities in documents for a DSAR "
    "redaction pipeline. Focus on these entity types ONLY:\n"
    "  - person: individual people's names\n"
    "  - organisation: company / legal entity names\n"
    "  - address: postal addresses, building names\n"
    "  - id: account/employee/staff IDs, NHS numbers, customer numbers\n"
    "  - date: dates of birth, sensitive dates\n"
    "Do NOT emit email addresses, phone numbers, or NI numbers — those "
    "are handled by a deterministic regex pass and would duplicate work.\n\n"
    "For each entity classify it relative to the data subject in the "
    "user message:\n"
    "  - data_subject: IS the data subject or one of their own "
    "identifiers / business identifiers listed under "
    "do_not_redact_phrases.\n"
    "  - third_party: a different individual or a third party's identifier.\n"
    "  - organisation: a company / legal entity not personally identifying.\n"
    "  - uncertain: ambiguous.\n\n"
    "Return UNIQUE entities only — list each distinct text once even if "
    "it appears multiple times in the document.\n\n"
    "Respond with VALID JSON ONLY, no markdown, no prose. Schema:\n"
    '{"entities": [{"text": "<verbatim string>", '
    '"type": "<person|organisation|address|id|date>", '
    '"classification": "<data_subject|third_party|organisation|uncertain>"}]}'
)

log = logging.getLogger("pii-tagger-mini")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_case_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("DSAR_CASE_ROOT")
    if env:
        return Path(env)
    return Path.cwd()


def _strip_fences(content: str) -> str:
    s = content.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s
        if s.lower().startswith("json"):
            s = s[4:]
    return s.strip().rstrip("`").strip()


def _load_subject(case_root: Path) -> dict:
    return json.loads((case_root / "working" / "data_subject.json").read_text())


def _subject_identifier_set(ds: dict) -> set[str]:
    """Lowercased set of strings that must NEVER be redacted because
    they identify the data subject themselves. Includes full name +
    individual name tokens > 2 chars + aliases + emails."""
    ids: set[str] = set()
    if ds.get("full_name"):
        ids.add(ds["full_name"].lower())
        for tok in ds["full_name"].split():
            if len(tok) > 2:
                ids.add(tok.lower())
    for alias in ds.get("aliases", []):
        ids.add(alias.lower())
        for tok in alias.split():
            if len(tok) > 2:
                ids.add(tok.lower())
    if ds.get("email"):
        ids.add(ds["email"].lower())
    for e in ds.get("additional_emails", []):
        ids.add(e.lower())
    return ids


def _protected_phrases_set(ds: dict) -> set[str]:
    return {p.lower() for p in ds.get("subject_protected_phrases", [])}


def _included_refs(case_root: Path) -> set[str]:
    """The subset of refs that need PII tagging for redaction.

    Per the Durant biographical-focus directive: ``scope_verdicts.jsonl``
    is the canonical filter (built from durant). Tag everything where
    ``scope_verdict in {present, ambiguous}``. Falls back to
    ``responsiveness_decisions.jsonl::disposition=included`` if the
    Durant verdicts file is absent (and logs a warning).
    """
    working = case_root / "working"
    scope = working / "scope_verdicts.jsonl"
    responsiveness = working / "responsiveness_decisions.jsonl"
    if not scope.exists():
        log.warning(
            "scope_verdicts.jsonl missing — falling back to responsiveness "
            "disposition=included filter (NOT Durant-filtered)"
        )
        if not responsiveness.exists():
            return set()
        out: set[str] = set()
        with responsiveness.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("disposition") == "included" and r.get("doc_ref"):
                    out.add(r["doc_ref"])
        return out
    in_scope: set[str] = set()
    with scope.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("scope_verdict") in ("present", "ambiguous") and r.get("doc_ref"):
                in_scope.add(r["doc_ref"])
    if responsiveness.exists():
        excluded: set[str] = set()
        with responsiveness.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("disposition") == "excluded" and r.get("doc_ref"):
                    excluded.add(r["doc_ref"])
        discrepancy = in_scope & excluded
        if discrepancy:
            log.info(
                "%d ref(s) are durant=present but responsiveness=excluded; "
                "tagging anyway per Durant-canonical filter (sample: %s)",
                len(discrepancy),
                sorted(discrepancy)[:5],
            )
    return in_scope


def _load_register_by_ref(case_root: Path) -> dict[str, dict]:
    by_ref = {}
    for entry in json.loads((case_root / "working" / "register.json").read_text()):
        by_ref[entry["ref"]] = entry
    return by_ref


def _post(system: str, user: str, max_tokens: int = 1500) -> dict:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
    ).encode()
    last_exc: Exception | None = None
    for attempt_idx, delay in enumerate([0.0, *RETRY_DELAYS_S]):
        if delay:
            log.warning(
                "broker retry attempt %d after %.0fs (last error: %s)",
                attempt_idx,
                delay,
                last_exc,
            )
            time.sleep(delay)
        req = urllib.request.Request(
            BROKER, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 500:
                last_exc = exc
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def detect_via_llm(text: str, subject_summary: str) -> list[dict]:
    """One LLM call returning entities (no offsets)."""
    truncated = text[:MAX_TEXT_CHARS]
    truncation_note = (
        f"\n[truncated from {len(text)} to {MAX_TEXT_CHARS} chars]"
        if len(text) > MAX_TEXT_CHARS
        else ""
    )
    user = f"Data subject: {subject_summary}\n\nDocument content:\n{truncated}{truncation_note}"
    raw = _post(SYSTEM_PROMPT, user)
    content = (raw["choices"][0]["message"].get("content") or "").strip()
    content = _strip_fences(content)
    if not content:
        return []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return []
    ents = parsed.get("entities", [])
    return [e for e in ents if isinstance(e, dict) and e.get("text")]


def _find_all_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """All non-overlapping case-insensitive matches of needle in text."""
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    lo = 0
    needle_lower = needle.lower()
    text_lower = text.lower()
    while True:
        idx = text_lower.find(needle_lower, lo)
        if idx < 0:
            break
        spans.append((idx, idx + len(needle)))
        lo = idx + len(needle)
    return spans


def _regex_layer(text: str) -> list[dict]:
    """Deterministic email / UK phone / NI-number detection."""
    out = []
    for m in EMAIL_RE.finditer(text):
        out.append(
            {
                "text": m.group(),
                "type": "email",
                "start": m.start(),
                "end": m.end(),
                "source": "regex",
            }
        )
    for m in PHONE_UK_RE.finditer(text):
        out.append(
            {
                "text": m.group(),
                "type": "phone_uk",
                "start": m.start(),
                "end": m.end(),
                "source": "regex",
            }
        )
    for m in NINO_RE.finditer(text):
        out.append(
            {
                "text": m.group(),
                "type": "nino",
                "start": m.start(),
                "end": m.end(),
                "source": "regex",
            }
        )
    return out


def _classify_entity(
    text_value: str,
    llm_classification: str | None,
    *,
    subject_ids: set[str],
    protected_phrases: set[str],
) -> tuple[str, object]:
    """Return ``(classification, redact_flag)`` where redact_flag is
    True / False / 'flag'.

    Precedence (highest-priority first):

      1. ``subject_protected_phrases`` → data_subject, never redact
      2. subject identifier match      → data_subject, never redact
      3. LLM said ``data_subject``     → data_subject, never redact
      4. LLM said ``third_party``      → third_party, redact=True
      5. LLM said ``organisation``     → organisation, redact='flag'
      6. anything else                 → unknown, redact='flag'
    """
    tl = text_value.lower()
    for p in protected_phrases:
        if p in tl or tl in p:
            return "data_subject", False
    if tl in subject_ids or any(sid in tl for sid in subject_ids):
        return "data_subject", False
    if llm_classification == "data_subject":
        return "data_subject", False
    if llm_classification == "third_party":
        return "third_party", True
    if llm_classification == "organisation":
        return "organisation", "flag"
    return "unknown", "flag"


def build_tags_for_doc(
    *,
    ref: str,
    filename: str,
    text: str,
    subject: dict,
) -> dict:
    """Run LLM + regex, project to entity records, return the tag dict."""
    subject_ids = _subject_identifier_set(subject)
    protected_phrases = _protected_phrases_set(subject)
    subject_summary = (
        f"name='{subject['full_name']}'; "
        f"aliases={subject.get('aliases', [])}; "
        f"email='{subject.get('email', '')}'; "
        f"additional_emails={subject.get('additional_emails', [])}; "
        f"do_not_redact_phrases={subject.get('subject_protected_phrases', [])}"
    )

    llm_entities = detect_via_llm(text, subject_summary)
    llm_class_by_text: dict[str, str] = {}
    for e in llm_entities:
        if isinstance(e.get("text"), str):
            llm_class_by_text[e["text"].lower()] = str(e.get("classification", ""))

    seen: set[tuple[int, int, str]] = set()
    final_entities: list[dict] = []

    for ent in llm_entities:
        text_value = str(ent["text"]).strip()
        if not text_value or len(text_value) < 2:
            continue
        llm_type = str(ent.get("type", "other"))
        for start, end in _find_all_spans(text, text_value):
            key = (start, end, text_value)
            if key in seen:
                continue
            seen.add(key)
            classification, redact = _classify_entity(
                text_value,
                str(ent.get("classification", "")),
                subject_ids=subject_ids,
                protected_phrases=protected_phrases,
            )
            final_entities.append(
                {
                    "text": text_value,
                    "type": f"llm_{llm_type}",
                    "label": llm_type.upper(),
                    "start": start,
                    "end": end,
                    "source": "llm_mini",
                    "classification": classification,
                    "redact": redact,
                }
            )

    for ent in _regex_layer(text):
        key = (ent["start"], ent["end"], ent["text"])
        if key in seen:
            continue
        seen.add(key)
        llm_class = llm_class_by_text.get(ent["text"].lower())
        classification, redact = _classify_entity(
            ent["text"],
            llm_class,
            subject_ids=subject_ids,
            protected_phrases=protected_phrases,
        )
        # Regex-detected emails/phones/ninos default to third_party
        # redact=True unless the rules above preserved them as subject.
        if classification != "data_subject":
            classification = "third_party"
            redact = True
        ent.update({"classification": classification, "redact": redact})
        ent.setdefault("label", ent["type"].upper())
        final_entities.append(ent)

    redact_count = sum(1 for e in final_entities if e["redact"] is True)
    flag_count = sum(1 for e in final_entities if e["redact"] == "flag")

    return {
        "ref": ref,
        "filename": filename,
        "ruleset_version": RULESET_VERSION,
        "detector_versions": {"mini": MODEL, "regex": "email+phone_uk+nino"},
        "entity_count": len(final_entities),
        "redact_count": redact_count,
        "flag_count": flag_count,
        "pronoun_count": 0,
        "entities": final_entities,
        "pronouns": [],
        "cloaked_text": "",
        "scanned_at": _iso_now(),
    }


def run(case_root: Path) -> int:
    working = case_root / "working"
    audit = case_root / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    register = working / "register.json"
    responsiveness = working / "responsiveness_decisions.jsonl"
    data_subject = working / "data_subject.json"
    progress_log = audit / "pii-tagger-mini-progress.jsonl"

    if not register.exists():
        log.error("register.json missing at %s", register)
        return 1
    if not responsiveness.exists():
        log.error("responsiveness_decisions.jsonl missing at %s", responsiveness)
        return 1
    if not data_subject.exists():
        log.error("data_subject.json missing at %s", data_subject)
        return 1

    canonical = None if os.environ.get("INCLUDE_DUPLICATES") == "1" else canonical_refs(case_root)

    subject = _load_subject(case_root)
    by_ref = _load_register_by_ref(case_root)
    included = _included_refs(case_root)
    log.info("register: %d entries; included: %d", len(by_ref), len(included))

    progress = progress_log.open("a")
    processed = 0
    skipped_no_text = 0
    skipped_already_done = 0
    errors = 0
    started = time.monotonic()
    redact_sum = 0
    flag_sum = 0

    for ref in sorted(included):
        if canonical is not None and ref not in canonical:
            continue
        entry = by_ref.get(ref)
        if not entry:
            continue
        tag_path = working / f"{ref}_tags.json"
        if tag_path.exists():
            skipped_already_done += 1
            continue
        text_file = Path(entry["text_file"])
        if not text_file.exists():
            skipped_no_text += 1
            continue
        try:
            text = text_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped_no_text += 1
            continue
        try:
            tags = build_tags_for_doc(
                ref=ref, filename=entry.get("filename", ""), text=text, subject=subject
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            log.warning("network error on %s: %s", ref, exc)
            errors += 1
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("unexpected error on %s: %s: %s", ref, type(exc).__name__, exc)
            errors += 1
            continue
        with tag_path.open("w", encoding="utf-8") as f:
            json.dump(tags, f, indent=2, ensure_ascii=False)
        processed += 1
        redact_sum += tags["redact_count"]
        flag_sum += tags["flag_count"]
        if processed % PROGRESS_INTERVAL == 0:
            elapsed = time.monotonic() - started
            rate = processed / elapsed if elapsed > 0 else 0.0
            remaining_docs = len(included) - skipped_already_done - processed
            remaining_min = remaining_docs / rate / 60 if rate > 0 else float("inf")
            log.info(
                "processed %d (errors %d, skipped %d); rate %.2f/s; "
                "~%.1f min remaining; redact_sum=%d flag_sum=%d",
                processed,
                errors,
                skipped_no_text,
                rate,
                remaining_min,
                redact_sum,
                flag_sum,
            )
            progress.write(
                json.dumps(
                    {
                        "ts": _iso_now(),
                        "processed": processed,
                        "errors": errors,
                        "skipped_no_text": skipped_no_text,
                        "rate_per_sec": round(rate, 3),
                        "remaining_min": round(remaining_min, 1),
                        "redact_sum": redact_sum,
                        "flag_sum": flag_sum,
                    }
                )
                + "\n"
            )
            progress.flush()

    elapsed = time.monotonic() - started
    log.info(
        "done: processed=%d skipped_already_done=%d skipped_no_text=%d "
        "errors=%d redact_total=%d flag_total=%d elapsed=%.0fs (%.1f min)",
        processed,
        skipped_already_done,
        skipped_no_text,
        errors,
        redact_sum,
        flag_sum,
        elapsed,
        elapsed / 60,
    )
    progress.close()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(
        prog="dsar-pii-tagger-mini",
        description="Local-broker PII tagger producing toolkit-compatible <ref>_tags.json files.",
    )
    p.add_argument(
        "--case-root",
        type=Path,
        default=None,
        help="Case directory. Defaults to $DSAR_CASE_ROOT or cwd.",
    )
    args = p.parse_args()
    case_root = _resolve_case_root(args.case_root)
    return run(case_root)


if __name__ == "__main__":
    sys.exit(main())
