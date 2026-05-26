"""Local-broker context-classification driver for a DSAR case.

Bypasses ``dsar_pipeline.context_classify.QwenContextClient`` (hardcoded
to a remote 30B endpoint, ~120s/call on local hardware). Uses the
mlx-broker's ``mini`` alias (Qwen2.5-7B-Instruct-4bit) — fast enough to
classify thousands of docs in hours rather than days.

Output shape matches what Agent05Responsiveness consumes:
  {case_id, doc_ref, durant_verdict, durant_rationale,
   primary_classification, is_about_requester, confidence,
   requester_role, evidence_snippet, recommended_action,
   rationale, model, prompt_version, error_state?}

Output lands at ``<case-root>/working/context_classifications.jsonl``.

CLI usage:
  dsar-context-classify-mini                     # cwd or $DSAR_CASE_ROOT
  dsar-context-classify-mini --case-root <path>

Resume-safe: re-running skips doc_refs already present in the output.
Honours the canonical-only dedupe filter from
``dsar_orchestrator.local_broker.dedupe_filter``;
``INCLUDE_DUPLICATES=1`` opts out.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from dsar_orchestrator.local_broker.dedupe_filter import canonical_refs

BROKER = "http://127.0.0.1:8090/v1/chat/completions"
MODEL = "mini"
PROMPT_VERSION = "context-classify-mini/v1"
MAX_TEXT_CHARS = 8000
TIMEOUT_SECONDS = 90.0
PROGRESS_INTERVAL = 50

ALLOWED_CLASSIFICATIONS = (
    "communication",
    "transactional",
    "personal_record",
    "professional_record",
    "marketing",
    "system_notice",
    "other",
)
ALLOWED_IS_ABOUT = ("yes", "no", "partial", "unclear")
ALLOWED_DURANT = ("biographical", "work_context_only", "ambiguous")

log = logging.getLogger("context-classify-mini")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_case_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("DSAR_CASE_ROOT")
    if env:
        return Path(env)
    return Path.cwd()


def _load_subject_summary(case_root: Path) -> str:
    data = json.loads((case_root / "working" / "data_subject.json").read_text())
    parts = [f"name='{data['full_name']}'"]
    aliases = data.get("aliases", [])
    if aliases:
        parts.append(f"aliases={aliases}")
    email = data.get("email")
    if email:
        parts.append(f"email='{email}'")
    addl = data.get("additional_emails", [])
    if addl:
        parts.append(f"additional_emails={addl}")
    protected = data.get("subject_protected_phrases", [])
    if protected:
        parts.append(f"do_not_redact={protected[:5]}{'...' if len(protected) > 5 else ''}")
    return "; ".join(parts)


def _load_path_to_ref(case_root: Path) -> dict[str, dict]:
    by_path = {}
    for entry in json.loads((case_root / "working" / "register.json").read_text()):
        by_path[entry["path"]] = entry
    return by_path


def _load_completed_refs(output: Path) -> set[str]:
    if not output.exists():
        return set()
    done: set[str] = set()
    with output.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("doc_ref"):
                done.add(r["doc_ref"])
    return done


def _build_user_prompt(doc_ref: str, text: str, subject_summary: str) -> str:
    truncated = text[:MAX_TEXT_CHARS]
    truncation_note = ""
    if len(text) > MAX_TEXT_CHARS:
        truncation_note = (
            f"\n[truncated from {len(text)} to {MAX_TEXT_CHARS} chars for prompt budget]"
        )
    return (
        f"=== THE DATA SUBJECT ===\n{subject_summary}\n\n"
        f"Every verdict below is RELATIVE TO THE NAMED DATA SUBJECT ABOVE. "
        f"`biographical` means the document is about the data subject "
        f"specifically — not merely that it is about some person. If the "
        f"document focuses on someone else's work or another person's "
        f"projects, that is `work_context_only` relative to this subject "
        f"EVEN IF the named subject is mentioned or appears in a recipient "
        f"list.\n\n"
        f"=== STEP 1: DURANT BIOGRAPHICAL-FOCUS TEST (UK GDPR Art 15) ===\n"
        f"Apply Durant v FSA to this document, relative to the data subject "
        f"named above. Three verdicts:\n"
        f"  - biographical: the document content is about the data subject "
        f"(their work, their contract, their decisions, their performance, "
        f"their correspondence, their identity).\n"
        f"  - work_context_only: the document content is about OTHER people / "
        f"OTHER projects / generic business matters; the data subject (if "
        f"present at all) is only a peripheral cc/bcc recipient or one name "
        f"among many in a list.\n"
        f"  - ambiguous: evidence is genuinely mixed.\n"
        f"Default to `biographical` ONLY when the evidence is balanced. If "
        f"the document is clearly about a third party's work or another "
        f"person, choose `work_context_only` — that is the whole point of "
        f"the Durant test.\n\n"
        f"=== STEP 2: GENERAL CLASSIFICATION ===\n"
        f"Independently, categorise the document type (primary_classification) "
        f"and whether it concerns the subject (is_about_requester). These "
        f"signals are used downstream; do not let them override the durant "
        f"verdict above.\n\n"
        f"=== DOCUMENT (ref={doc_ref}) ===\n"
        f"{truncated}{truncation_note}\n\n"
        f"Respond with a single JSON object using these exact keys: "
        f'{{"durant_verdict": "<one of: {", ".join(ALLOWED_DURANT)}>", '
        f'"durant_rationale": "<one or two sentences citing the evidence; '
        f'explicitly name who the document is ABOUT>", '
        f'"primary_classification": "<one of: {", ".join(ALLOWED_CLASSIFICATIONS)}>", '
        f'"is_about_requester": "<one of: {", ".join(ALLOWED_IS_ABOUT)}>", '
        f'"confidence": <float 0.0-1.0>, '
        f'"requester_role": "<author|recipient|cc|subject|none>", '
        f'"evidence_snippet": "<short quote from doc, up to 200 chars>", '
        f'"recommended_action": "<disclose|escalate|redact|withhold>", '
        f'"rationale": "<one sentence>"}}. '
        f"No prose, no markdown fences."
    )


def _post(system: str, user: str, max_tokens: int = 600) -> dict:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(BROKER, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.load(resp)


def _coerce_parsed(parsed: dict) -> dict:
    """Best-effort coercion of model output into the agent05-compatible
    shape. Missing / wrong-typed fields fall back to safe defaults so a
    single bad row doesn't poison the stage."""
    durant = parsed.get("durant_verdict", "ambiguous")
    if durant not in ALLOWED_DURANT:
        durant = "ambiguous"
    pc = parsed.get("primary_classification", "communication")
    if pc not in ALLOWED_CLASSIFICATIONS:
        pc = "other"
    iar = parsed.get("is_about_requester", "unclear")
    if iar not in ALLOWED_IS_ABOUT:
        iar = "unclear"
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {
        "durant_verdict": durant,
        "durant_rationale": str(parsed.get("durant_rationale", ""))[:600],
        "primary_classification": pc,
        "is_about_requester": iar,
        "confidence": conf,
        "requester_role": str(parsed.get("requester_role", "none"))[:32],
        "evidence_snippet": str(parsed.get("evidence_snippet", ""))[:300],
        "recommended_action": str(parsed.get("recommended_action", "escalate"))[:32],
        "rationale": str(parsed.get("rationale", ""))[:500],
    }


def _strip_fences(content: str) -> str:
    s = content.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s
        if s.lower().startswith("json"):
            s = s[4:]
    return s.strip().rstrip("`").strip()


def _error_row(
    base: dict, *, rationale: str, error_state: str, elapsed: float | None = None
) -> dict:
    """Build an error row in the agent05-compatible shape so downstream
    consumers don't have to special-case errors."""
    row = {
        **base,
        "durant_verdict": "ambiguous",
        "durant_rationale": "",
        "primary_classification": "communication",
        "is_about_requester": "unclear",
        "confidence": 0.0,
        "requester_role": "none",
        "evidence_snippet": "",
        "recommended_action": "escalate",
        "rationale": rationale,
        "error_state": error_state,
    }
    if elapsed is not None:
        row["elapsed_sec"] = elapsed
    return row


def classify_one(
    *,
    case_id: str,
    doc_ref: str,
    text: str,
    subject_summary: str,
) -> dict:
    """Classify a single document. Always returns an agent05-compatible
    row; errors land in ``error_state`` rather than raising."""
    system = (
        "You classify documents for a DSAR (Data Subject Access Request) "
        "pipeline. Return ONLY valid JSON with the requested keys; no prose, "
        "no markdown."
    )
    user = _build_user_prompt(doc_ref, text, subject_summary)
    started = time.monotonic()
    base = {
        "case_id": case_id,
        "doc_ref": doc_ref,
        "model": f"{MODEL}@127.0.0.1:8090/v1",
        "prompt_version": PROMPT_VERSION,
    }
    try:
        raw = _post(system, user)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return _error_row(base, rationale=f"network error: {exc}", error_state="model_unreachable")
    elapsed = round(time.monotonic() - started, 2)
    content = (raw["choices"][0]["message"].get("content") or "").strip()
    content = _strip_fences(content)
    if not content:
        return _error_row(
            base,
            rationale="model returned empty content",
            error_state="empty_response",
            elapsed=elapsed,
        )
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return _error_row(
            base,
            rationale=f"json decode error: {exc}; raw[:200]={content[:200]!r}",
            error_state="schema_validation_failed",
            elapsed=elapsed,
        )
    coerced = _coerce_parsed(parsed)
    return {**base, **coerced, "elapsed_sec": elapsed}


def run(case_root: Path) -> int:
    working = case_root / "working"
    audit = case_root / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    ingested = working / "ingested_items.jsonl"
    register = working / "register.json"
    data_subject = working / "data_subject.json"
    output = working / "context_classifications.jsonl"
    progress_log = audit / "context-classify-mini-progress.jsonl"

    if not ingested.exists():
        log.error("ingested_items.jsonl missing at %s", ingested)
        return 1
    if not register.exists():
        log.error("register.json missing at %s", register)
        return 1
    if not data_subject.exists():
        log.error("data_subject.json missing at %s", data_subject)
        return 1

    canonical = None if os.environ.get("INCLUDE_DUPLICATES") == "1" else canonical_refs(case_root)
    if canonical is not None:
        log.info(
            "DEDUPE FILTER ON: %d canonical refs; non-canonical skipped",
            len(canonical),
        )

    by_path = _load_path_to_ref(case_root)
    log.info("loaded register with %d entries", len(by_path))
    subject_summary = _load_subject_summary(case_root)
    log.info("subject: %s", subject_summary[:160])

    done = _load_completed_refs(output)
    log.info("resume: %d doc_refs already classified", len(done))

    case_id = case_root.name
    started = time.monotonic()
    processed = 0
    errors = 0
    skipped_no_register = 0
    skipped_no_text = 0
    progress = progress_log.open("a")

    with output.open("a", encoding="utf-8") as fout, ingested.open() as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = rec.get("source_location", {}).get("path", "")
            entry = by_path.get(path)
            if not entry:
                skipped_no_register += 1
                continue
            doc_ref = entry["ref"]
            if canonical is not None and doc_ref not in canonical:
                continue
            if doc_ref in done:
                continue
            text_file = Path(entry["text_file"])
            if not text_file.exists():
                skipped_no_text += 1
                continue
            try:
                text = text_file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.warning("text read failed for %s: %s", doc_ref, exc)
                skipped_no_text += 1
                continue
            result = classify_one(
                case_id=case_id,
                doc_ref=doc_ref,
                text=text,
                subject_summary=subject_summary,
            )
            fout.write(json.dumps(result, ensure_ascii=False) + "\n")
            fout.flush()
            done.add(doc_ref)
            processed += 1
            if result.get("error_state"):
                errors += 1
            if processed % PROGRESS_INTERVAL == 0:
                elapsed = time.monotonic() - started
                rate = processed / elapsed if elapsed > 0 else 0.0
                remaining = (len(by_path) - len(done)) / rate if rate > 0 else float("inf")
                log.info(
                    "processed %d (errors %d); rate %.2f/s; ~%.1f min remaining",
                    processed,
                    errors,
                    rate,
                    remaining / 60,
                )
                progress.write(
                    json.dumps(
                        {
                            "ts": _iso_now(),
                            "processed": processed,
                            "errors": errors,
                            "rate_per_sec": round(rate, 3),
                            "remaining_min": round(remaining / 60, 1),
                            "total_done": len(done),
                        }
                    )
                    + "\n"
                )
                progress.flush()

    elapsed = time.monotonic() - started
    log.info(
        "done: processed=%d errors=%d skipped_no_register=%d "
        "skipped_no_text=%d elapsed=%.0fs (%.1f min)",
        processed,
        errors,
        skipped_no_register,
        skipped_no_text,
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
        prog="dsar-context-classify-mini",
        description="Local-broker context classifier — fast bypass of the toolkit's remote 30B classifier.",
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
