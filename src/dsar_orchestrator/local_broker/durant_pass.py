"""Durant biographical-focus test pass for a DSAR case.

Applies the Durant v FSA (2003) test to each ingested document via the
local mlx-broker (model alias ``mini``). Output:
``<case-root>/working/durant_verdicts.jsonl``.

The Durant test asks whether a document is *biographical* for the data
subject (focus of the content) vs *work_context_only* (subject is
peripheral cc/bcc on an unrelated topic) vs *ambiguous*. The default
under uncertainty is ``biographical`` — under-disclosure is the worse
error per the user's directive.

CLI usage:
  dsar-durant-pass                     # uses cwd or $DSAR_CASE_ROOT
  dsar-durant-pass --case-root <path>

Resume-safe: a re-run skips refs already classified successfully and
re-processes any rows that errored out (errored rows are dropped from
the output file atomically before the rerun starts).

Honours the canonical-only dedupe filter from
``dsar_orchestrator.local_broker.dedupe_filter`` — set
``INCLUDE_DUPLICATES=1`` to opt out.
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
PROMPT_VERSION = "durant/v1"
MAX_TEXT_CHARS = 6000
TIMEOUT_SECONDS = 120.0
PROGRESS_INTERVAL = 50

# Verbatim from dsar_pipeline/gates/gate_durant.py:DURANT_SYSTEM_PROMPT
DURANT_SYSTEM_PROMPT = """You are a UK data-protection adjudicator applying the Durant v FSA \
biographical-focus test to a single document. The data subject has \
made a UK GDPR Article 15 access request. A document is disclosable \
only if it is biographical for the subject — i.e. the subject is the \
focus of the content, not merely an incidental cc/bcc recipient on a \
matter about other people or about a business topic.

Three verdicts are possible:
  biographical — subject is named/discussed; document content is \
about them (their work, their decisions, their performance, their \
correspondence, their identity).
  work_context_only — subject appears only as a cc/bcc recipient or \
as a routine business addressee, and the document content is about \
other matters (third-party operations, unrelated projects, generic \
broadcasts).
  ambiguous — evidence is mixed; cannot decide cleanly.

Default to 'biographical' under uncertainty (the operator can exclude \
later; under-disclosure is the worse error). Direct-addressee status \
(subject in the To: line) of an email about a topic that concerns the \
subject themselves (e.g. their contract, their assignment, their \
performance) is biographical, not work_context_only — work_context_only \
is reserved for cases where the subject is a peripheral cc/bcc and \
the topic is unrelated to them.

Respond with VALID JSON ONLY, no markdown, no prose. Schema:
{"durant_verdict": "<biographical|work_context_only|ambiguous>", \
"rationale": "<one or two sentences citing the evidence>"}"""

VALID_VERDICTS = ("biographical", "work_context_only", "ambiguous")
RETRY_DELAYS_S = (2.0, 8.0, 30.0, 60.0)

log = logging.getLogger("durant-pass")


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


def _post(system: str, user: str, max_tokens: int = 400) -> dict:
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


def _load_subject_summary(case_root: Path) -> str:
    data = json.loads((case_root / "working" / "data_subject.json").read_text())
    parts = [f"name='{data['full_name']}'"]
    if data.get("aliases"):
        parts.append(f"aliases={data['aliases']}")
    if data.get("email"):
        parts.append(f"primary_email='{data['email']}'")
    if data.get("additional_emails"):
        parts.append(f"additional_emails={data['additional_emails']}")
    return "; ".join(parts)


def _load_register(case_root: Path) -> dict[str, dict]:
    by_path = {}
    for entry in json.loads((case_root / "working" / "register.json").read_text()):
        by_path[entry["path"]] = entry
    return by_path


def _load_completed_refs(output: Path) -> set[str]:
    """Resume-skip set; rewrite the file atomically to drop errored rows."""
    if not output.exists():
        return set()
    keep_rows: list[dict] = []
    errored_count = 0
    with output.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not r.get("doc_ref"):
                continue
            if r.get("error_state"):
                errored_count += 1
                continue
            keep_rows.append(r)
    if errored_count > 0:
        log.info(
            "resume cleanup: dropping %d errored rows; keeping %d successful",
            errored_count,
            len(keep_rows),
        )
        tmp_path = output.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for r in keep_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp_path.replace(output)
    return {r["doc_ref"] for r in keep_rows}


def classify_one(*, case_id: str, doc_ref: str, text: str, subject_summary: str) -> dict:
    truncated = text[:MAX_TEXT_CHARS]
    truncation_note = (
        f"\n[truncated from {len(text)} to {MAX_TEXT_CHARS} chars]"
        if len(text) > MAX_TEXT_CHARS
        else ""
    )
    user = (
        f"Data subject: {subject_summary}\n\n"
        f"Document ref: {doc_ref}\n\n"
        f"Document content:\n{truncated}{truncation_note}"
    )
    base = {
        "case_id": case_id,
        "doc_ref": doc_ref,
        "model": f"{MODEL}@127.0.0.1:8090/v1",
        "prompt_version": PROMPT_VERSION,
    }
    started = time.monotonic()
    try:
        raw = _post(DURANT_SYSTEM_PROMPT, user)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return {
            **base,
            "durant_verdict": "ambiguous",
            "rationale": f"network error: {exc}",
            "error_state": "model_unreachable",
        }
    elapsed = round(time.monotonic() - started, 2)
    content = (raw["choices"][0]["message"].get("content") or "").strip()
    content = _strip_fences(content)
    if not content:
        return {
            **base,
            "durant_verdict": "ambiguous",
            "rationale": "model returned empty content",
            "error_state": "empty_response",
            "elapsed_sec": elapsed,
        }
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return {
            **base,
            "durant_verdict": "ambiguous",
            "rationale": f"json decode error: {exc}; raw[:200]={content[:200]!r}",
            "error_state": "schema_validation_failed",
            "elapsed_sec": elapsed,
        }
    verdict = str(parsed.get("durant_verdict", "ambiguous"))
    if verdict not in VALID_VERDICTS:
        verdict = "ambiguous"
    return {
        **base,
        "durant_verdict": verdict,
        "rationale": str(parsed.get("rationale", ""))[:600],
        "elapsed_sec": elapsed,
    }


def run(case_root: Path) -> int:
    """Apply the Durant pass to every ingested doc under ``case_root``."""
    working = case_root / "working"
    audit = case_root / "audit"
    ingested = working / "ingested_items.jsonl"
    register = working / "register.json"
    data_subject = working / "data_subject.json"
    output = working / "durant_verdicts.jsonl"
    progress_log = audit / "durant-progress.jsonl"
    audit.mkdir(parents=True, exist_ok=True)

    if not ingested.exists() or not register.exists() or not data_subject.exists():
        log.error("required inputs missing under %s/working", case_root)
        return 1

    canonical = None if os.environ.get("INCLUDE_DUPLICATES") == "1" else canonical_refs(case_root)
    if canonical is not None:
        log.info(
            "DEDUPE FILTER ON: %d canonical refs; non-canonical inputs skipped",
            len(canonical),
        )
    elif os.environ.get("INCLUDE_DUPLICATES") == "1":
        log.info("DEDUPE FILTER OFF: INCLUDE_DUPLICATES=1")
    else:
        log.info("DEDUPE FILTER N/A: no dedupe_findings.jsonl yet")

    by_path = _load_register(case_root)
    subject_summary = _load_subject_summary(case_root)
    log.info(
        "register: %d entries; subject: %s",
        len(by_path),
        subject_summary[:120],
    )

    done = _load_completed_refs(output)
    log.info("resume: %d already classified", len(done))

    case_id = case_root.name
    started = time.monotonic()
    processed = 0
    errors = 0
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
                continue
            doc_ref = entry["ref"]
            if canonical is not None and doc_ref not in canonical:
                continue
            if doc_ref in done:
                continue
            text_file = Path(entry["text_file"])
            if not text_file.exists():
                continue
            try:
                text = text_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
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
                        }
                    )
                    + "\n"
                )
                progress.flush()

    elapsed = time.monotonic() - started
    log.info(
        "done: processed=%d errors=%d elapsed=%.0fs (%.1f min)",
        processed,
        errors,
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
        prog="dsar-durant-pass",
        description="Apply the Durant biographical-focus test to every ingested doc in a case.",
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
