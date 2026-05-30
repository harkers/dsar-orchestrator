"""Under-disclosure safety net for a DSAR Durant pass.

For every doc that ``durant_pass`` marked ``work_context_only`` (i.e.
EXCLUDED from the disclosure pack), asks the broker the inverse
question: "Is there ANY reasonable basis to think this doc IS about
the subject after all?" Catches false negatives before disclosure.

Output: ``<case-root>/working/durant_underdisclosure_recheck.jsonl``.

CLI usage:
  dsar-durant-recheck                     # uses cwd or $DSAR_CASE_ROOT
  dsar-durant-recheck --case-root <path>

Resume-safe with the same atomic-cleanup-of-errored-rows pattern as
``durant_pass``. Retry-with-backoff (2/8/30/60s) on HTTP 500 /
connection errors / timeouts.
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
MAX_TEXT_CHARS = 6000
TIMEOUT_SECONDS = 120.0
PROGRESS_INTERVAL = 100

RETRY_DELAYS_S = (2.0, 8.0, 30.0, 60.0)

ALLOWED_VERDICTS = (
    "confirmed_work_context_only",
    "reclassify_to_biographical",
    "reclassify_to_ambiguous",
)

# Recheck system prompt loaded from the toolkit's sealed prompt registry so
# every recheck row carries verifiable provenance. The registered
# durant.recheck.system v1.1.0 body is byte-identical to the prior inline
# constant — adjudication unchanged, provenance added.
_RECHECK_PROMPT_ID = "durant.recheck.system"


def _recheck_asset():
    """Load the sealed durant.recheck.system asset (lru_cached in PromptLoader).
    Fail-loud if the toolkit prompt registry is unavailable."""
    from dsar_pipeline.gates.prompt_loader import PromptLoader

    return PromptLoader.load(_RECHECK_PROMPT_ID)


log = logging.getLogger("durant-recheck")


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


def _load_register_by_ref(case_root: Path) -> dict[str, dict]:
    return {e["ref"]: e for e in json.loads((case_root / "working" / "register.json").read_text())}


def _load_completed_refs(output: Path) -> set[str]:
    """Resume-safe: drop errored rows; keep successful ones."""
    if not output.exists():
        return set()
    keep_rows: list[dict] = []
    errored = 0
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
                errored += 1
                continue
            keep_rows.append(r)
    if errored:
        log.info("resume cleanup: dropping %d errored; keeping %d", errored, len(keep_rows))
        tmp = output.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in keep_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(output)
    return {r["doc_ref"] for r in keep_rows}


def _excluded_durant_refs(durant_verdicts: Path) -> list[tuple[str, str]]:
    """Returns (doc_ref, original_rationale) for work_context_only rows."""
    out = []
    with durant_verdicts.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("durant_verdict") == "work_context_only":
                out.append((r["doc_ref"], r.get("rationale", "")))
    return out


def recheck_one(
    *,
    case_id: str,
    doc_ref: str,
    text: str,
    subject_summary: str,
    original_rationale: str,
) -> dict:
    """Second-guess the Durant exclusion for one doc.

    Confirmation-bias guard: original_rationale is NOT shown to the
    model — only kept in the audit record (base dict) so the chain of
    reasoning stays reconstructible. The recheck assesses the doc on
    its own merits.
    """
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
    asset = _recheck_asset()
    base = {
        "case_id": case_id,
        "doc_ref": doc_ref,
        "model": f"{MODEL}@127.0.0.1:8090/v1",
        "prompt_id": asset.prompt_id,
        "prompt_version": asset.version,
        "prompt_canonical_seal_sha256": asset.canonical_seal_sha256,
        "prompt_effective_sha256": asset.effective_sha256,
        "prompt_applied_strips": list(asset.applied_strips or []),
        "original_verdict": "work_context_only",
        "original_rationale": original_rationale,
    }
    started = time.monotonic()
    try:
        raw = _post(asset.body, user)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return {
            **base,
            "recheck_verdict": "reclassify_to_ambiguous",
            "rationale": f"network error: {exc}",
            "error_state": "model_unreachable",
        }
    elapsed = round(time.monotonic() - started, 2)
    content = (raw["choices"][0]["message"].get("content") or "").strip()
    content = _strip_fences(content)
    if not content:
        return {
            **base,
            "recheck_verdict": "reclassify_to_ambiguous",
            "rationale": "empty content",
            "error_state": "empty_response",
            "elapsed_sec": elapsed,
        }
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return {
            **base,
            "recheck_verdict": "reclassify_to_ambiguous",
            "rationale": f"json decode error: {exc}",
            "error_state": "schema_validation_failed",
            "elapsed_sec": elapsed,
        }
    # Malformed verdicts default to ambiguous (escalation), NOT
    # confirmed-exclude. For an under-disclosure safety check,
    # "I'm not sure" must escalate, never silently agree.
    verdict = str(parsed.get("recheck_verdict", "reclassify_to_ambiguous"))
    if verdict not in ALLOWED_VERDICTS:
        verdict = "reclassify_to_ambiguous"
    return {
        **base,
        "recheck_verdict": verdict,
        "rationale": str(parsed.get("rationale", ""))[:600],
        "elapsed_sec": elapsed,
    }


def run(case_root: Path) -> int:
    working = case_root / "working"
    audit = case_root / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    durant_verdicts = working / "durant_verdicts.jsonl"
    register = working / "register.json"
    data_subject = working / "data_subject.json"
    output = working / "durant_underdisclosure_recheck.jsonl"
    progress_log = audit / "durant-recheck-progress.jsonl"

    if not (durant_verdicts.exists() and register.exists() and data_subject.exists()):
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

    subject = _load_subject_summary(case_root)
    by_ref = _load_register_by_ref(case_root)
    candidates = _excluded_durant_refs(durant_verdicts)
    log.info(
        "rechecking %d durant=work_context_only docs; subject: %s",
        len(candidates),
        subject[:120],
    )
    done = _load_completed_refs(output)
    log.info("resume: %d already rechecked", len(done))

    case_id = case_root.name
    started = time.monotonic()
    processed = 0
    errors = 0
    progress = progress_log.open("a")

    with output.open("a", encoding="utf-8") as fout:
        for doc_ref, orig_rationale in candidates:
            if canonical is not None and doc_ref not in canonical:
                continue
            if doc_ref in done:
                continue
            entry = by_ref.get(doc_ref)
            if not entry:
                continue
            text_file = Path(entry["text_file"])
            if not text_file.exists():
                continue
            try:
                text = text_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            result = recheck_one(
                case_id=case_id,
                doc_ref=doc_ref,
                text=text,
                subject_summary=subject,
                original_rationale=orig_rationale,
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
                remaining = (len(candidates) - len(done)) / rate if rate > 0 else float("inf")
                log.info(
                    "processed %d/%d (errors %d); rate %.2f/s; ~%.1f min remaining",
                    processed,
                    len(candidates),
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
        prog="dsar-durant-recheck",
        description=(
            "Under-disclosure safety net for the Durant pass; rechecks every work_context_only doc."
        ),
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
