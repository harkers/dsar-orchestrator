"""DSAR conductor operator console — web UI for case review + sign-off.

Redesigned per a jury-of-LLMs design pass (chat + code-qwen25) on the
v1 console UX. Operator-first wording: 4 phases not 14 stages, plain
English, one primary call-to-action per page, blockers as the workhorse
checklist, internal state hidden behind a "details" toggle.

Single-file stdlib HTTP server. No broker calls from the console; all
write actions shell out to dsar-orchestrator / dsar-approver CLIs.

Usage:
    dsar-operator-console --case-dir <path> [--port 8089] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import shutil
import subprocess
import sys
import queue
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .local_broker.unextractable import (
    _CaseShim as _UnextCaseShim,
    list_unextractable as _list_unextractable,
    summary_counts as _unext_summary_counts,
    record_decision as _unext_record_decision,
    retry_extract as _unext_retry,
)
from .local_broker.leak_review import (
    _CaseShim as _LeakCaseShim,
    list_leaks as _list_leaks,
    summary_counts as _leak_summary_counts,
    record_decision as _leak_record_decision,
    retry_redaction as _leak_retry,
)
from .local_broker.closure_letter import (
    _CaseShim as _LetterCaseShim,
    draft_letter as _draft_letter,
    readiness_state as _letter_readiness_state,
)
from .local_broker.stage_summariser import (
    SummariserConfig,
    check_broker_eviction_risk,
    summarise_stage,
)

DEFAULT_PORT = 8089
DEFAULT_HOST = "127.0.0.1"
DEFAULT_ORCHESTRATOR_CLI = "dsar-orchestrator"

# Orchestrator state names (internal). Surface ONLY behind the "Show
# pipeline details" toggle. Operator-facing UI uses the four phases below.
STAGES = (
    "intake_created",
    "ingestion_running",
    "ingestion_qc_running",
    "dedupe_running",
    "context_running",
    "scope_check_running",
    "responsiveness_running",
    "redaction_running",
    "redaction_qc_a_running",
    "redaction_qc_b_running",
    "improvement_loop_running",
    "human_review_pending",
    "release_gate_running",
    "disclosure_pack_ready",
    "closed",
)

# Operator-facing 4-phase mapping. Each phase has friendly label + the
# internal stages that roll up into it.
PHASES: list[dict] = [
    {
        "key": "discovery",
        "label": "Discovery",
        "blurb": "Ingest the corpus, dedupe, classify each document.",
        "stages": [
            "intake_created",
            "ingestion_running",
            "ingestion_qc_running",
            "dedupe_running",
            "context_running",
        ],
    },
    {
        "key": "filter",
        "label": "Filter",
        "blurb": "Apply the Durant biographical-focus test + responsiveness rules to narrow the disclosure set.",
        "stages": ["scope_check_running", "responsiveness_running"],
    },
    {
        "key": "redact",
        "label": "Redact",
        "blurb": "Identify PII, run redaction, QC the over- and under-disclosure of each document.",
        "stages": [
            "redaction_running",
            "redaction_qc_a_running",
            "redaction_qc_b_running",
            "improvement_loop_running",
        ],
    },
    {
        "key": "release",
        "label": "Release & Sign-off",
        "blurb": "Operator review + DSAR Approver verdict + final disclosure pack.",
        "stages": [
            "human_review_pending",
            "release_gate_running",
            "disclosure_pack_ready",
            "closed",
        ],
    },
]

# Friendly stage labels (used inside the "Show pipeline details" panel
# and on per-stage drilldown headers).
STAGE_LABELS: dict[str, str] = {
    "intake_created": "Case opened",
    "ingestion_running": "Ingest source documents",
    "ingestion_qc_running": "Check ingest quality",
    "dedupe_running": "Remove duplicate documents",
    "context_running": "Classify document context",
    "scope_check_running": "Apply Durant focus test",
    "responsiveness_running": "Decide responsiveness",
    "redaction_running": "Redact third-party data",
    "redaction_qc_a_running": "Over-disclosure check",
    "redaction_qc_b_running": "Under-disclosure check",
    "improvement_loop_running": "Apply improvement decisions",
    "human_review_pending": "Awaiting your decision",
    "release_gate_running": "Release readiness review",
    "disclosure_pack_ready": "Disclosure pack ready",
    "closed": "Case closed",
}

# Per-stage artefact files (counted + shown on the "Show details" drilldown).
STAGE_ARTEFACTS: dict[str, list[str]] = {
    "ingestion_running": ["ingested_items.jsonl", "register.json"],
    "ingestion_qc_running": ["ingestion_qc_findings.jsonl"],
    "dedupe_running": ["dedupe_findings.jsonl"],
    "context_running": ["context_classifications.jsonl"],
    "scope_check_running": ["scope_verdicts.jsonl", "durant_verdicts.jsonl"],
    "responsiveness_running": ["responsiveness_decisions.jsonl"],
    "redaction_running": ["redaction_decisions.jsonl"],
    "redaction_qc_a_running": ["qc_findings_07a.jsonl"],
    "redaction_qc_b_running": [
        "qc_findings_07b.jsonl",
        "durant_underdisclosure_recheck.jsonl",
    ],
    "improvement_loop_running": ["improvement_decisions.jsonl"],
    "release_gate_running": [],
}

# Verdict copy: turn the raw Approver enum into operator-readable verdicts.
VERDICT_DISPLAY: dict[str, dict] = {
    "APPROVE_FOR_HUMAN_SIGNOFF": {
        "label": "Approved for sign-off",
        "icon": "✓",
        "css_class": "ok",
        "operator_meaning": "All checks pass. Ready for the human approver to sign and release.",
    },
    "APPROVE_WITH_CONDITIONS": {
        "label": "Approved with conditions",
        "icon": "⚠",
        "css_class": "warn",
        "operator_meaning": "Broadly safe; resolve the listed conditions then release.",
    },
    "REJECT": {
        "label": "Blocked",
        "icon": "✗",
        "css_class": "fail",
        "operator_meaning": "NOT safe to release. Work through the blockers, then re-check readiness.",
    },
    "ESCALATE_TO_DPO_OR_LEGAL": {
        "label": "Escalate to DPO / Legal",
        "icon": "↑",
        "css_class": "escalate",
        "operator_meaning": "Beyond operator authority. Hand to DPO / Legal for a release decision.",
    },
}

# Lock for write actions (prevents double-click races)
_ACTION_LOCK = threading.Lock()

# Background summary generator — single worker that consumes a queue of
# (stage, cfg) tuples and produces cached writer-model summaries. On first
# /pipeline GET we enqueue every stage missing a cached summary; the worker
# generates them one at a time so the page can render placeholders + meta-
# refresh until they fill in. One worker only — writer is a 70B model so
# loading it is the bottleneck; serialise broker calls to avoid swap thrash.
_SUMMARY_QUEUE: "queue.Queue[tuple[str, str, str, list[str]]]" = queue.Queue()
_SUMMARY_QUEUE_LOCK = threading.Lock()
_SUMMARY_ENQUEUED: set[str] = set()  # stage names currently queued or generating
_SUMMARY_WORKER_STARTED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("operator-console")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _human_ts(ts: str) -> str:
    """ISO timestamp → 'Mon 26 May 10:14' (24h, local-ish)."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%a %d %b %H:%M")
    except ValueError:
        return ts


def _reason_code_select_html() -> str:
    """A <select> with R001-R010 + R-PENDING. Required field; empty
    default forces operator to pick before submitting."""
    from dsar_orchestrator.local_broker.reason_codes import REASON_CODES

    opts = "".join(
        f"<option value='{code}'>{code} — {html.escape(entry['label'])}</option>"
        for code, entry in REASON_CODES.items()
    )
    return (
        "<select name='reason_code' required style='margin-right:4px;'>"
        "<option value=''>— reason —</option>"
        f"{opts}"
        "</select>"
    )


# ---------------------------------------------------------------------------
# State reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseContext:
    case_dir: Path

    @property
    def case_id(self) -> str:
        return self.case_dir.name

    @property
    def working(self) -> Path:
        return self.case_dir / "working"

    @property
    def audit(self) -> Path:
        return self.case_dir / "audit"

    @property
    def state_file(self) -> Path:
        return self.working / "orchestrator_state.json"

    @property
    def approver_decisions(self) -> Path:
        return self.audit / "approver-decisions.jsonl"

    @property
    def gate_decisions(self) -> Path:
        return self.audit / "gate-decisions.jsonl"

    @property
    def console_state(self) -> Path:
        return self.audit / "operator_console_state.json"

    @property
    def data_subject(self) -> Path:
        return self.working / "data_subject.json"

    @property
    def case_context(self) -> Path:
        return self.working / "case_context.json"


def load_orchestrator_state(ctx: CaseContext) -> dict:
    if not ctx.state_file.exists():
        return {"current_stage": "intake_created", "awaiting_operator_review": False, "history": []}
    try:
        return json.loads(ctx.state_file.read_text())
    except json.JSONDecodeError:
        return {"current_stage": "intake_created", "awaiting_operator_review": False, "history": []}


def load_case_metadata(ctx: CaseContext) -> dict:
    """Subject + context summary for the header strip."""
    out = {"subject_name": "(unknown)", "controller": "(unknown)", "deadline": None}
    if ctx.data_subject.exists():
        try:
            ds = json.loads(ctx.data_subject.read_text())
            out["subject_name"] = ds.get("full_name", "(unknown)")
        except json.JSONDecodeError:
            pass
    if ctx.case_context.exists():
        try:
            cc = json.loads(ctx.case_context.read_text())
            out["controller"] = cc.get("controller", "(unknown)")
            out["deadline"] = cc.get("response_deadline")
        except json.JSONDecodeError:
            pass
    return out


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.open() if line.strip())
    except OSError:
        return 0


def _file_size(path: Path) -> str:
    if not path.exists():
        return "-"
    try:
        n = path.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# --- Stage-rail enforcement ------------------------------------------------
#
# Operators cannot deep-link past the current phase. The jury's "pipeline
# integrity beats speed" principle: surfacing a Release-phase page while
# the case is still in Discovery hides which decisions still need to land
# in the earlier phase. Forward routes get hard-gated; read-only drilldowns
# (/pipeline, /audit, /file) stay open.
#
# Mapping intentionally key=route → required_phase_key (smallest phase that
# unlocks the route). Routes not in the dict are unguarded.

_PHASE_INDEX = {p["key"]: i for i, p in enumerate(PHASES)}

ROUTE_REQUIRED_PHASE: dict[str, str] = {
    "/unextractable": "discovery",
    "/leak-review": "redact",
    "/qa-sample": "redact",
    "/blockers": "release",
    "/release-check": "release",
    "/closure-letter": "release",
}

# Phase gating for dynamic-suffix routes. ``is_route_accessible`` consults
# this dict for any path that doesn't exact-match ROUTE_REQUIRED_PHASE.
ROUTE_PREFIX_REQUIRED_PHASE: dict[str, str] = {
    "/redaction-viewer/": "redact",
}


def current_phase_key(state: dict) -> str:
    """Return the phase key matching ``state['current_stage']``. Unknown
    stages fall back to the first phase ('discovery') with a warning —
    falling back silently would hide a state-file corruption from the
    operator until they wonder why every page is gated."""
    current = state.get("current_stage", "")
    for phase in PHASES:
        if current in phase["stages"]:
            return phase["key"]
    log.warning(
        "current_stage=%r not in any known phase; falling back to %s",
        current,
        PHASES[0]["key"],
    )
    return PHASES[0]["key"]


def is_route_accessible(state: dict, path: str) -> tuple[bool, str | None]:
    """Return ``(True, None)`` if the operator can reach ``path`` given the
    current pipeline state; ``(False, msg)`` if the route belongs to a
    later phase. ``msg`` names both the current and required phase so the
    redirect banner can be self-explanatory."""
    required_key = ROUTE_REQUIRED_PHASE.get(path)
    if not required_key:
        for prefix, key in ROUTE_PREFIX_REQUIRED_PHASE.items():
            if path.startswith(prefix):
                required_key = key
                break
    if not required_key:
        return True, None
    required_idx = _PHASE_INDEX[required_key]
    cur_key = current_phase_key(state)
    cur_idx = _PHASE_INDEX[cur_key]
    if cur_idx >= required_idx:
        return True, None
    cur_label = next(p["label"] for p in PHASES if p["key"] == cur_key)
    req_label = next(p["label"] for p in PHASES if p["key"] == required_key)
    return (
        False,
        f"'{req_label}' isn't reachable yet — case is in '{cur_label}'.",
    )


def phase_status(state: dict, phase: dict) -> str:
    """Returns 'done' / 'current' / 'pending' for the phase."""
    current = state["current_stage"]
    try:
        current_idx = STAGES.index(current)
    except ValueError:
        return "pending"
    phase_stage_idxs = [STAGES.index(s) for s in phase["stages"] if s in STAGES]
    if not phase_stage_idxs:
        return "pending"
    last_phase_idx = max(phase_stage_idxs)
    first_phase_idx = min(phase_stage_idxs)
    if current_idx < first_phase_idx:
        return "pending"
    if current_idx > last_phase_idx:
        return "done"
    return "current"


def pipeline_summary_numbers(ctx: CaseContext) -> dict:
    """Headline numbers shown on the landing page summary card."""
    ingested = _count_jsonl(ctx.working / "ingested_items.jsonl")
    redacted_decisions_path = ctx.working / "redaction_decisions.jsonl"
    redacted_count = 0
    failed_count = 0
    if redacted_decisions_path.exists():
        for line in redacted_decisions_path.open():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") == "redacted":
                redacted_count += 1
            elif r.get("status") == "failed":
                failed_count += 1
    durant_present = 0
    durant_path = ctx.working / "durant_verdicts.jsonl"
    if durant_path.exists():
        for line in durant_path.open():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("durant_verdict") == "biographical":
                durant_present += 1
    return {
        "source_files": ingested,
        "in_scope": durant_present,
        "redacted_documents": redacted_count,
        "leak_failures": failed_count,
    }


def latest_approver_verdict(ctx: CaseContext) -> dict | None:
    if not ctx.approver_decisions.exists():
        return None
    last = None
    with ctx.approver_decisions.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
    return last


# ---------------------------------------------------------------------------
# Portal-local state (blocker resolution)
# ---------------------------------------------------------------------------


def load_console_state(ctx: CaseContext) -> dict:
    if not ctx.console_state.exists():
        return {"resolved_blockers": {}, "operator_notes": []}
    try:
        return json.loads(ctx.console_state.read_text())
    except json.JSONDecodeError:
        return {"resolved_blockers": {}, "operator_notes": []}


def save_console_state(ctx: CaseContext, state: dict) -> None:
    with _ACTION_LOCK:
        ctx.console_state.parent.mkdir(parents=True, exist_ok=True)
        ctx.console_state.write_text(json.dumps(state, indent=2))


def toggle_blocker_resolved(
    ctx: CaseContext,
    blocker_id: str,
    *,
    resolved: bool,
    reason_code: str,
    note: str,
) -> dict:
    from dsar_orchestrator.local_broker.reason_codes import validate_reason_code

    validate_reason_code(reason_code, note)
    state = load_console_state(ctx)
    bks = state.setdefault("resolved_blockers", {})
    # Chain-first: if schema/IO breaks, state file isn't written either.
    from dsar_orchestrator.local_broker.audit_chain import (
        emit_failure_for_case_dir,
        emit_for_case_dir,
    )

    original_hash = emit_for_case_dir(
        ctx.case_dir,
        decision_kind="blocker_toggle",
        payload={
            "ts": _iso_now(),
            "blocker_id": blocker_id,
            "resolved": resolved,
            "reason_code": reason_code,
            "note": note,
        },
        item_id=blocker_id,
    )
    if resolved:
        bks[blocker_id] = {
            "resolved_at": _iso_now(),
            "reason_code": reason_code,
            "note": note,
        }
    else:
        bks.pop(blocker_id, None)
    try:
        save_console_state(ctx, state)
    except OSError as exc:
        emit_failure_for_case_dir(
            ctx.case_dir,
            decision_kind="blocker_toggle",
            payload={
                "phase": "post-chain-state-write",
                "original_event_hash": original_hash,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "target_path": str(ctx.console_state),
                "blocker_id": blocker_id,
            },
            item_id=blocker_id,
        )
        raise
    return state


def unresolved_blocker_summary(ctx: CaseContext) -> dict:
    """Return {total, open, open_critical, open_high} for the latest verdict."""
    last = latest_approver_verdict(ctx)
    if not last:
        return {"total": 0, "open": 0, "open_critical": 0, "open_high": 0, "exists": False}
    blocking = last.get("decision", {}).get("blocking_issues", [])
    resolved = load_console_state(ctx).get("resolved_blockers", {})
    open_blockers = [b for b in blocking if b.get("issue_id") not in resolved]
    return {
        "exists": True,
        "total": len(blocking),
        "open": len(open_blockers),
        "open_critical": sum(1 for b in open_blockers if b.get("severity") == "CRITICAL"),
        "open_high": sum(1 for b in open_blockers if b.get("severity") == "HIGH"),
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _run_cli(
    cmd: list[str], *, env_case_root: Path, input_bytes: bytes | None = None
) -> tuple[int, str, str]:
    import os

    env = os.environ.copy()
    env["DSAR_CASE_ROOT"] = str(env_case_root)
    try:
        result = subprocess.run(
            cmd,
            input=input_bytes,
            capture_output=True,
            env=env,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (124, "", "timeout (15 min)")
    except FileNotFoundError as exc:
        return (127, "", f"binary not found: {exc}")
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


def action_advance(ctx: CaseContext, *, orchestrator_cli: str, gate_after: bool = False) -> dict:
    cmd = [orchestrator_cli, "--case", ctx.case_id, "advance"]
    if gate_after:
        cmd.append("--gate-after")
    rc, out, err = _run_cli(cmd, env_case_root=ctx.case_dir.parent)
    return {"rc": rc, "stdout": out, "stderr": err, "command": " ".join(cmd)}


def action_clear_gate(ctx: CaseContext, *, orchestrator_cli: str) -> dict:
    cmd = [orchestrator_cli, "--case", ctx.case_id, "clear-gate"]
    rc, out, err = _run_cli(cmd, env_case_root=ctx.case_dir.parent)
    return {"rc": rc, "stdout": out, "stderr": err, "command": " ".join(cmd)}


def action_run_approver(ctx: CaseContext, *, approver_bin: str, approver_input_path: Path) -> dict:
    if not approver_input_path.exists():
        return {
            "rc": 2,
            "stdout": "",
            "stderr": f"approver input not found at {approver_input_path}",
            "command": "(skipped)",
        }
    cmd = [approver_bin, ctx.case_id]
    rc, out, err = _run_cli(
        cmd, env_case_root=ctx.case_dir.parent, input_bytes=approver_input_path.read_bytes()
    )
    return {"rc": rc, "stdout": out, "stderr": err, "command": " ".join(cmd)}


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


_BASE_CSS = """
<style>
:root { color-scheme: light; --max: 1000px;
        --blue: #0969da; --green: #1f883d; --red: #cf222e;
        --amber: #9a6700; --grey: #57606a; --bg: #f6f8fa;
        --line: #d0d7de; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: var(--max); margin: 0 auto; padding: 24px 20px;
       color: #1f2328; line-height: 1.45; }
h1 { font-size: 24px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 18px; font-weight: 600; margin: 24px 0 12px; }
h3 { font-size: 15px; font-weight: 600; margin: 20px 0 8px; }
p { margin: 0 0 12px; }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }

.case-header { padding: 12px 16px; background: var(--bg); border: 1px solid var(--line);
               border-radius: 8px; margin: 0 0 20px; display: flex;
               justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
.case-header b { font-size: 15px; }
.case-header .meta { color: var(--grey); font-size: 13px; }
.case-header nav a { margin-left: 16px; font-size: 13px; }

.phases { display: flex; gap: 8px; margin: 20px 0 28px; padding: 0; list-style: none; }
.phases li { flex: 1; padding: 14px 12px; border-radius: 6px; text-align: center;
             border: 1px solid var(--line); background: white; font-size: 13px;
             position: relative; }
.phases li.done { background: #dafbe1; border-color: var(--green); color: #0a4119; }
.phases li.current { background: #fff8c5; border-color: #d4a72c; color: #594b00;
                     font-weight: 600; box-shadow: 0 0 0 3px rgba(212,167,44,0.2); }
.phases li.pending { color: var(--grey); }
.phases li .step { display: block; font-size: 11px; text-transform: uppercase;
                   letter-spacing: 0.6px; margin-bottom: 2px; opacity: 0.7; }
.phases li .label { font-weight: 600; font-size: 14px; }

.decision-hero { padding: 20px; border-radius: 8px; margin: 20px 0;
                 border: 1px solid; }
.decision-hero.fail { background: #ffebe9; border-color: var(--red); }
.decision-hero.warn { background: #fff8c5; border-color: #d4a72c; }
.decision-hero.ok { background: #dafbe1; border-color: var(--green); }
.decision-hero.escalate { background: #fff1f7; border-color: #bf3989; }
.decision-hero.neutral { background: var(--bg); border-color: var(--line); }
.decision-hero .label { text-transform: uppercase; font-size: 11px;
                        letter-spacing: 0.8px; opacity: 0.7; margin-bottom: 4px; }
.decision-hero h2 { margin: 0 0 8px; font-size: 22px; }
.decision-hero .sub { font-size: 14px; margin-bottom: 16px; }

.btn { display: inline-block; padding: 10px 20px; border-radius: 6px;
       font-size: 14px; font-weight: 500; text-decoration: none; cursor: pointer;
       border: 1px solid; background: white; color: #1f2328; border-color: var(--line); }
.btn:hover { opacity: 0.85; text-decoration: none; }
.btn-primary { background: var(--blue); color: white; border-color: var(--blue); }
.btn-success { background: var(--green); color: white; border-color: var(--green); }
.btn-warn    { background: #bf8700; color: white; border-color: #bf8700; }
.btn-danger  { background: var(--red); color: white; border-color: var(--red); }
.btn-large   { padding: 12px 28px; font-size: 15px; }
form { display: inline; }

.card { border: 1px solid var(--line); border-radius: 8px; padding: 16px 20px; margin: 16px 0; background: white; }
.card h2 { margin-top: 0; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr));
                gap: 14px; margin-top: 10px; }
.summary-grid .stat { background: var(--bg); padding: 12px; border-radius: 6px;
                      border: 1px solid var(--line); }
.summary-grid .stat .n { font-size: 22px; font-weight: 600; display: block; }
.summary-grid .stat .label { color: var(--grey); font-size: 12px; }

details > summary { cursor: pointer; padding: 8px 0; color: var(--blue); }

table { border-collapse: collapse; width: 100%; margin: 10px 0; }
th, td { border: 1px solid var(--line); padding: 8px 12px; text-align: left;
         font-size: 13px; vertical-align: top; }
th { background: var(--bg); font-weight: 600; }
tr.done td { color: var(--grey); }

.blocker-card { border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px;
                margin: 12px 0; background: white; }
.blocker-card.crit { border-left: 4px solid var(--red); }
.blocker-card.high { border-left: 4px solid #d4a72c; }
.blocker-card.med  { border-left: 4px solid var(--blue); }
.blocker-card.low  { border-left: 4px solid var(--green); }
.blocker-card.resolved { background: #f6f8fa; opacity: 0.7; }
.blocker-card h3 { margin: 0 0 6px; font-size: 15px; }
.blocker-card .meta { color: var(--grey); font-size: 12px; margin: 0 0 6px; }
.blocker-card .req { font-size: 14px; margin: 8px 0; }
.blocker-card form { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
.blocker-card input[type=text] { flex: 1; padding: 8px; border: 1px solid var(--line); border-radius: 4px; }

.pill { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 11px;
        font-weight: 600; text-transform: uppercase; letter-spacing: 0.4px; }
.pill.crit { background: #ffebe9; color: var(--red); }
.pill.high { background: #fff1cc; color: var(--amber); }
.pill.med  { background: #ddf4ff; color: #0550ae; }
.pill.low  { background: #dafbe1; color: #0a4119; }
.pill.ok   { background: #dafbe1; color: var(--green); }
.pill.fail { background: #ffebe9; color: var(--red); }
.pill.np   { background: var(--bg); color: var(--grey); }
.pill.warn { background: #fff8c5; color: var(--amber); }

.action-result { padding: 12px 16px; border-radius: 6px; margin: 16px 0; border: 1px solid; font-size: 13px; }
.action-result.ok   { background: #dafbe1; border-color: var(--green); }
.action-result.fail { background: #ffebe9; border-color: var(--red); }

.rag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
       font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.rag-g { background: #dafbe1; color: var(--green); border: 1px solid var(--green); }
.rag-a { background: #fff8c5; color: var(--amber); border: 1px solid #d4a72c; }
.rag-r { background: #ffebe9; color: var(--red);   border: 1px solid var(--red); }
.action-result code { background: rgba(0,0,0,0.05); padding: 1px 6px; border-radius: 3px; }
.action-result pre { background: rgba(0,0,0,0.04); padding: 8px; border-radius: 4px;
                     font-size: 12px; max-height: 30vh; overflow: auto; }

footer { margin-top: 60px; padding: 16px 0; color: var(--grey); font-size: 12px;
         border-top: 1px solid var(--line); display: flex; gap: 16px; flex-wrap: wrap; }
</style>
"""


def _case_header(ctx: CaseContext, meta: dict) -> str:
    deadline = meta.get("deadline") or "NOT RECORDED"
    return (
        "<div class='case-header'>"
        "<div>"
        f"<b>{html.escape(meta.get('subject_name', '(unknown)'))}</b> "
        f"<span class='meta'>· {html.escape(meta.get('controller', '(unknown)'))} "
        f"· {html.escape(ctx.case_id)}</span>"
        f"<div class='meta'>Statutory deadline: <b>{html.escape(deadline)}</b></div>"
        "</div>"
        "<nav>"
        "<a href='/'>Home</a>"
        "<a href='/blockers'>Blockers</a><a href='/unextractable'>Unextractable</a><a href='/leak-review'>Leak review</a>"
        "<a href='/release-check'>Release readiness</a>"
        "<a href='/pipeline'>Pipeline details</a>"
        "</nav></div>"
    )


def _phase_strip(state: dict) -> str:
    lis = []
    for i, phase in enumerate(PHASES, start=1):
        status = phase_status(state, phase)
        icon = "✓" if status == "done" else "●" if status == "current" else "○"
        lis.append(
            f"<li class='{status}'><span class='step'>Phase {i}</span>"
            f"<span class='label'>{icon} {html.escape(phase['label'])}</span></li>"
        )
    return f"<ul class='phases'>{''.join(lis)}</ul>"


def _action_result_html(result: dict | None) -> str:
    if result is None:
        return ""
    cls = "ok" if result.get("rc") == 0 else "fail"
    return (
        f"<div class='action-result {cls}'>"
        f"<b>Last action:</b> <code>{html.escape(result.get('command', ''))}</code> "
        f"<i>(rc={result.get('rc', '?')})</i>"
        f"<details><summary>output</summary>"
        f"<pre>{html.escape(result.get('stdout', '')[:1800])}\n"
        f"--- stderr ---\n{html.escape(result.get('stderr', '')[:1500])}</pre>"
        f"</details></div>"
    )


def _footer(ctx: CaseContext) -> str:
    return (
        "<footer>"
        f"<span>Console state: <code>{html.escape(str(ctx.console_state))}</code></span>"
        f"<span>Audit log: <code>{html.escape(str(ctx.audit))}</code></span>"
        "<span><a href='/pipeline'>Pipeline details</a></span>"
        "</footer>"
    )


def _decision_hero(state: dict, ctx: CaseContext, summary: dict) -> str:
    """The hero block: 'what needs my decision right now?'"""
    current = state["current_stage"]
    gate_active = state.get("awaiting_operator_review", False)
    verdict = latest_approver_verdict(ctx)
    block = unresolved_blocker_summary(ctx)

    if gate_active:
        return (
            "<div class='decision-hero warn'>"
            "<div class='label'>Decision required</div>"
            f"<h2>Confirm '{html.escape(STAGE_LABELS.get(current, current))}' is complete</h2>"
            "<p class='sub'>The pipeline is paused at an operator checkpoint. Confirm "
            "you've reviewed the work above before it can advance to the next stage.</p>"
            "<form method='POST' action='/api/clear-gate' "
            "onsubmit=\"return confirm('Confirm this stage is reviewed and ready to advance?');\">"
            "<button class='btn btn-primary btn-large' type='submit'>"
            "Confirm this stage is ready</button></form>"
            " &nbsp; "
            "<a class='btn btn-large' href='/pipeline'>Inspect first</a>"
            "</div>"
        )

    if current == "closed":
        return (
            "<div class='decision-hero ok'>"
            "<div class='label'>Case closed</div>"
            "<h2>This case is closed</h2>"
            "<p class='sub'>The disclosure pack has been released and the audit chain is sealed.</p>"
            "</div>"
        )

    if current == "release_gate_running":
        if verdict is None:
            return (
                "<div class='decision-hero neutral'>"
                "<div class='label'>Release readiness</div>"
                "<h2>Run the release readiness check</h2>"
                "<p class='sub'>The pipeline is at the release gate. Run the DSAR Approver "
                "to get a structured verdict on whether the pack is safe to release.</p>"
                "<form method='POST' action='/api/run-approver' "
                "onsubmit=\"return confirm('Run the DSAR Approver against the current release pack?');\">"
                "<button class='btn btn-primary btn-large' type='submit'>"
                "Re-check release readiness</button></form>"
                "</div>"
            )
        d = verdict.get("decision", {})
        decision_code = d.get("decision", "REJECT")
        meta = VERDICT_DISPLAY.get(decision_code, VERDICT_DISPLAY["REJECT"])
        risk = d.get("risk_level", "?")
        ts = _human_ts(verdict.get("ts", ""))
        when = f" · {html.escape(ts)}" if ts else ""
        # Decide CTA based on blocker state
        if decision_code == "APPROVE_FOR_HUMAN_SIGNOFF":
            cta = (
                "<a class='btn btn-success btn-large' href='/release-check'>"
                "View readiness report</a>"
            )
        elif block["open"] > 0:
            crit_high = block["open_critical"] + block["open_high"]
            cta = (
                f"<a class='btn btn-primary btn-large' href='/blockers'>"
                f"Work through {block['open']} blocker(s)</a>"
            )
        else:
            cta = (
                "<form method='POST' action='/api/run-approver' "
                "onsubmit=\"return confirm('Re-run the Approver?');\">"
                "<button class='btn btn-primary btn-large' type='submit'>"
                "Re-check release readiness</button></form>"
            )
        return (
            f"<div class='decision-hero {meta['css_class']}'>"
            f"<div class='label'>Release readiness · {risk} risk</div>"
            f"<h2>{meta['icon']} {meta['label']}</h2>"
            f"<p class='sub'>{html.escape(meta['operator_meaning'])} {when}</p>"
            f"<p>{block['open']} open blocker(s) "
            f"({block['open_critical']} critical, {block['open_high']} high). "
            f"{cta}</p>"
            "</div>"
        )

    # Default: pipeline is mid-run, no operator decision needed
    return (
        "<div class='decision-hero neutral'>"
        "<div class='label'>Pipeline running</div>"
        f"<h2>In progress: {html.escape(STAGE_LABELS.get(current, current))}</h2>"
        "<p class='sub'>No operator decision needed right now. When this stage "
        "finishes, click 'Move to next phase' to advance.</p>"
        "<form method='POST' action='/api/advance' "
        "onsubmit=\"return confirm('Mark this stage complete and advance?');\">"
        "<button class='btn btn-primary btn-large' type='submit'>"
        "Move to next phase →</button></form>"
        "</div>"
    )


def _action_queue_html(ctx: CaseContext, state: dict) -> str:
    """Top-5 scored action items + 'Next Best Review' deep-link."""
    from dsar_orchestrator.local_broker.action_queue import scored_queue

    queue = scored_queue(ctx.case_dir, state)
    if not queue:
        return ""
    top = queue[0]
    rows: list[str] = []
    for i, s in enumerate(queue[:5], start=1):
        bd = s.breakdown
        sla_txt = (
            f"{bd['sla_days_remaining']}d to deadline"
            if bd["sla_days_remaining"] is not None
            else "—"
        )
        rows.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td><span class='pill'>{html.escape(s.item.kind)}</span></td>"
            f"<td><a href='{html.escape(s.item.detail_url)}'>{html.escape(s.item.label[:80])}</a></td>"
            f"<td>{s.score:.2f}</td>"
            f"<td><span class='meta' style='font-size:11px;'>risk {bd['risk']}/10 · {sla_txt}"
            f" · stage-pos {bd['stage_position']} · fatigue −{bd['fatigue_penalty']:.2f}"
            f" · div +{bd['diversity_bonus']:.2f}</span></td>"
            f"</tr>"
        )
    return (
        "<div class='card'>"
        "<h2>Action queue</h2>"
        f"<p>{len(queue)} pending decision(s). Sorted by risk × SLA × stage × fatigue × diversity.</p>"
        f"<p><a class='btn btn-primary' href='{html.escape(top.item.detail_url)}'>Next Best Review → "
        f"{html.escape(top.item.kind)}: {html.escape(top.item.label[:60])}</a></p>"
        "<table><thead><tr><th>#</th><th>Kind</th><th>Item</th><th>Score</th><th>Why</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</div>"
    )


def render_landing(ctx: CaseContext, state: dict, action_result: dict | None) -> str:
    meta = load_case_metadata(ctx)
    nums = pipeline_summary_numbers(ctx)
    return f"""<!doctype html>
<html><head><title>{html.escape(ctx.case_id)} — Operator console</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
{_phase_strip(state)}
{_action_result_html(action_result)}
{_decision_hero(state, ctx, nums)}
{_action_queue_html(ctx, state)}

<div class='card'>
<h2>Pipeline summary</h2>
<div class='summary-grid'>
<div class='stat'><span class='n'>{nums["source_files"]:,}</span><span class='label'>Source documents ingested</span></div>
<div class='stat'><span class='n'>{nums["in_scope"]:,}</span><span class='label'>In scope after Durant filter</span></div>
<div class='stat'><span class='n'>{nums["redacted_documents"]:,}</span><span class='label'>Documents redacted</span></div>
<div class='stat'><span class='n'>{nums["leak_failures"]:,}</span><span class='label'>Leak-check failures</span></div>
</div>
</div>

<details>
<summary>Show 14-stage pipeline detail</summary>
<p class='meta' style='color:var(--grey);font-size:12px;'>Internal orchestrator state. Operator-facing flow uses the 4 phases above.</p>
<table>
<thead><tr><th>Stage</th><th>State name</th><th>Status</th></tr></thead>
<tbody>{_pipeline_table_rows(ctx, state)}</tbody></table>
</details>

{_footer(ctx)}
</body></html>"""


def _pipeline_table_rows(ctx: CaseContext, state: dict) -> str:
    current = state["current_stage"]
    history_ts = {h["stage"]: h["ts"] for h in state.get("history", [])}
    rows = []
    seen_current = False
    for stage in STAGES:
        is_current = stage == current
        if is_current:
            seen_current = True
            status = "current"
        elif not seen_current:
            status = "done"
        else:
            status = "pending"
        cls = "done" if status == "done" else ""
        status_pill = {
            "done": "<span class='pill ok'>Done</span>",
            "current": "<span class='pill warn'>Current</span>",
            "pending": "<span class='pill np'>Pending</span>",
        }[status]
        ts = history_ts.get(stage, "")
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{html.escape(STAGE_LABELS.get(stage, stage))}<br>"
            f"<span class='meta' style='color:var(--grey);font-size:11px;'>"
            f"{html.escape(_human_ts(ts))}</span></td>"
            f"<td><code style='font-size:11px;'>{html.escape(stage)}</code></td>"
            f"<td>{status_pill}</td>"
            f"</tr>"
        )
    return "".join(rows)


def render_blockers(ctx: CaseContext, action_result: dict | None) -> str:
    meta = load_case_metadata(ctx)
    state = load_orchestrator_state(ctx)
    last = latest_approver_verdict(ctx)
    if not last:
        body = (
            "<div class='card'>"
            "<h2>No blockers yet</h2>"
            "<p>The DSAR Approver hasn't run on this case. Once the pipeline reaches "
            "the release gate and the Approver is invoked, any blocking issues it "
            "finds will appear here as a checklist.</p>"
            "</div>"
        )
    else:
        d = last.get("decision", {})
        blocking = d.get("blocking_issues", [])
        resolved = load_console_state(ctx).get("resolved_blockers", {})
        open_count = sum(1 for b in blocking if b.get("issue_id") not in resolved)
        decision_code = d.get("decision", "?")
        verdict_meta = VERDICT_DISPLAY.get(
            decision_code, {"icon": "?", "label": decision_code, "css_class": "neutral"}
        )
        ts = _human_ts(last.get("ts", ""))
        cards = []
        for b in blocking:
            bid = b.get("issue_id", "")
            sev = b.get("severity", "MEDIUM").lower()
            sev_cls = {"critical": "crit", "high": "high", "medium": "med", "low": "low"}.get(
                sev, "med"
            )
            is_resolved = bid in resolved
            cls = f"blocker-card {sev_cls}" + (" resolved" if is_resolved else "")
            resolved_note = resolved.get(bid, {}).get("note", "") if is_resolved else ""
            resolved_at_text = (
                f"<div class='meta'>Resolved {html.escape(_human_ts(resolved[bid].get('resolved_at', '')))}: "
                f"{html.escape(resolved.get(bid, {}).get('note', '(no note)'))}</div>"
                if is_resolved
                else ""
            )
            form_html = (
                "<form method='POST' action='/api/blocker/toggle'>"
                f"<input type='hidden' name='id' value='{html.escape(bid)}'>"
                f"<input type='hidden' name='resolved' value='{'0' if is_resolved else '1'}'>"
                f"{_reason_code_select_html()}"
                f"<input type='text' name='note' placeholder='How was this resolved? (note; required for R006/R010/R-PENDING)' "
                f"value='{html.escape(resolved_note)}'>"
                f"<button class='btn {'btn-warn' if is_resolved else 'btn-success'}' type='submit'>"
                f"{'Mark unresolved' if is_resolved else 'Mark resolved'}</button>"
                "</form>"
            )
            cards.append(
                f"<div class='{cls}'>"
                f"<h3>{'✓ ' if is_resolved else ''}{html.escape(b.get('issue', ''))} "
                f"<span class='pill {sev_cls}'>{html.escape(b.get('severity', '?'))}</span></h3>"
                f"<div class='meta'>Area: <b>{html.escape(b.get('area', ''))}</b> · "
                f"Owner: <b>{html.escape(b.get('owner', '(unassigned)'))}</b> · "
                f"<code style='font-size:11px;'>{html.escape(bid)}</code></div>"
                f"{resolved_at_text}"
                f"<div class='req'><b>To resolve:</b> {html.escape(b.get('required_action', ''))}</div>"
                f"{form_html}</div>"
            )
        ready_to_rerun = open_count == 0 and len(blocking) > 0
        rerun_box = ""
        if ready_to_rerun:
            rerun_box = (
                "<div class='decision-hero warn'>"
                "<div class='label'>All blockers cleared</div>"
                "<h2>Re-check release readiness</h2>"
                "<p class='sub'>You've marked every blocker resolved. Re-run the DSAR Approver "
                "so the release verdict reflects your work. Marking blockers resolved here is "
                "operator-local — the Approver verdict itself only updates when you re-run it.</p>"
                "<form method='POST' action='/api/run-approver' "
                "onsubmit=\"return confirm('Re-run the DSAR Approver?');\">"
                "<button class='btn btn-success btn-large' type='submit'>"
                "Re-check release readiness</button></form></div>"
            )
        body = (
            f"<div class='card'>"
            f"<h2>Release blockers · {open_count} of {len(blocking)} open</h2>"
            f"<p>Latest release verdict: <b>{verdict_meta['icon']} {html.escape(verdict_meta['label'])}</b> "
            f"({html.escape(ts)})</p>"
            f"<p class='meta'>Each blocker has an owner. Resolving here records that you've "
            f"addressed it — but the Approver verdict only updates when re-run.</p>"
            f"</div>"
            f"{rerun_box}"
            f"{''.join(cards)}"
        )
    return f"""<!doctype html>
<html><head><title>Blockers — {html.escape(ctx.case_id)}</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
<h1>What's blocking release</h1>
{_action_result_html(action_result)}
{body}
{_footer(ctx)}
</body></html>"""


def render_release_check(ctx: CaseContext, action_result: dict | None) -> str:
    meta = load_case_metadata(ctx)
    last = latest_approver_verdict(ctx)
    if not last:
        body = (
            "<div class='decision-hero neutral'>"
            "<div class='label'>Not yet run</div>"
            "<h2>No release readiness check has been done</h2>"
            "<p class='sub'>The DSAR Approver hasn't been invoked yet. Click below to run it "
            "against the current pack contents.</p>"
            "<form method='POST' action='/api/run-approver' "
            "onsubmit=\"return confirm('Run DSAR Approver now?');\">"
            "<button class='btn btn-primary btn-large' type='submit'>"
            "Run release readiness check</button></form></div>"
        )
    else:
        d = last.get("decision", {})
        decision_code = d.get("decision", "?")
        vmeta = VERDICT_DISPLAY.get(
            decision_code,
            {"icon": "?", "label": decision_code, "css_class": "neutral", "operator_meaning": ""},
        )
        ts = _human_ts(last.get("ts", ""))
        risk = d.get("risk_level", "?")
        summary = d.get("summary", "")
        reviewed = d.get("reviewed_areas", [])
        safety = d.get("release_safety_checks", {})
        rev_rows = []
        for a in reviewed:
            status = a.get("status", "")
            pill_cls = {
                "PASS": "ok",
                "PASS_WITH_NOTE": "ok",
                "FAIL": "fail",
                "NOT_PROVIDED": "np",
                "ESCALATE": "warn",
            }.get(status, "np")
            rev_rows.append(
                f"<tr><td>{html.escape(a.get('area', ''))}</td>"
                f"<td><span class='pill {pill_cls}'>{html.escape(status)}</span></td>"
                f"<td>{html.escape(a.get('notes', ''))}</td></tr>"
            )
        safety_rows = []
        for k, v in sorted(safety.items()):
            pill_cls = "ok" if v == "YES" else "fail" if v == "NO" else "np"
            safety_rows.append(
                f"<tr><td>{html.escape(k.replace('_', ' '))}</td>"
                f"<td><span class='pill {pill_cls}'>{html.escape(str(v))}</span></td></tr>"
            )
        block = unresolved_blocker_summary(ctx)
        body = (
            f"<div class='decision-hero {vmeta['css_class']}'>"
            f"<div class='label'>Verdict · {risk} risk · {html.escape(ts)}</div>"
            f"<h2>{vmeta['icon']} {html.escape(vmeta['label'])}</h2>"
            f"<p class='sub'>{html.escape(vmeta['operator_meaning'])}</p>"
            f"<p>{block['open']} of {block['total']} blockers open. "
            f"<a class='btn' href='/blockers'>Work through blockers</a> &nbsp; "
            "<form method='POST' action='/api/run-approver' "
            "onsubmit=\"return confirm('Re-run the Approver?');\" style='display:inline;'>"
            "<button class='btn btn-primary' type='submit'>Re-check now</button></form></p>"
            f"</div>"
            f"<div class='card'><h2>Summary from Approver</h2><p>{html.escape(summary)}</p></div>"
            f"<div class='card'><h2>Per-area review ({len(reviewed)} areas)</h2>"
            f"<table><thead><tr><th>Area</th><th>Status</th><th>Notes</th></tr></thead>"
            f"<tbody>{''.join(rev_rows)}</tbody></table></div>"
            f"<div class='card'><h2>Release safety checks</h2>"
            f"<table><thead><tr><th>Check</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(safety_rows)}</tbody></table></div>"
            f"<details><summary>Raw verdict JSON</summary>"
            f"<pre>{html.escape(json.dumps(d, indent=2))}</pre></details>"
        )
    return f"""<!doctype html>
<html><head><title>Release readiness — {html.escape(ctx.case_id)}</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
<h1>Release readiness</h1>
{_action_result_html(action_result)}
{body}
{_footer(ctx)}
</body></html>"""


RAG_PILL: dict[str, str] = {
    "G": "<span class='rag rag-g' title='Green: stage clean'>● Green</span>",
    "A": "<span class='rag rag-a' title='Amber: review-needed'>● Amber</span>",
    "R": "<span class='rag rag-r' title='Red: blocking'>● Red</span>",
}


def _phase_label_for_stage(stage: str) -> str:
    for ph in PHASES:
        if stage in ph["stages"]:
            return ph["label"]
    return ""


def _load_cached_summaries(ctx: CaseContext) -> dict[str, dict]:
    """Read audit/stage_summaries.jsonl latest-per-stage. Empty if missing."""
    path = ctx.audit / "stage_summaries.jsonl"
    if not path.exists():
        return {}
    by_stage: dict[str, dict] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("stage"):
                by_stage[r["stage"]] = r
    return by_stage


def _summary_worker_loop() -> None:
    """Module-level worker. Drains _SUMMARY_QUEUE, generating one summary
    at a time via summarise_stage(). Started lazily on first /pipeline GET."""
    while True:
        try:
            stage, stage_label, phase_label, art_names, case_dir_str, status = _SUMMARY_QUEUE.get(
                timeout=0.5
            )
        except queue.Empty:
            continue
        try:
            cfg = SummariserConfig(case_dir=Path(case_dir_str))
            log.info("summary worker: generating for %s", stage)
            summarise_stage(
                cfg,
                stage=stage,
                stage_label=stage_label,
                phase_label=phase_label,
                status=status,
                stage_artefact_names=art_names,
                force_refresh=False,
            )
        except Exception as exc:
            log.warning("summary worker: %s for %s: %s", type(exc).__name__, stage, exc)
        finally:
            with _SUMMARY_QUEUE_LOCK:
                _SUMMARY_ENQUEUED.discard(stage)
            _SUMMARY_QUEUE.task_done()


def _start_summary_worker_if_needed() -> None:
    global _SUMMARY_WORKER_STARTED
    if _SUMMARY_WORKER_STARTED:
        return
    with _SUMMARY_QUEUE_LOCK:
        if _SUMMARY_WORKER_STARTED:
            return
        t = threading.Thread(target=_summary_worker_loop, daemon=True, name="summary-worker")
        t.start()
        _SUMMARY_WORKER_STARTED = True


def _enqueue_missing_summaries(ctx: CaseContext, state: dict) -> int:
    """For each stage with artefacts and no cache hit, enqueue it for
    background summary generation. Returns the number enqueued."""
    summaries = _load_cached_summaries(ctx)
    stage_flows = _stage_flow_block(ctx)
    current = state["current_stage"]
    history_ts = {h["stage"]: h["ts"] for h in state.get("history", [])}
    enqueued = 0
    for stage in STAGES:
        art_names = STAGE_ARTEFACTS.get(stage, [])
        if not art_names:
            continue  # nothing to summarise for stages with no artefacts
        # Skip if any artefact missing (stage hasn't produced output yet)
        if not any((ctx.working / a).exists() for a in art_names):
            continue
        if stage in summaries:
            continue  # have cached summary
        with _SUMMARY_QUEUE_LOCK:
            if stage in _SUMMARY_ENQUEUED:
                continue
            _SUMMARY_ENQUEUED.add(stage)
        status = "current" if stage == current else "done" if history_ts.get(stage) else "pending"
        _SUMMARY_QUEUE.put(
            (
                stage,
                STAGE_LABELS.get(stage, stage),
                _phase_label_for_stage(stage),
                art_names,
                str(ctx.case_dir),
                status,
            )
        )
        enqueued += 1
    if enqueued > 0:
        _start_summary_worker_if_needed()
    return enqueued


def _dedupe_findings_block(ctx: CaseContext) -> str:
    """Render a dedicated findings block for the dedupe stage with the
    actual canonical/duplicate counts + breakdown by basis. Shown above
    the LLM summary on the dedupe stage card."""
    path = ctx.working / "dedupe_findings.jsonl"
    if not path.exists():
        return ""
    from collections import Counter

    verdicts = Counter()
    bases = Counter()
    dup_bases = Counter()
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                v = r.get("dedupe_verdict", "?")
                b = r.get("dedupe_basis", "?")
                verdicts[v] += 1
                bases[b] += 1
                if v == "duplicate":
                    dup_bases[b] += 1
    except OSError:
        return ""
    total = sum(verdicts.values())
    canonical = verdicts.get("canonical", 0)
    duplicate = verdicts.get("duplicate", 0)
    if total == 0:
        return ""
    pct_saved = (100.0 * duplicate / total) if total else 0
    # Top dup-bases
    msg_dups = dup_bases.get("message_id", 0)
    sha_dups = dup_bases.get("sha256", 0)
    return (
        "<div style='background:#dafbe1;border:1px solid #4ac26b;"
        "border-radius:6px;padding:12px 14px;margin:10px 0;'>"
        f"<b>● Dedupe findings:</b> {canonical:,} unique canonical / "
        f"{duplicate:,} duplicates "
        f"({pct_saved:.0f}% of corpus collapsed). "
        f"<br><span class='meta'>By basis: "
        f"<b>{msg_dups:,}</b> cross-mailbox dups caught via Message-ID; "
        f"<b>{sha_dups:,}</b> byte-identical via SHA-256. "
        f"Downstream stages can filter to canonical-only for ~{pct_saved:.0f}% LLM savings."
        "</span></div>"
    )


def _stage_flow_block(ctx: CaseContext) -> dict[str, str]:
    """Per-stage 'data flow' block. Hand-coded per stage to read the
    relevant artefact and show: what came in, what went out, what was
    removed/found. Returns a dict {stage_name: html} for stages that
    have meaningful flow data; missing stages return ''."""
    from collections import Counter

    def _read_jsonl(name: str) -> list[dict]:
        p = ctx.working / name
        if not p.exists():
            return []
        out = []
        try:
            with p.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return out

    def _fmt(label: str, value: object) -> str:
        if isinstance(value, int):
            value = f"{value:,}"
        return f"<b>{label}:</b> {value}"

    def _wrap(items: list[str]) -> str:
        if not items:
            return ""
        return (
            "<div style='background:#ddf4ff;border:1px solid #54aeff;"
            "border-radius:6px;padding:10px 14px;margin:6px 0;font-size:13px;'>"
            + " &nbsp; · &nbsp; ".join(items)
            + "</div>"
        )

    flows: dict[str, str] = {}

    # Stage 1: ingestion
    ingested = _read_jsonl("ingested_items.jsonl")
    if ingested:
        flows["ingestion_running"] = _wrap(
            [
                _fmt("Source files attempted", "4,675 (from agent01_input.jsonl)"),
                _fmt("Successfully ingested", len(ingested)),
                _fmt("Failed extraction", 4675 - len(ingested)),
                _fmt("Register entries", len(ingested)),
            ]
        )

    # Stage 2: ingestion QC
    qc = _read_jsonl("ingestion_qc_findings.jsonl")
    if qc or (ctx.working / "ingestion_qc_findings.jsonl").exists():
        sev = Counter(r.get("severity", "?") for r in qc)
        flows["ingestion_qc_running"] = _wrap(
            [
                _fmt("Records checked", len(ingested)),
                _fmt("Findings", len(qc)),
                _fmt(
                    "Severity",
                    f"high={sev.get('high', 0)} / medium={sev.get('medium', 0)} "
                    f"/ low={sev.get('low', 0)} / critical={sev.get('critical', 0)}",
                ),
            ]
        )

    # Stage 3: dedupe (handled separately by _dedupe_findings_block; we'll
    # not duplicate it here)

    # Stage 4: context classify
    ctx_class = _read_jsonl("context_classifications.jsonl")
    if ctx_class:
        iar = Counter(r.get("is_about_requester", "?") for r in ctx_class)
        flows["context_running"] = _wrap(
            [
                _fmt("Records classified", len(ctx_class)),
                _fmt(
                    "is_about_requester",
                    f"yes={iar.get('yes', 0):,} / partial={iar.get('partial', 0):,} "
                    f"/ no={iar.get('no', 0):,} / unclear={iar.get('unclear', 0):,}",
                ),
            ]
        )

    # Stage 5: scope_check (Durant)
    durant = _read_jsonl("durant_verdicts.jsonl")
    if durant:
        d = Counter(r.get("durant_verdict", "?") for r in durant)
        bio = d.get("biographical", 0)
        wco = d.get("work_context_only", 0)
        amb = d.get("ambiguous", 0)
        tot = bio + wco + amb
        pct_kept = (100.0 * bio / tot) if tot else 0
        flows["scope_check_running"] = _wrap(
            [
                _fmt("Records evaluated", tot),
                _fmt("biographical (in scope)", bio),
                _fmt("work_context_only (excluded)", wco),
                _fmt("ambiguous", amb),
                f"<b>Filter kept</b>: {pct_kept:.0f}% for redaction",
            ]
        )

    # Stage 6: responsiveness
    resp = _read_jsonl("responsiveness_decisions.jsonl")
    if resp:
        disp = Counter(r.get("disposition", "?") for r in resp)
        review = sum(1 for r in resp if r.get("requires_human_review"))
        flows["responsiveness_running"] = _wrap(
            [
                _fmt("Records evaluated", len(resp)),
                _fmt("included", disp.get("included", 0)),
                _fmt("excluded", disp.get("excluded", 0)),
                _fmt("requires_human_review", review),
            ]
        )

    # Stage 7: redaction
    red = _read_jsonl("redaction_decisions.jsonl")
    if red:
        s = Counter(r.get("status", "?") for r in red)
        total_redactions = sum(r.get("redaction_count", 0) for r in red)
        flows["redaction_running"] = _wrap(
            [
                _fmt("Documents attempted", len(red)),
                _fmt("Successfully redacted", s.get("redacted", 0)),
                _fmt("Failed (leak detection)", s.get("failed", 0)),
                _fmt("PII redactions applied", total_redactions),
            ]
        )

    # Stage 8: redaction_qc_a (over-disclosure)
    qc_a = _read_jsonl("qc_findings_07a.jsonl")
    if qc_a:
        flows["redaction_qc_a_running"] = _wrap(
            [
                _fmt("Over-disclosure candidates flagged", len(qc_a)),
                _fmt("All severity", "high (all rows)"),
            ]
        )

    # Stage 9: redaction_qc_b (under-disclosure / custom recheck)
    recheck = _read_jsonl("durant_underdisclosure_recheck.jsonl")
    if recheck:
        r = Counter(rr.get("recheck_verdict", "?") for rr in recheck)
        flows["redaction_qc_b_running"] = _wrap(
            [
                _fmt("Excluded docs rechecked", len(recheck)),
                _fmt("Confirmed excluded", r.get("confirmed_work_context_only", 0)),
                _fmt(
                    "Recheck wants reclassify (biographical)",
                    r.get("reclassify_to_biographical", 0),
                ),
                _fmt("Ambiguous (operator review)", r.get("reclassify_to_ambiguous", 0)),
                "<i>Calibration: original Durant 100% / recheck 33% on 30-doc sample — "
                "<b>recheck verdicts discarded as noise</b></i>",
            ]
        )

    # Stage 10: improvement
    imp = _read_jsonl("improvement_decisions.jsonl")
    if imp:
        p = Counter(r.get("proposed_change", "?") for r in imp)
        flows["improvement_loop_running"] = _wrap(
            [
                _fmt("Improvement proposals", len(imp)),
                *[f"<b>{k}:</b> {v:,}" for k, v in p.most_common(5)],
            ]
        )

    # Stage 12: release gate (approver)
    last_app = latest_approver_verdict(ctx)
    if last_app:
        d = last_app.get("decision", {})
        decision = d.get("decision", "?")
        risk = d.get("risk_level", "?")
        blockers = d.get("blocking_issues", [])
        flows["release_gate_running"] = _wrap(
            [
                _fmt("Latest verdict", f"{decision} ({risk} risk)"),
                _fmt("Blocking issues", len(blockers)),
                _fmt("Open critical", sum(1 for b in blockers if b.get("severity") == "CRITICAL")),
                _fmt("Open high", sum(1 for b in blockers if b.get("severity") == "HIGH")),
            ]
        )

    return flows


def render_pipeline_details(ctx: CaseContext, action_result: dict | None) -> str:
    meta = load_case_metadata(ctx)
    state = load_orchestrator_state(ctx)
    current = state["current_stage"]
    history_ts = {h["stage"]: h["ts"] for h in state.get("history", [])}
    # Kick off auto-summary generation for any stage that has artefacts
    # but no cached summary. Non-blocking.
    enqueued_now = _enqueue_missing_summaries(ctx, state)
    summaries = _load_cached_summaries(ctx)
    stage_flows = _stage_flow_block(ctx)
    pending_count = 0
    with _SUMMARY_QUEUE_LOCK:
        pending_count = len(_SUMMARY_ENQUEUED)
    cards = []
    for phase in PHASES:
        status = phase_status(state, phase)
        rows = []
        for s in phase["stages"]:
            ts = history_ts.get(s, "")
            is_curr = s == current
            arts = STAGE_ARTEFACTS.get(s, [])
            art_strs = []
            for name in arts:
                p = ctx.working / name
                if p.exists():
                    art_strs.append(
                        f"<a href='/file?path={html.escape(urllib.parse.quote(str(p)))}'>"
                        f"{html.escape(name)}</a> "
                        f"<span class='meta'>({_count_jsonl(p):,} rows)</span>"
                    )
                else:
                    art_strs.append(f"<span class='meta'>{html.escape(name)} (not present)</span>")
            status_cell = (
                "<span class='pill warn'>Current</span>"
                if is_curr
                else (
                    "<span class='pill ok'>Done</span>"
                    if ts
                    else "<span class='pill np'>Pending</span>"
                )
            )
            summary = summaries.get(s)
            rag_html = ""
            findings_html = ""
            # Stage-specific findings block: dedupe gets a green canonical/dup
            # breakdown; other stages get a blue numeric "flow" block from
            # _stage_flow_block (input -> output -> what was removed/found).
            if s == "dedupe_running":
                findings_html = _dedupe_findings_block(ctx)
            else:
                findings_html = stage_flows.get(s, "")
            # LLM summary block (auto-generated; placeholder if pending)
            summary_block = ""
            if summary:
                rag_html = RAG_PILL.get(summary.get("rag", "?"), "")
                summary_block = (
                    f"<div style='background:#f6f8fa;padding:10px 14px;"
                    f"border-left:3px solid #d0d7de;margin:6px 0;border-radius:0 4px 4px 0;'>"
                    f"<i>{html.escape(summary.get('summary', ''))}</i><br>"
                    f"<span class='meta'>{rag_html} &nbsp; "
                    f"<small>{html.escape(summary.get('reasoning', ''))} · "
                    f"generated {html.escape(_human_ts(summary.get('ts', '')))}</small></span></div>"
                )
            elif arts and any((ctx.working / a).exists() for a in arts):
                summary_block = (
                    "<div style='background:#fff8c5;padding:10px 14px;"
                    "border-left:3px solid #d4a72c;margin:6px 0;border-radius:0 4px 4px 0;'>"
                    "<i>Generating summary… (refreshes automatically)</i></div>"
                )
            # RAG pill lives only in the summary block below — not duplicated in the label.
            art_join = "<br>".join(art_strs) if art_strs else "<span class='meta'>—</span>"
            rows.append(
                "<tr>"
                f"<td>{html.escape(STAGE_LABELS.get(s, s))}</td>"
                f"<td>{status_cell}</td>"
                f"<td>{html.escape(_human_ts(ts))}</td>"
                f"<td>{art_join}</td></tr>"
            )
            # Per-step findings + summary row spans all 4 cols below the step
            if findings_html or summary_block:
                rows.append(
                    f"<tr><td colspan='4' style='padding:6px 12px;'>"
                    f"{findings_html}{summary_block}</td></tr>"
                )
        phase_status_pill = {
            "done": "<span class='pill ok'>Complete</span>",
            "current": "<span class='pill warn'>In progress</span>",
            "pending": "<span class='pill np'>Not started</span>",
        }[status]
        cards.append(
            f"<div class='card'>"
            f"<h2>{html.escape(phase['label'])} &nbsp; {phase_status_pill}</h2>"
            f"<p class='meta'>{html.escape(phase['blurb'])}</p>"
            "<table><thead><tr><th>Step</th><th>Status</th><th>Entered</th>"
            "<th>Artefacts</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    # Action buttons
    actions = []
    if state.get("awaiting_operator_review"):
        actions.append(
            "<form method='POST' action='/api/clear-gate' "
            "onsubmit=\"return confirm('Confirm this stage is ready to advance?');\">"
            "<button class='btn btn-warn btn-large' type='submit'>"
            "Confirm current stage is ready</button></form>"
        )
    if current != "closed":
        actions.append(
            "<form method='POST' action='/api/advance' "
            "onsubmit=\"return confirm('Mark current stage complete and advance?');\">"
            "<button class='btn btn-primary btn-large' type='submit'>"
            "Move to next stage →</button></form>"
        )
    # Meta-refresh while summaries are still generating
    meta_refresh = ""
    summary_status = ""
    if pending_count > 0:
        meta_refresh = "<meta http-equiv='refresh' content='8'>"
        summary_status = (
            f"<div class='banner info' style='margin-top:0;'>"
            f"Auto-generating stage summaries via broker writer model "
            f"({pending_count} pending). Page refreshes every 8s.</div>"
        )
    return f"""<!doctype html>
<html><head><title>Pipeline details — {html.escape(ctx.case_id)}</title>{meta_refresh}{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
<h1>Pipeline details</h1>
<p class='meta'>Drilldown into the 14 internal stages, grouped into the 4 operator-facing phases.
Summaries below each stage are auto-generated by the local writer model on first visit.</p>
{summary_status}
{_action_result_html(action_result)}
<p>{" ".join(actions)}</p>
{"".join(cards)}
{_footer(ctx)}
</body></html>"""


def render_closure_letter(ctx: CaseContext) -> str:
    """Render the auto-drafted closure letter as HTML."""
    meta = load_case_metadata(ctx)
    shim = _LetterCaseShim(case_dir=ctx.case_dir)
    state = _letter_readiness_state(shim)
    markdown = _draft_letter(shim)
    # Minimal markdown → HTML: code blocks not needed, just paragraphs + tables
    # Convert tables; convert headings; preserve <code> + <b>
    import re as _re

    h = html.escape(markdown)
    # Restore the existing markdown markers we want to render as HTML
    # Headings
    h = _re.sub(r"^# (.+)$", r"<h1>\\1</h1>", h, flags=_re.M)
    h = _re.sub(r"^## (.+)$", r"<h2>\\1</h2>", h, flags=_re.M)
    h = _re.sub(r"^### (.+)$", r"<h3>\\1</h3>", h, flags=_re.M)
    # Bold
    h = _re.sub(r"\\*\\*(.+?)\\*\\*", r"<b>\\1</b>", h)
    # Blockquotes
    h = _re.sub(
        r"^&gt; (.+)$",
        r'<blockquote style="margin:8px 0;border-left:3px solid #54aeff;padding:4px 16px;background:#f6f8fa;">\\1</blockquote>',
        h,
        flags=_re.M,
    )
    # Tables: leave as <pre> since they're complex; wrap whole letter in <pre> for raw view
    # Actually render the markdown as <pre> for simplicity + reliability
    # Banner status
    banner_meaning = {
        "ready_approved": ("<span class='pill ok'>✓ Approved — ready for sign-off</span>"),
        "ready_with_conditions": ("<span class='pill med'>⚠ Approved with conditions</span>"),
        "not_ready_blocked": (
            "<span class='pill fail'>✗ Blocked — Approver returned REJECT</span>"
        ),
        "escalate": ("<span class='pill high'>↑ Escalate to DPO/Legal</span>"),
        "case_closed": ("<span class='pill ok'>✓ Case closed</span>"),
        "not_ready_no_approver": ("<span class='pill np'>Approver not run yet</span>"),
    }.get(state, "<span class='pill np'>—</span>")
    return f"""<!doctype html>
<html><head><title>Closure letter draft — {html.escape(ctx.case_id)}</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
<h1>Closure letter — auto-drafted</h1>
<p class='meta'>Auto-generated from case state. Status: {banner_meaning} &nbsp;
<a href='/release-check'>View Approver verdict</a> &nbsp; · &nbsp;
<a href='/blockers'>Work through blockers</a></p>
<pre style="white-space:pre-wrap;background:#fff;border:1px solid #d0d7de;padding:18px;font-family:-apple-system,sans-serif;font-size:14px;line-height:1.5;">{html.escape(markdown)}</pre>
{_footer(ctx)}
</body></html>"""


def render_qa_sample(ctx: CaseContext, action_result: dict | None) -> str:
    """30-doc QA sample table with per-doc decision form."""
    from dsar_orchestrator.local_broker.qa_sample import (
        list_qa_sample,
        qa_sample_complete,
        summary_counts,
    )

    meta = load_case_metadata(ctx)
    rows = list_qa_sample(ctx.case_dir)
    counts = summary_counts(ctx.case_dir)
    complete = qa_sample_complete(ctx.case_dir)
    bucket_pill = {
        "high": "<span class='pill fail'>HIGH</span>",
        "medium": "<span class='pill warn'>MED</span>",
        "random": "<span class='pill np'>RAND</span>",
    }
    decision_pill = {
        "pending": "<span class='pill np'>Pending</span>",
        "approve": "<span class='pill ok'>Approved</span>",
        "request_reredaction": "<span class='pill warn'>Re-redact requested</span>",
        "mark_false_positive": "<span class='pill warn'>False positive</span>",
        "mark_missed_redaction": "<span class='pill fail'>Missed redaction</span>",
        "escalate": "<span class='pill fail'>Escalated</span>",
    }
    table_rows: list[str] = []
    for r in rows:
        ref = html.escape(r["doc_ref"])
        form = (
            "<form method='POST' action='/api/qa-sample/decide' style='display:inline;'>"
            f"<input type='hidden' name='doc_ref' value='{ref}'>"
            "<select name='decision' required style='margin-right:4px;'>"
            "<option value=''>— decision —</option>"
            "<option value='approve'>Approve</option>"
            "<option value='request_reredaction'>Request re-redaction</option>"
            "<option value='mark_false_positive'>Mark false positive</option>"
            "<option value='mark_missed_redaction'>Mark missed redaction</option>"
            "<option value='escalate'>Escalate</option>"
            "</select>"
            f"{_reason_code_select_html()}"
            "<input type='text' name='note' placeholder='note (required for R006/R010/R-PENDING)' style='width:200px;'>"
            "<button class='btn btn-primary' type='submit'>Record</button>"
            "</form>"
        )
        rc_badge = (
            f"<span class='pill'>{html.escape(r['reason_code'])}</span> "
            if r["reason_code"]
            else ""
        )
        note_html = (
            f"<br><span class='meta'>{rc_badge}{html.escape(r['note'])}"
            f" · {html.escape(_human_ts(r['ts']))}</span>"
            if r["note"] or r["ts"] or r["reason_code"]
            else ""
        )
        table_rows.append(
            f"<tr>"
            f"<td>{bucket_pill.get(r['bucket'], r['bucket'])}</td>"
            f"<td><code>{html.escape(r['filename'][:60])}</code><br>"
            f"<span class='meta'>ents {r['entity_count']} · redactions {r['redact_count']}</span></td>"
            f"<td>{decision_pill.get(r['decision'], r['decision'])}{note_html}</td>"
            f"<td>{form}</td>"
            f"</tr>"
        )
    complete_banner = (
        "<p class='pill ok' style='padding:8px;'>"
        "✓ Stage complete — all sampled docs have a final decision."
        "</p>"
        if complete
        else f"<p class='pill warn' style='padding:8px;'>"
        f"{counts.get('pending', 0)} of {counts.get('total', 0)} sampled docs still pending."
        "</p>"
    )
    return f"""<!doctype html>
<html><head><title>30-Doc QA — {html.escape(ctx.case_id)}</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
{_action_result_html(action_result)}
<h1>30-Doc QA sample</h1>
<p>Stratified sample: 10 high-risk + 10 medium + 10 random. Stage doesn't pass until every doc has a final decision.</p>
{complete_banner}
<table><thead><tr><th>Bucket</th><th>Document</th><th>Status</th><th>Decide</th></tr></thead>
<tbody>{"".join(table_rows)}</tbody></table>
{_footer(ctx)}
</body></html>"""


def render_unextractable(ctx: CaseContext, action_result: dict | None) -> str:
    """List unextractable docs with accept/reject/retry buttons."""
    meta = load_case_metadata(ctx)
    shim = _UnextCaseShim(case_dir=ctx.case_dir)
    items = _list_unextractable(shim)
    counts = _unext_summary_counts(shim)
    decision_pill = {
        "pending": "<span class='pill np'>Pending</span>",
        "accept": "<span class='pill ok'>Accepted (documented exclusion)</span>",
        "reject": "<span class='pill fail'>Rejected (out of scope)</span>",
        "retried_ok": "<span class='pill ok'>Retried — success</span>",
        "retried_fail": "<span class='pill fail'>Retried — still failed</span>",
    }
    rows = []
    for it in items:
        ref = html.escape(it["source_path"])
        # Forms: accept / reject / retry
        actions = (
            "<form method='POST' action='/api/unextractable/decide' style='display:inline;'>"
            f"<input type='hidden' name='source_path' value='{ref}'>"
            f"<input type='hidden' name='decision' value='accept'>"
            f"{_reason_code_select_html()}"
            "<input type='text' name='note' placeholder='note (required for R006/R010/R-PENDING)' style='width:200px;'>"
            "<button class='btn btn-success' type='submit' title='Documented exclusion'>Accept</button></form>"
            " "
            "<form method='POST' action='/api/unextractable/decide' style='display:inline;'>"
            f"<input type='hidden' name='source_path' value='{ref}'>"
            f"<input type='hidden' name='decision' value='reject'>"
            f"{_reason_code_select_html()}"
            "<input type='text' name='note' placeholder='note (required for R006/R010/R-PENDING)' style='width:200px;'>"
            "<button class='btn btn-danger' type='submit' title='Out of scope'>Reject</button></form>"
            " "
            "<form method='POST' action='/api/unextractable/retry' style='display:inline;' "
            "onsubmit=\"return confirm('Retry extraction on this file? (~5s)');\">"
            f"<input type='hidden' name='source_path' value='{ref}'>"
            "<button class='btn btn-primary' type='submit'>Retry</button></form>"
        )
        rc_badge = (
            f"<span class='pill'>{html.escape(it.get('decision_reason_code', ''))}</span> "
            if it.get("decision_reason_code")
            else ""
        )
        note_html = (
            f"<br><span class='meta'>{rc_badge}{html.escape(it['decision_note'])} · "
            f"{html.escape(it['decision_ts'])}</span>"
            if it["decision_note"] or it["decision_ts"] or it.get("decision_reason_code")
            else ""
        )
        rows.append(
            f"<tr><td><code>{html.escape(it['filename'][:60])}</code></td>"
            f"<td>{html.escape(it['extension'])}</td>"
            f"<td><span class='meta' style='font-size:11px;'>{html.escape(it['source_path'][:80])}…</span></td>"
            f"<td>{decision_pill.get(it['decision'], it['decision'])}{note_html}</td>"
            f"<td>{actions}</td></tr>"
        )
    counts_line = (
        f"{counts['total']} unextractable shown · "
        f"{counts['pending']} pending · "
        f"{counts['accept']} accepted · "
        f"{counts['reject']} rejected · "
        f"{counts.get('retried_ok_total', 0)} retried-OK historically"
    )
    return f"""<!doctype html>
<html><head><title>Unextractable — {html.escape(ctx.case_id)}</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
<h1>Unextractable documents</h1>
<p class='meta'>{counts_line}</p>
{_action_result_html(action_result)}
{('<div class="banner info">All unextractable items have been reviewed.</div>' if counts["pending"] == 0 and counts["total"] > 0 else "")}
{'<p class="muted">No unextractable items to review.</p>' if not items else ""}
{"<table><thead><tr><th>File</th><th>Ext</th><th>Source path</th><th>Decision</th><th>Actions</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>" if items else ""}
{_footer(ctx)}
</body></html>"""


def render_leak_review(ctx: CaseContext, action_result: dict | None) -> str:
    """Operator review for redaction leak-failures.

    Each item is a doc where ``verify_redacted_artifact`` still found the
    leaking text after redaction was applied — operator triage decides
    accept_exclude / include_with_note / retry / manual_fix_done.
    """
    meta = load_case_metadata(ctx)
    shim = _LeakCaseShim(case_dir=ctx.case_dir)
    items = _list_leaks(shim)
    counts = _leak_summary_counts(shim)
    decision_pill = {
        "pending": "<span class='pill np'>Pending</span>",
        "accept_exclude": "<span class='pill fail'>Excluded (documented exemption)</span>",
        "include_with_note": "<span class='pill warn'>Included with note</span>",
        "retried_ok": "<span class='pill ok'>Retried — success</span>",
        "retried_fail": "<span class='pill fail'>Retried — still failed</span>",
        "manual_fix_done": "<span class='pill ok'>Manually fixed</span>",
    }
    cards = []
    for it in items:
        ref = html.escape(it["doc_ref"])
        sample = ", ".join(f"<code>{html.escape(t)}</code>" for t in it["leaks_sample"])
        distinct_n = len(it["leaks_all_distinct"])
        leak_summary = (
            f"<b>{it['leaks_count']} leak(s)</b> across <b>{distinct_n}</b> distinct terms"
            + (f". Sample: {sample}" if sample else ".")
        )
        rc_badge = (
            f"<span class='pill'>{html.escape(it.get('decision_reason_code', ''))}</span> "
            if it.get("decision_reason_code")
            else ""
        )
        note_html = (
            f"<br><span class='meta'>{rc_badge}{html.escape(it['decision_note'])} · "
            f"{html.escape(it['decision_ts'])}</span>"
            if it["decision_note"] or it["decision_ts"] or it.get("decision_reason_code")
            else ""
        )
        # Action forms
        accept_form = (
            "<form method='POST' action='/api/leak-review/decide' style='display:inline-block;margin:4px 4px 0 0;'>"
            f"<input type='hidden' name='doc_ref' value='{ref}'>"
            f"<input type='hidden' name='decision' value='accept_exclude'>"
            f"{_reason_code_select_html()}"
            "<input type='text' name='note' placeholder='exemption rationale' style='width:200px;'>"
            "<button class='btn btn-danger' type='submit'>Exclude (with exemption)</button></form>"
        )
        include_form = (
            "<form method='POST' action='/api/leak-review/decide' style='display:inline-block;margin:4px 4px 0 0;'>"
            f"<input type='hidden' name='doc_ref' value='{ref}'>"
            f"<input type='hidden' name='decision' value='include_with_note'>"
            f"{_reason_code_select_html()}"
            "<input type='text' name='note' placeholder='operator rationale' style='width:200px;'>"
            "<button class='btn btn-warn' type='submit'>Include with note</button></form>"
        )
        manual_form = (
            "<form method='POST' action='/api/leak-review/decide' style='display:inline-block;margin:4px 4px 0 0;'>"
            f"<input type='hidden' name='doc_ref' value='{ref}'>"
            f"<input type='hidden' name='decision' value='manual_fix_done'>"
            f"{_reason_code_select_html()}"
            "<input type='text' name='note' placeholder='what you fixed' style='width:200px;'>"
            "<button class='btn btn-success' type='submit'>Manually fixed (mark done)</button></form>"
        )
        retry_form = (
            "<form method='POST' action='/api/leak-review/retry' style='display:inline-block;margin:4px 0 0 0;' "
            "onsubmit=\"return confirm('Re-run redaction on this doc? (uses current tag file + protected_phrases — edit those first if needed)');\">"
            f"<input type='hidden' name='doc_ref' value='{ref}'>"
            "<button class='btn btn-primary' type='submit'>Retry redaction</button></form>"
        )
        cards.append(
            "<div class='card'>"
            f"<h3>{html.escape(it['filename'])} <span class='meta' style='font-weight:normal;'>(<code>{ref}</code>)</span></h3>"
            f"<p class='meta'>{it['entity_count']:,} entities tagged; {it['redact_count']:,} redactions attempted. Last leak check: {html.escape(it['leak_checked_at'])}</p>"
            f"<p>{leak_summary}</p>"
            f"<p><b>Status:</b> {decision_pill.get(it['decision'], it['decision'])}{note_html}</p>"
            f"<p>{accept_form}{include_form}{manual_form}<br>{retry_form}</p>"
            "<details><summary>All distinct leaks</summary>"
            f"<pre style='font-size:11px;max-height:200px;overflow:auto;'>{html.escape(', '.join(it['leaks_all_distinct']))}</pre>"
            "</details>"
            "</div>"
        )
    counts_line = (
        f"{counts['total']} stuck redactions · "
        f"{counts['pending']} pending · "
        f"{counts.get('accept_exclude', 0)} excluded · "
        f"{counts.get('include_with_note', 0)} included-with-note · "
        f"{counts.get('manual_fix_done', 0)} manually fixed · "
        f"{counts.get('retried_ok', 0)} retried-OK"
    )
    return f"""<!doctype html>
<html><head><title>Leak review — {html.escape(ctx.case_id)}</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
<h1>Redaction leak review</h1>
<p class='meta'>Documents the post-redaction verifier could not confirm were safely redacted. {counts_line}</p>
{_action_result_html(action_result)}
{'<p class="muted">No leak-review items.</p>' if not items else ""}
{"".join(cards)}
{_footer(ctx)}
</body></html>"""


def render_redaction_viewer(ctx: CaseContext, doc_ref: str) -> str:
    """Two-pane viewer projecting overlays from ``<ref>_tags.json``.
    Left pane is the verbatim original text; right pane shows the
    redacted view with ``[CODE]`` span overlays where the toolkit
    would replace text. Empty-pane fallback when text or tags missing."""
    from dsar_orchestrator.local_broker.redaction_viewer import (
        build_overlay,
        render_original_html,
        render_redacted_html,
    )

    ref_safe = html.escape(doc_ref)
    overlay = build_overlay(ctx.case_dir, doc_ref)
    text_path = ctx.case_dir / "working" / f"{doc_ref}.txt"
    if text_path.exists():
        try:
            text = text_path.read_text(encoding="utf-8")
            text_missing = False
        except OSError:
            text = ""
            text_missing = True
    else:
        text = ""
        text_missing = True

    if text_missing:
        original_block = "<p class='muted'>no text — source extraction missing for this ref</p>"
        redacted_block = "<p class='muted'>no text — source extraction missing for this ref</p>"
    else:
        original_block = render_original_html(text)
        if overlay["exists"]:
            redacted_block = render_redacted_html(text, overlay)
        else:
            redacted_block = (
                "<p class='muted'>no tags file — pii_tagger hasn't seen this ref yet</p>"
                + render_original_html(text)
            )

    tag_summary = (
        f"<span class='meta'>{len(overlay['entities'])} entities · "
        f"{sum(1 for e in overlay['entities'] if e['redact'] in (True, 'flag'))} redacted</span>"
        if overlay["exists"]
        else "<span class='meta'>no tag file</span>"
    )

    return f"""<!doctype html>
<html><head><title>Redaction viewer · {ref_safe}</title>
<style>
body{{font-family:system-ui;margin:1em;}}
.viewer{{display:grid;grid-template-columns:1fr 1fr;gap:1em;}}
.pane-original,.pane-redacted{{border:1px solid #ccc;padding:0.5em;white-space:pre-wrap;font-family:monospace;font-size:0.9em;overflow-x:auto;}}
.pane-original h2,.pane-redacted h2{{margin-top:0;font-size:0.95em;}}
span[data-code]{{background:#fee;color:#900;padding:1px 4px;border-radius:3px;font-weight:bold;}}
span[data-code="NR"]{{background:#ffd;color:#960;}}
span[data-code="DS"]{{background:#efe;color:#060;}}
.meta{{color:#666;font-size:0.85em;}}
</style></head><body>
<h1>Redaction viewer · {ref_safe}</h1>
<p>{tag_summary} · filename: <code>{html.escape(overlay["filename"] or "?")}</code></p>
<div class="viewer">
  <div class="pane-original"><h2>Original</h2>{original_block}</div>
  <div class="pane-redacted"><h2>Redacted</h2>{redacted_block}</div>
</div>
{_footer(ctx)}
</body></html>"""


def render_file_view(ctx: CaseContext, path_str: str) -> str | None:
    try:
        p = Path(path_str).resolve()
    except (OSError, ValueError):
        return None
    case_root = ctx.case_dir.resolve()
    try:
        p.relative_to(case_root)
    except ValueError:
        return None
    if not p.exists() or not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        body = f"<p>read error: {html.escape(str(exc))}</p>"
    else:
        truncated = raw[:200_000]
        suffix = f"\n\n[truncated from {len(raw)} chars]" if len(raw) > 200_000 else ""
        body = f"<pre>{html.escape(truncated + suffix)}</pre>"
    meta = load_case_metadata(ctx)
    return f"""<!doctype html>
<html><head><title>{html.escape(p.name)} — {html.escape(ctx.case_id)}</title>{_BASE_CSS}</head>
<body>
{_case_header(ctx, meta)}
<h1>{html.escape(p.name)}</h1>
<p class='meta'><code>{html.escape(str(p))}</code> &nbsp; ({_file_size(p)})</p>
{body}
{_footer(ctx)}
</body></html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    case_dir: Path
    orchestrator_cli: str = DEFAULT_ORCHESTRATOR_CLI
    approver_bin: str | None = None
    approver_input: Path = Path("/tmp/approver_input.json")


_CFG: ServerConfig | None = None
_LAST_ACTION_RESULT: dict | None = None


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _ctx(self) -> CaseContext:
        return CaseContext(case_dir=_CFG.case_dir)

    def do_GET(self) -> None:
        global _LAST_ACTION_RESULT
        url = urllib.parse.urlparse(self.path)
        ctx = self._ctx()
        # Stage-rail enforcement: deep-links past the current phase 303
        # back to the landing page with a banner explaining why. Also
        # covers dynamic-suffix routes registered in ROUTE_PREFIX_REQUIRED_PHASE
        # (e.g. /redaction-viewer/<ref>).
        gated = url.path in ROUTE_REQUIRED_PHASE or any(
            url.path.startswith(prefix) for prefix in ROUTE_PREFIX_REQUIRED_PHASE
        )
        if gated:
            state = load_orchestrator_state(ctx)
            allowed, msg = is_route_accessible(state, url.path)
            if not allowed:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": msg,
                    "command": f"GET {url.path} blocked by stage-rail enforcement",
                }
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
        ar = _LAST_ACTION_RESULT
        if url.path == "/":
            state = load_orchestrator_state(ctx)
            body = render_landing(ctx, state, ar)
            _LAST_ACTION_RESULT = None
            self._send(200, body)
            return
        if url.path == "/blockers":
            body = render_blockers(ctx, ar)
            _LAST_ACTION_RESULT = None
            self._send(200, body)
            return
        if url.path == "/release-check":
            body = render_release_check(ctx, ar)
            _LAST_ACTION_RESULT = None
            self._send(200, body)
            return
        if url.path == "/pipeline":
            body = render_pipeline_details(ctx, ar)
            _LAST_ACTION_RESULT = None
            self._send(200, body)
            return
        if url.path == "/unextractable":
            self._send(200, render_unextractable(ctx, ar))
            _LAST_ACTION_RESULT = None
            return
        if url.path == "/qa-sample":
            self._send(200, render_qa_sample(ctx, ar))
            _LAST_ACTION_RESULT = None
            return
        if url.path == "/leak-review":
            self._send(200, render_leak_review(ctx, ar))
            _LAST_ACTION_RESULT = None
            return
        if url.path == "/closure-letter":
            self._send(200, render_closure_letter(ctx))
            return
        if url.path == "/file":
            q = urllib.parse.parse_qs(url.query)
            path = (q.get("path") or [""])[0]
            body = render_file_view(ctx, path)
            if body is None:
                self._send(404, "<h1>404 file not found / not in case dir</h1>")
            else:
                self._send(200, body)
            return
        if url.path.startswith("/redaction-viewer/"):
            doc_ref = url.path[len("/redaction-viewer/") :]
            if not doc_ref or "/" in doc_ref:
                self._send(404, "<h1>404 redaction viewer: missing or invalid doc ref</h1>")
                return
            self._send(200, render_redaction_viewer(ctx, doc_ref))
            return
        self._send(404, "<h1>404</h1>")

    def do_POST(self) -> None:
        global _LAST_ACTION_RESULT
        url = urllib.parse.urlparse(self.path)
        ctx = self._ctx()
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        target = "/"
        if url.path == "/api/advance":
            gate_after = form.get("gate_after") == "1"
            _LAST_ACTION_RESULT = action_advance(
                ctx, orchestrator_cli=_CFG.orchestrator_cli, gate_after=gate_after
            )
        elif url.path == "/api/clear-gate":
            _LAST_ACTION_RESULT = action_clear_gate(ctx, orchestrator_cli=_CFG.orchestrator_cli)
        elif url.path == "/api/run-approver":
            if not _CFG.approver_bin:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": "no --approver-bin configured",
                    "command": "(no approver bin)",
                }
            else:
                _LAST_ACTION_RESULT = action_run_approver(
                    ctx, approver_bin=_CFG.approver_bin, approver_input_path=_CFG.approver_input
                )
            target = "/release-check"
        elif url.path == "/api/unextractable/decide":
            source_path = form.get("source_path", "")
            decision = form.get("decision", "")
            reason_code = form.get("reason_code", "")
            note = form.get("note", "")
            shim = _UnextCaseShim(case_dir=ctx.case_dir)
            try:
                _unext_record_decision(
                    shim,
                    source_path=source_path,
                    decision=decision,
                    reason_code=reason_code,
                    note=note,
                )
                _LAST_ACTION_RESULT = {
                    "rc": 0,
                    "stdout": f"decision={decision} recorded for {Path(source_path).name}",
                    "stderr": "",
                    "command": f"unextractable.record_decision({decision!r})",
                }
            except Exception as exc:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                    "command": "unextractable.record_decision",
                }
            target = "/unextractable"
        elif url.path == "/api/unextractable/retry":
            source_path = form.get("source_path", "")
            shim = _UnextCaseShim(case_dir=ctx.case_dir)
            try:
                result = _unext_retry(shim, source_path=source_path, case_id=ctx.case_id)
                _LAST_ACTION_RESULT = {
                    "rc": 0 if result["ok"] else 2,
                    "stdout": (
                        f"retry OK — chars={result['item']['extracted_text_chars']}, "
                        f"yield={result['item']['yield_ratio']:.3f}"
                    )
                    if result["ok"]
                    else "",
                    "stderr": "" if result["ok"] else result["error"],
                    "command": f"unextractable.retry_extract({Path(source_path).name!r})",
                }
            except Exception as exc:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                    "command": "unextractable.retry_extract",
                }
            target = "/unextractable"
        elif url.path == "/api/qa-sample/decide":
            from dsar_orchestrator.local_broker.qa_sample import record_qa_decision

            doc_ref = form.get("doc_ref", "")
            decision = form.get("decision", "")
            reason_code = form.get("reason_code", "")
            note = form.get("note", "")
            try:
                record_qa_decision(
                    ctx.case_dir,
                    doc_ref=doc_ref,
                    decision=decision,
                    reason_code=reason_code,
                    note=note,
                )
                _LAST_ACTION_RESULT = {
                    "rc": 0,
                    "stdout": f"qa decision={decision} recorded for {doc_ref}",
                    "stderr": "",
                    "command": f"qa_sample.record_qa_decision({decision!r})",
                }
            except Exception as exc:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                    "command": "qa_sample.record_qa_decision",
                }
            target = "/qa-sample"
        elif url.path == "/api/leak-review/decide":
            doc_ref = form.get("doc_ref", "")
            decision = form.get("decision", "")
            reason_code = form.get("reason_code", "")
            note = form.get("note", "")
            shim = _LeakCaseShim(case_dir=ctx.case_dir)
            try:
                _leak_record_decision(
                    shim,
                    doc_ref=doc_ref,
                    decision=decision,
                    reason_code=reason_code,
                    note=note,
                )
                _LAST_ACTION_RESULT = {
                    "rc": 0,
                    "stdout": f"decision={decision} recorded for {doc_ref}",
                    "stderr": "",
                    "command": f"leak_review.record_decision({decision!r})",
                }
            except Exception as exc:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                    "command": "leak_review.record_decision",
                }
            target = "/leak-review"
        elif url.path == "/api/leak-review/retry":
            doc_ref = form.get("doc_ref", "")
            shim = _LeakCaseShim(case_dir=ctx.case_dir)
            try:
                result = _leak_retry(shim, doc_ref=doc_ref)
                _LAST_ACTION_RESULT = {
                    "rc": 0 if result["ok"] else 2,
                    "stdout": (f"retry OK — redactions_applied={result['count']}")
                    if result["ok"]
                    else "",
                    "stderr": "" if result["ok"] else result["error"],
                    "command": f"leak_review.retry_redaction({doc_ref!r})",
                }
            except Exception as exc:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                    "command": "leak_review.retry_redaction",
                }
            target = "/leak-review"
        elif url.path == "/api/blocker/toggle":
            bid = form.get("id", "")
            resolved = form.get("resolved") == "1"
            reason_code = form.get("reason_code", "")
            note = form.get("note", "")
            if bid:
                try:
                    toggle_blocker_resolved(
                        ctx, bid, resolved=resolved, reason_code=reason_code, note=note
                    )
                    _LAST_ACTION_RESULT = None
                except Exception as exc:
                    _LAST_ACTION_RESULT = {
                        "rc": 2,
                        "stdout": "",
                        "stderr": f"{type(exc).__name__}: {exc}",
                        "command": f"toggle_blocker_resolved({bid!r}, resolved={resolved})",
                    }
            else:
                _LAST_ACTION_RESULT = None
            target = "/blockers"
        elif url.path == "/api/summarise-stage":
            stage = form.get("stage", "")
            if stage in STAGES:
                phase_label = _phase_label_for_stage(stage)
                state = load_orchestrator_state(ctx)
                current = state["current_stage"]
                history_ts = {h["stage"]: h["ts"] for h in state.get("history", [])}
                status = (
                    "current"
                    if stage == current
                    else "done"
                    if history_ts.get(stage)
                    else "pending"
                )
                sum_cfg = SummariserConfig(case_dir=ctx.case_dir)
                eviction = check_broker_eviction_risk(sum_cfg)
                try:
                    record = summarise_stage(
                        sum_cfg,
                        stage=stage,
                        stage_label=STAGE_LABELS.get(stage, stage),
                        phase_label=phase_label,
                        status=status,
                        stage_artefact_names=STAGE_ARTEFACTS.get(stage, []),
                        force_refresh=True,
                    )
                    msg = (
                        f"stage={record['stage']} rag={record['rag']} "
                        f"elapsed={record['elapsed_sec']}s"
                    )
                    if eviction.get("warning"):
                        msg += f"\n[warn] {eviction['warning']}"
                    _LAST_ACTION_RESULT = {
                        "rc": 0,
                        "stdout": msg,
                        "stderr": "",
                        "command": (f"summarise_stage(stage={stage!r}, model={sum_cfg.model})"),
                    }
                except Exception as exc:
                    _LAST_ACTION_RESULT = {
                        "rc": 2,
                        "stdout": "",
                        "stderr": f"{type(exc).__name__}: {exc}",
                        "command": f"summarise_stage(stage={stage!r})",
                    }
            else:
                _LAST_ACTION_RESULT = {
                    "rc": 2,
                    "stdout": "",
                    "stderr": f"unknown stage: {stage!r}",
                    "command": "(skipped)",
                }
            target = "/pipeline"
        else:
            self._send(404, "<h1>404</h1>")
            return
        self.send_response(303)
        self.send_header("Location", target)
        self.end_headers()


def main(argv: list[str] | None = None) -> int:
    global _CFG
    p = argparse.ArgumentParser(
        prog="dsar-operator-console",
        description="Web UI to inspect, advance, and gate a DSAR conductor case.",
    )
    p.add_argument("--case-dir", required=True, type=Path)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--orchestrator-cli", default=DEFAULT_ORCHESTRATOR_CLI)
    p.add_argument("--approver-bin", default=None)
    p.add_argument("--approver-input", default="/tmp/approver_input.json", type=Path)
    args = p.parse_args(argv)
    case_dir = args.case_dir.resolve()
    if not case_dir.exists():
        print(f"error: case-dir does not exist: {case_dir}", file=sys.stderr)
        return 2
    resolved_cli = shutil.which(args.orchestrator_cli) or args.orchestrator_cli
    if "/opt/homebrew/" in resolved_cli:
        print(
            f"warn: --orchestrator-cli resolved to {resolved_cli} (Homebrew shim). "
            "Pass an absolute venv path to be safe.",
            file=sys.stderr,
        )
    _CFG = ServerConfig(
        case_dir=case_dir,
        orchestrator_cli=resolved_cli,
        approver_bin=args.approver_bin,
        approver_input=args.approver_input.resolve(),
    )
    server = ThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    log.info("operator console v2 on http://%s:%d/ (case=%s)", args.host, args.port, case_dir.name)
    log.info("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
