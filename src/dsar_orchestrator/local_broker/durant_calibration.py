"""Operator-calibration web portal for a DSAR Durant pass.

Serves a stratified 30-doc sample for manual ground-truth review.
Operator decides yes/no/uncertain per doc on the question:
"is this document about the data subject?" Ground truth lands at
``<case-root>/working/operator_calibration_30.jsonl``. A comparison
report shows operator vs original-Durant vs recheck agreement rates.

Distinct from ``qa_sample`` (#108) which validates redaction quality.
This validates the Durant verdict quality.

Stratified sample composition:
  - 10 disputed: durant=work_context_only + recheck=reclassify_to_biographical
  - 10 agreed-exclude: durant=work_context_only + recheck=confirmed_work_context_only
  -  5 recheck-ambiguous: recheck=reclassify_to_ambiguous
  -  5 originally-biographical: durant=biographical (validate positive set)

CLI usage:
  dsar-durant-calibration                   # build sample, serve on :8088
  dsar-durant-calibration --case-root <path>
  dsar-durant-calibration --port N
  dsar-durant-calibration --sample-only     # rebuild sample, exit
  dsar-durant-calibration --report-only     # print agreement report, exit
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8088
HOST = "127.0.0.1"
TEXT_PREVIEW_CHARS = 12000

SAMPLE_STRATA = {
    "disputed_recheck_says_bio": 10,
    "agreed_work_context_only": 10,
    "recheck_ambiguous": 5,
    "originally_biographical": 5,
}

_DECISIONS_LOCK = threading.Lock()

log = logging.getLogger("durant-calibration")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_case_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("DSAR_CASE_ROOT")
    if env:
        return Path(env)
    return Path.cwd()


# --- Paths derived from case_root ---


def _paths(case_root: Path) -> dict[str, Path]:
    working = case_root / "working"
    audit = case_root / "audit"
    return {
        "working": working,
        "audit": audit,
        "register": working / "register.json",
        "durant_verdicts": working / "durant_verdicts.jsonl",
        "recheck_verdicts": working / "durant_underdisclosure_recheck.jsonl",
        "data_subject": working / "data_subject.json",
        "sample_file": audit / "calibration_sample_30.json",
        "decisions_file": working / "operator_calibration_30.jsonl",
    }


# --- Sample building ---


def _load_verdicts(case_root: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    paths = _paths(case_root)
    durant: dict[str, dict] = {}
    with paths["durant_verdicts"].open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("doc_ref"):
                durant[r["doc_ref"]] = r
    recheck: dict[str, dict] = {}
    if paths["recheck_verdicts"].exists():
        with paths["recheck_verdicts"].open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("doc_ref"):
                    recheck[r["doc_ref"]] = r
    return durant, recheck


def _register_by_ref(case_root: Path) -> dict[str, dict]:
    return {e["ref"]: e for e in json.loads(_paths(case_root)["register"].read_text())}


def build_sample(case_root: Path, *, seed: int = 0xDEC01D) -> list[dict]:
    """Build the stratified sample. Deterministic with the default seed
    so the same 30 docs are selected across rebuilds (operator can resume
    a partial review session)."""
    paths = _paths(case_root)
    durant, recheck = _load_verdicts(case_root)
    register = _register_by_ref(case_root)

    disputed: list[str] = []
    agreed_excl: list[str] = []
    recheck_amb: list[str] = []
    orig_bio: list[str] = []
    for ref, d in durant.items():
        d_v = d.get("durant_verdict")
        r = recheck.get(ref)
        r_v = r.get("recheck_verdict") if r else None
        if ref not in register:
            continue
        if d_v == "work_context_only" and r_v == "reclassify_to_biographical":
            disputed.append(ref)
        elif d_v == "work_context_only" and r_v == "confirmed_work_context_only":
            agreed_excl.append(ref)
        elif r_v == "reclassify_to_ambiguous":
            recheck_amb.append(ref)
        elif d_v == "biographical":
            orig_bio.append(ref)

    rng = random.Random(seed)
    picked: list[dict] = []
    for stratum_name, pool in [
        ("disputed_recheck_says_bio", disputed),
        ("agreed_work_context_only", agreed_excl),
        ("recheck_ambiguous", recheck_amb),
        ("originally_biographical", orig_bio),
    ]:
        want = SAMPLE_STRATA[stratum_name]
        if len(pool) < want:
            log.warning(
                "stratum %s has %d available; want %d (under-sampling)",
                stratum_name,
                len(pool),
                want,
            )
        sample_refs = rng.sample(pool, min(want, len(pool)))
        for ref in sample_refs:
            picked.append(
                {
                    "ref": ref,
                    "stratum": stratum_name,
                    "filename": register[ref].get("filename", ""),
                    "durant_verdict": durant[ref].get("durant_verdict"),
                    "durant_rationale": durant[ref].get("rationale", ""),
                    "recheck_verdict": (recheck.get(ref) or {}).get("recheck_verdict"),
                    "recheck_rationale": (recheck.get(ref) or {}).get("rationale", ""),
                }
            )
    paths["sample_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["sample_file"].write_text(json.dumps(picked, indent=2))
    log.info(
        "wrote %d-doc sample to %s (strata=%s)",
        len(picked),
        paths["sample_file"],
        {s: sum(1 for p in picked if p["stratum"] == s) for s in SAMPLE_STRATA},
    )
    return picked


# --- Decisions storage ---


def _load_decisions(case_root: Path) -> dict[str, dict]:
    paths = _paths(case_root)
    if not paths["decisions_file"].exists():
        return {}
    by_ref: dict[str, dict] = {}
    with paths["decisions_file"].open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("ref"):
                by_ref[r["ref"]] = r
    return by_ref


def _save_decision(
    case_root: Path,
    *,
    ref: str,
    verdict: str,
    notes: str,
    decided_at: str,
    time_taken_s: float,
) -> None:
    """Append a new decision row. Never overwrite — every click is
    recorded; latest (last appended) row for a ref wins on read."""
    payload = {
        "ref": ref,
        "verdict": verdict,
        "notes": notes,
        "decided_at": decided_at,
        "time_taken_s": round(time_taken_s, 2),
    }
    target = _paths(case_root)["decisions_file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with _DECISIONS_LOCK:
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# --- Comparison report ---


def _normalise_op_verdict(v: str) -> str:
    return {"yes": "include", "no": "exclude", "uncertain": "uncertain"}.get(v, v)


def _normalise_durant(v: str | None) -> str:
    return {
        "biographical": "include",
        "work_context_only": "exclude",
        "ambiguous": "uncertain",
    }.get(v or "", "uncertain")


def _normalise_recheck(v: str | None) -> str:
    return {
        "reclassify_to_biographical": "include",
        "confirmed_work_context_only": "exclude",
        "reclassify_to_ambiguous": "uncertain",
    }.get(v or "", "uncertain")


def agreement_report(case_root: Path) -> str:
    paths = _paths(case_root)
    sample = json.loads(paths["sample_file"].read_text()) if paths["sample_file"].exists() else []
    decisions = _load_decisions(case_root)
    if not sample:
        return "no sample built yet"
    lines = [
        f"Calibration report — {len(sample)} sample docs, {len(decisions)} decisions recorded",
        "",
    ]
    for stratum in SAMPLE_STRATA:
        rows = [s for s in sample if s["stratum"] == stratum]
        decided = [s for s in rows if s["ref"] in decisions]
        lines.append(f"## Stratum: {stratum}  ({len(decided)}/{len(rows)} decided)")
        op_dur = op_rec = 0
        for s in decided:
            op = _normalise_op_verdict(decisions[s["ref"]]["verdict"])
            dur = _normalise_durant(s["durant_verdict"])
            rec = _normalise_recheck(s["recheck_verdict"])
            if op == dur:
                op_dur += 1
            if op == rec:
                op_rec += 1
        if decided:
            lines.append(f"  operator agrees with original durant: {op_dur}/{len(decided)}")
            lines.append(f"  operator agrees with recheck:         {op_rec}/{len(decided)}")
        lines.append("")
    decided_all = [s for s in sample if s["ref"] in decisions]
    if decided_all:
        op_dur = sum(
            1
            for s in decided_all
            if _normalise_op_verdict(decisions[s["ref"]]["verdict"])
            == _normalise_durant(s["durant_verdict"])
        )
        op_rec = sum(
            1
            for s in decided_all
            if _normalise_op_verdict(decisions[s["ref"]]["verdict"])
            == _normalise_recheck(s["recheck_verdict"])
        )
        lines.append(f"## Overall ({len(decided_all)} decided)")
        lines.append(
            f"  operator agrees with original durant: {op_dur}/{len(decided_all)} "
            f"({100 * op_dur / len(decided_all):.0f}%)"
        )
        lines.append(
            f"  operator agrees with recheck:         {op_rec}/{len(decided_all)} "
            f"({100 * op_rec / len(decided_all):.0f}%)"
        )
    return "\n".join(lines)


# --- HTTP layer ---


_PAGE_CSS = """
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #1f2328; }
h1, h2 { font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; }
th, td { border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; font-size: 13px; }
th { background: #f6f8fa; }
tr.decided { background: #ddf4ff; }
.verdict-box { background: #f6f8fa; padding: 8px 12px; margin: 8px 0; border-left: 3px solid #0969da; }
.verdict-box.bio { border-left-color: #1f883d; }
.verdict-box.wco { border-left-color: #cf222e; }
.verdict-box.amb { border-left-color: #9a6700; }
.text-pre { background: #f6f8fa; border: 1px solid #d0d7de; padding: 12px;
            font-family: ui-monospace, 'SF Mono', monospace; font-size: 12px;
            white-space: pre-wrap; max-height: 60vh; overflow: auto; }
.decision-bar { position: sticky; bottom: 0; background: #fff; border-top: 1px solid #d0d7de;
                padding: 16px 0; display: flex; gap: 12px; align-items: center; margin-top: 16px; }
.btn { padding: 10px 20px; border-radius: 6px; border: 1px solid; cursor: pointer; font-size: 14px; }
.btn-yes { background: #1f883d; color: white; border-color: #1f883d; }
.btn-no  { background: #cf222e; color: white; border-color: #cf222e; }
.btn-unc { background: #9a6700; color: white; border-color: #9a6700; }
.btn-skip{ background: #f6f8fa; color: #1f2328; border-color: #d0d7de; }
input[type=text] { padding: 8px; border-radius: 6px; border: 1px solid #d0d7de;
                   flex: 1; font-size: 13px; }
.progress { font-size: 13px; color: #57606a; margin-left: auto; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
</style>
"""


def _verdict_class(v: str | None) -> str:
    if v in ("biographical", "reclassify_to_biographical"):
        return "bio"
    if v in ("work_context_only", "confirmed_work_context_only"):
        return "wco"
    return "amb"


def _subject_display_name(case_root: Path) -> str:
    """Read full_name from data_subject.json; fall back to dir name."""
    try:
        data = json.loads(_paths(case_root)["data_subject"].read_text())
    except (OSError, json.JSONDecodeError):
        return case_root.name
    return str(data.get("full_name") or case_root.name)


def _index_html(case_root: Path, sample: list[dict], decisions: dict[str, dict]) -> str:
    subject = html.escape(_subject_display_name(case_root))
    case_name = html.escape(case_root.name)
    rows_html = []
    for i, s in enumerate(sample):
        decided = s["ref"] in decisions
        op = decisions.get(s["ref"], {}).get("verdict", "")
        cls = "decided" if decided else ""
        rows_html.append(
            f"<tr class='{cls}'><td>{i + 1}</td>"
            f"<td><a href='/review/{html.escape(s['ref'])}'>{html.escape(s['ref'])}</a></td>"
            f"<td>{html.escape(s['stratum'])}</td>"
            f"<td>{html.escape(s.get('filename', '')[:60])}</td>"
            f"<td>{html.escape(s.get('durant_verdict') or '')}</td>"
            f"<td>{html.escape(s.get('recheck_verdict') or '')}</td>"
            f"<td><b>{html.escape(op)}</b></td></tr>"
        )
    progress = f"{len(decisions)}/{len(sample)} decided"
    next_undecided = next((s["ref"] for s in sample if s["ref"] not in decisions), None)
    next_link = (
        f"<a class='btn btn-yes' href='/review/{html.escape(next_undecided)}'>"
        f"Start / continue review &rarr;</a>"
        if next_undecided
        else "<b>All decided.</b> <a href='/report'>View report</a>"
    )
    return f"""<!doctype html>
<html><head><title>Calibration Portal — {case_name}</title>{_PAGE_CSS}</head><body>
<h1>Operator calibration — {case_name}</h1>
<p>Manual ground-truth review of a stratified 30-doc sample. Subject: <b>{subject}</b>.
Compare against the original Durant pass and the recheck pass.</p>
<p>{next_link} &nbsp; <span class='progress'>{progress}</span> &nbsp; <a href='/report'>Report</a></p>
<table>
<thead><tr><th>#</th><th>ref</th><th>stratum</th><th>filename</th>
<th>durant</th><th>recheck</th><th>operator</th></tr></thead>
<tbody>{"".join(rows_html)}</tbody></table>
</body></html>"""


def _review_html(
    case_root: Path, sample: list[dict], decisions: dict[str, dict], ref: str
) -> str | None:
    entry = next((s for s in sample if s["ref"] == ref), None)
    if not entry:
        return None
    register = _register_by_ref(case_root)
    reg = register.get(ref, {})
    text_file = Path(reg.get("text_file", ""))
    text_preview = ""
    if text_file.exists():
        try:
            full = text_file.read_text(encoding="utf-8", errors="replace")
            text_preview = full[:TEXT_PREVIEW_CHARS]
            if len(full) > TEXT_PREVIEW_CHARS:
                text_preview += f"\n\n[...truncated from {len(full)} chars]"
        except OSError:
            text_preview = "[failed to read text file]"
    idx = next(i for i, s in enumerate(sample) if s["ref"] == ref)
    prev_ref = sample[idx - 1]["ref"] if idx > 0 else None
    next_ref = sample[idx + 1]["ref"] if idx < len(sample) - 1 else None
    existing = decisions.get(ref)
    dv = entry.get("durant_verdict")
    rv = entry.get("recheck_verdict")
    subject = html.escape(_subject_display_name(case_root))
    return f"""<!doctype html>
<html><head><title>Review {html.escape(ref)}</title>{_PAGE_CSS}</head><body>
<p><a href='/'>&larr; back to list</a> &nbsp; doc {idx + 1} of {len(sample)} &nbsp;
{f"<a href='/review/{html.escape(prev_ref)}'>prev</a>" if prev_ref else ""} &nbsp;
{f"<a href='/review/{html.escape(next_ref)}'>next</a>" if next_ref else ""}</p>

<h1>{html.escape(reg.get("filename", "") or ref)}</h1>
<p><b>Ref:</b> {html.escape(ref)} &nbsp; <b>Stratum:</b> {html.escape(entry["stratum"])}</p>

<div class='verdict-box {_verdict_class(dv)}'><b>Original Durant:</b> {html.escape(dv or "?")}<br>
<i>Rationale:</i> {html.escape(entry.get("durant_rationale", "") or "(none)")}</div>

<div class='verdict-box {_verdict_class(rv)}'><b>Recheck:</b> {html.escape(rv or "?")}<br>
<i>Rationale:</i> {html.escape(entry.get("recheck_rationale", "") or "(none)")}</div>

<h2>Document text (first {TEXT_PREVIEW_CHARS} chars)</h2>
<div class='text-pre'>{html.escape(text_preview)}</div>

<form method='POST' action='/decision' class='decision-bar'>
<input type='hidden' name='ref' value='{html.escape(ref)}'>
<input type='hidden' name='shown_at' value='{_iso_now()}'>
<input type='hidden' name='next_ref' value='{html.escape(next_ref) if next_ref else ""}'>
<input type='text' name='notes' placeholder='optional note...'
       value='{html.escape((existing or {}).get("notes", ""))}'>
<button type='submit' name='verdict' value='yes' class='btn btn-yes'>YES — about {subject}</button>
<button type='submit' name='verdict' value='no'  class='btn btn-no'>NO — not about {subject}</button>
<button type='submit' name='verdict' value='uncertain' class='btn btn-unc'>Uncertain</button>
{f"<a href='/review/{html.escape(next_ref)}' class='btn btn-skip'>Skip</a>" if next_ref else ""}
<span class='progress'>
{f"previously: <b>{html.escape(existing['verdict'])}</b>" if existing else "not yet decided"}</span>
</form>
</body></html>"""


def _report_html(case_root: Path) -> str:
    return f"""<!doctype html>
<html><head><title>Calibration Report</title>{_PAGE_CSS}</head><body>
<p><a href='/'>&larr; back to list</a></p>
<h1>Calibration report</h1>
<pre>{html.escape(agreement_report(case_root))}</pre>
</body></html>"""


def make_handler(case_root: Path, sample: list[dict]) -> type[BaseHTTPRequestHandler]:
    """Build a handler class closed over case_root + sample."""

    class CalibrationHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            encoded = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            decisions = _load_decisions(case_root)
            if url.path == "/":
                self._send(200, _index_html(case_root, sample, decisions))
                return
            if url.path == "/report":
                self._send(200, _report_html(case_root))
                return
            if url.path.startswith("/review/"):
                ref = url.path[len("/review/") :]
                body = _review_html(case_root, sample, decisions, ref)
                if body is None:
                    self._send(404, "<h1>404 unknown ref</h1>")
                else:
                    self._send(200, body)
                return
            self._send(404, "<h1>404</h1>")

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if url.path != "/decision":
                self._send(404, "<h1>404</h1>")
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
            ref = form.get("ref", "")
            verdict = form.get("verdict", "")
            notes = form.get("notes", "")
            next_ref = form.get("next_ref", "")
            shown_at = form.get("shown_at", "")
            time_taken = 0.0
            if shown_at:
                try:
                    shown_dt = datetime.fromisoformat(shown_at.replace("Z", "+00:00"))
                    time_taken = (datetime.now(UTC) - shown_dt).total_seconds()
                except ValueError:
                    pass
            if not ref or verdict not in ("yes", "no", "uncertain"):
                self._send(400, "<h1>400 bad request</h1>")
                return
            _save_decision(
                case_root,
                ref=ref,
                verdict=verdict,
                notes=notes,
                decided_at=_iso_now(),
                time_taken_s=time_taken,
            )
            target = f"/review/{next_ref}" if next_ref else "/"
            self.send_response(303)
            self.send_header("Location", target)
            self.end_headers()

    return CalibrationHandler


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(
        prog="dsar-durant-calibration",
        description="Operator-calibration web portal for a DSAR Durant pass.",
    )
    p.add_argument(
        "--case-root",
        type=Path,
        default=None,
        help="Case directory. Defaults to $DSAR_CASE_ROOT or cwd.",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--sample-only", action="store_true")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    case_root = _resolve_case_root(args.case_root)
    paths = _paths(case_root)

    if args.report_only:
        print(agreement_report(case_root))
        return 0

    if not paths["sample_file"].exists() or args.sample_only:
        build_sample(case_root)
        if args.sample_only:
            return 0
    sample = json.loads(paths["sample_file"].read_text())

    handler_cls = make_handler(case_root, sample)
    server = ThreadingHTTPServer((HOST, args.port), handler_cls)
    log.info(
        "calibration portal on http://%s:%d/ (sample size %d)",
        HOST,
        args.port,
        len(sample),
    )
    log.info("decisions stream to: %s", paths["decisions_file"])
    log.info("Ctrl-C to stop. Reports at /report or via --report-only.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
