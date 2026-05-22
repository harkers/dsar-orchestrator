"""Top-level ``analyse_case()`` entry point.

Collects audit logs → asks mlx-broker for a structured analysis →
writes findings to ~/.dsar-audit/<case>/analysis.{jsonl,md} → drops
a block flag if any critical finding is present.

The orchestrator reads the block flag at next-run startup (see
``pipeline.run()``) and refuses to proceed without
``--acknowledge-issues``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dsar_orchestrator.log_analyser.client import (
    ChatResponse,
    chat,
)
from dsar_orchestrator.log_analyser.collectors import (
    basic_stats,
    collect_case_logs,
    summarise_for_prompt,
)
from dsar_orchestrator.log_analyser.prompts import SYSTEM_PROMPT, build_user_message
from dsar_orchestrator.log_analyser.schemas import (
    AnalysisFinding,
    AnalysisReport,
    Severity,
)

BLOCK_FLAG_FILENAME = "analysis-block.flag"


def analyse_case(
    case_no: str,
    *,
    audit_root: Path | None = None,
    model_alias: str | None = None,
    broker_url: str | None = None,
    write_outputs: bool = True,
    chat_fn=chat,  # injectable for tests; default = real mlx-broker
) -> AnalysisReport:
    """Run the analyser on one case's audit logs.

    Writes (when ``write_outputs=True``):
    - ``<audit_root>/<case>/analysis.jsonl`` — one row per finding
    - ``<audit_root>/<case>/analysis.md`` — human-readable summary
    - ``<audit_root>/<case>/analysis-block.flag`` — present iff any
      critical finding

    Returns the AnalysisReport regardless of write_outputs.
    """
    audit_root = audit_root or (Path.home() / ".dsar-audit")
    case_dir = audit_root / case_no

    logs = collect_case_logs(case_no, audit_root=audit_root)
    stats = basic_stats(logs)
    logs_summary = summarise_for_prompt(case_no, logs)

    response = chat_fn(
        system=SYSTEM_PROMPT,
        user=build_user_message(case_no, logs_summary, stats),
        model_alias=model_alias,
        base_url=broker_url,
    )

    report = _parse_response(case_no, response)

    if write_outputs:
        _write_outputs(case_dir, report)

    return report


def _parse_response(case_no: str, response: ChatResponse) -> AnalysisReport:
    """Extract the JSON object from the LLM's response into an
    AnalysisReport. Tolerates code-fence wrappers + leading prose."""
    text = response.text.strip()
    raw = _extract_json_object(text)

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to a single critical finding telling the operator
        # the LLM didn't produce valid JSON. Better than crashing.
        return AnalysisReport(
            case_no=case_no,
            model_alias=response.model_alias,
            resolved_model=response.resolved_model,
            summary="Analyser LLM returned non-JSON output; review manually.",
            findings=[
                AnalysisFinding(
                    severity=Severity.WARNING,
                    category="analyser_self_error",
                    message="The analyser LLM produced output that was not "
                    "a parseable JSON object. The raw text is preserved "
                    "below for manual review.",
                    evidence=[text[:1000]],
                    recommendation=(
                        "Re-run `dsar-analyse-logs --case <no>`; if it "
                        "happens repeatedly, switch the model alias via "
                        "`DSAR_ANALYSER_MODEL=<other-alias>`."
                    ),
                )
            ],
        )

    findings = [AnalysisFinding.from_dict(d) for d in obj.get("findings", [])]
    summary = obj.get("summary", "")

    return AnalysisReport(
        case_no=case_no,
        model_alias=response.model_alias,
        resolved_model=response.resolved_model,
        summary=summary,
        findings=findings,
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> str:
    """Pull a JSON object out of an LLM response. Strips ```json fences
    if present; otherwise grabs from the first ``{`` to the last
    ``}``."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    matched = _JSON_BLOCK_RE.search(text)
    if matched:
        return matched.group(0)
    return text  # let json.loads raise a clearer error


def _write_outputs(case_dir: Path, report: AnalysisReport) -> None:
    case_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # JSONL: header row (report metadata) + one row per finding
    jsonl_path = case_dir / "analysis.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        header = {
            "row_type": "header",
            "case_no": report.case_no,
            "generated_at": report.generated_at,
            "model_alias": report.model_alias,
            "resolved_model": report.resolved_model,
            "summary": report.summary,
            "schema_version": "1.0",
            "producer_version": "dsar_orchestrator.log_analyser 0.1.0",
        }
        f.write(json.dumps(header) + "\n")
        for finding in report.findings:
            row = {"row_type": "finding", **finding.as_dict()}
            f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())

    # Human-readable markdown
    md_path = case_dir / "analysis.md"
    md_path.write_text(report.render_markdown(), encoding="utf-8")

    # Block flag — drop if any critical, remove otherwise so a clean
    # follow-up analysis clears a previous block.
    flag_path = case_dir / BLOCK_FLAG_FILENAME
    if report.has_blocking_issues:
        flag_text = (
            f"BLOCK: {len(report.critical)} critical finding(s) in "
            f"{case_dir}/analysis.md\n"
            f"To proceed despite the block: `dsar-conductor --case "
            f"{report.case_no} --acknowledge-issues`.\n"
        )
        flag_path.write_text(flag_text, encoding="utf-8")
    elif flag_path.exists():
        flag_path.unlink()


def is_blocked(case_no: str, audit_root: Path | None = None) -> bool:
    """Return True if the analyser previously found a critical issue
    for this case and the operator has not acknowledged it yet."""
    audit_root = audit_root or (Path.home() / ".dsar-audit")
    return (audit_root / case_no / BLOCK_FLAG_FILENAME).exists()


def clear_block(case_no: str, audit_root: Path | None = None) -> None:
    """Acknowledge + clear an analyser block."""
    audit_root = audit_root or (Path.home() / ".dsar-audit")
    flag = audit_root / case_no / BLOCK_FLAG_FILENAME
    if flag.exists():
        flag.unlink()
