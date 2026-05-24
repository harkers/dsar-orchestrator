"""Collect + summarise audit logs into a compact payload for the LLM.

Important: the analyser receives **only structured metadata** — refs,
hashes, timestamps, scores, error messages, schema fields. Raw
document text never enters the LLM context. This is the line the
analyser must not cross.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KNOWN_LOGS = (
    "pipeline.jsonl",
    "llm_calls.jsonl",
    "scope_rerank.jsonl",
    "pii_collection.jsonl",
    "scope_recheck.jsonl",
)

# Logs written by the toolkit into <case>/working/ (not under ~/.dsar-audit).
# Collected from the case working directory, not the audit root.
WORKING_KNOWN_LOGS = ("post_bake_findings.jsonl",)


def _read_jsonl_safely(path: Path, max_rows: int = 5000) -> list[dict[str, Any]]:
    """Read a JSONL audit log; tolerate per-row parse errors."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip malformed rows but keep collecting
            rows.append({"_parse_error": line[:120]})
        if len(rows) >= max_rows:
            rows.append({"_truncated": True, "max_rows": max_rows})
            break
    return rows


def collect_case_logs(
    case_no: str,
    audit_root: Path | None = None,
    case_root: Path | None = None,
) -> dict[str, list[dict]]:
    """Return a dict ``{log_filename: [rows...]}`` for every known
    audit log present under ``~/.dsar-audit/<case_no>/`` plus working-
    directory logs (e.g. ``working/post_bake_findings.jsonl``) from
    ``<case_root>/<case_no>/``.

    Missing logs are returned as empty lists so downstream code can
    iterate without per-file existence checks.
    """
    base = audit_root or (Path.home() / ".dsar-audit")
    case_dir = base / case_no
    result = {name: _read_jsonl_safely(case_dir / name) for name in KNOWN_LOGS}

    # Collect working-dir logs (v5.0+: written by the toolkit into
    # <case>/working/, not the audit root).
    if case_root is not None:
        working_dir = Path(case_root) / case_no / "working"
    else:
        working_dir = Path.home() / "dsars" / "cases" / case_no / "working"
    for name in WORKING_KNOWN_LOGS:
        result[name] = _read_jsonl_safely(working_dir / name)

    return result


def summarise_for_prompt(
    case_no: str, logs: dict[str, list[dict]], *, max_rows_per_log: int = 200
) -> str:
    """Build a compact, prompt-safe text rendering of the collected
    logs.

    Each log gets a header + the first ``max_rows_per_log`` rows
    pretty-printed as JSON. Truncation is announced explicitly so the
    LLM doesn't draw conclusions from absence.
    """
    parts: list[str] = []
    parts.append(f"# Audit logs for case {case_no}")
    parts.append("")

    for name in (*KNOWN_LOGS, *WORKING_KNOWN_LOGS):
        rows = logs.get(name, [])
        parts.append(f"## {name}")
        parts.append("")
        if not rows:
            parts.append("(empty / file does not exist)")
            parts.append("")
            continue
        truncated = len(rows) > max_rows_per_log
        shown = rows[:max_rows_per_log]
        parts.append(
            f"Rows shown: {len(shown)} of {len(rows)} total" + (" (truncated)" if truncated else "")
        )
        parts.append("")
        parts.append("```json")
        for r in shown:
            parts.append(json.dumps(r, sort_keys=True))
        parts.append("```")
        parts.append("")

    return "\n".join(parts)


def basic_stats(logs: dict[str, list[dict]]) -> dict[str, Any]:
    """Compute cheap deterministic stats the analyser can include in
    its findings without needing the LLM.

    Useful both as evidence for LLM findings and as a sanity check
    against LLM hallucinations.
    """
    pipeline = logs.get("pipeline.jsonl", [])
    stages_run: list[str] = []
    stages_failed: list[str] = []
    halt_reasons: list[str] = []
    durations: dict[str, float] = {}

    for row in pipeline:
        event = row.get("event")
        if event == "stage_end":
            stage = row.get("stage", "")
            stages_run.append(stage)
            durations[stage] = float(row.get("duration_s", 0))
            if row.get("outcome") == "failed":
                stages_failed.append(stage)
        elif event == "run_complete" and row.get("halted"):
            halt_reasons.append(row.get("halt_reason", ""))

    return {
        "pipeline_stages_run": stages_run,
        "pipeline_stages_failed": stages_failed,
        "pipeline_halt_reasons": halt_reasons,
        "pipeline_durations_s": durations,
        "llm_call_count": len(logs.get("llm_calls.jsonl", [])),
        "rerank_row_count": len(logs.get("scope_rerank.jsonl", [])),
        "pii_collection_row_count": len(logs.get("pii_collection.jsonl", [])),
        "disputed_doc_count": sum(
            1 for r in logs.get("scope_recheck.jsonl", []) if r.get("verdict") == "disputed"
        ),
        "verify_failed_count": sum(
            1 for r in logs.get("post_bake_findings.jsonl", []) if r.get("severity") == "high"
        ),
    }
