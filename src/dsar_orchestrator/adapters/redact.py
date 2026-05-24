"""Conductor-owned redact adapter — Stage 6.

Bridges to the toolkit's ``dsar-redact`` CLI (entry:
``dsar_pipeline.redact_stage:main``). The CLI builds the canonical
``working/redaction_input.jsonl`` by unioning scope verdicts +
PII findings + exemption findings. Actual file output (redacted PDFs
etc.) happens downstream in the ``bake`` stage (handled by the
export adapter).

This is a deliberate split: the conductor's Stage 6 (redact)
captures "what to redact" (the input spec); Stage 7 (bake) is
"apply the redactions" + "write redacted PDFs". Matches the toolkit's
v5 pipeline shape.

**Retirement contract.** When the toolkit ships a thin Python entry
``dsar_pipeline.redact_stage.run_for_case(case_path)``, this adapter
retires.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import hash_pairs, sha256_file, sha256_text

PRODUCER_VERSION = "dsar_orchestrator.adapters.redact 0.1.0"
SCHEMA_VERSION = "1.0"
DEFAULT_CLI = "dsar-redact"

RunnerFn = Callable[[list[str], dict[str, str]], subprocess.CompletedProcess]


def _default_runner() -> RunnerFn:
    def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )

    return run


def run_for_case(
    cfg: CaseConfig,
    *,
    runner: RunnerFn | None = None,
    cli: str = DEFAULT_CLI,
) -> None:
    """Drive the toolkit's redact stage; write the cascade anchor."""
    if runner is None:
        runner = _default_runner()

    env = dict(os.environ)
    env["DSAR_CASE_ROOT"] = str(cfg.case_path.parent)

    argv = [cli, "--case", cfg.case_no]
    completed = runner(argv, env)

    if completed.returncode != 0:
        stderr = (completed.stderr or "")[-2000:]
        raise DSARPipelineError(
            f"case={cfg.case_no}: redact CLI exited {completed.returncode}. stderr tail:\n{stderr}"
        )

    redaction_input_path = cfg.case_path / "working" / "redaction_input.jsonl"
    if not redaction_input_path.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: redact CLI succeeded but "
            f"redaction_input.jsonl was not produced at "
            f"{redaction_input_path}."
        )

    upstream_hash = _compute_upstream_hash(cfg)
    summary = _summarise_redaction_input(redaction_input_path)
    _write_anchor(cfg.case_path, upstream_hash, summary)


def _compute_upstream_hash(cfg: CaseConfig) -> str:
    """Mirror stages._hash_redact_inputs: hash of all *_tags.json files
    + pii_collection.jsonl + enforce flag."""
    tags_dir = cfg.case_path / "working"
    pairs: list[tuple[str, str]] = []
    if tags_dir.exists():
        for p in sorted(tags_dir.glob("*_tags.json")):
            pairs.append((p.name, sha256_file(p)))
    pii_file = tags_dir / "pii_collection.jsonl"
    pii_hash = sha256_file(pii_file) if pii_file.exists() else ""
    enforce = cfg.pii_classify_mode == "enforce"
    return sha256_text(f"{hash_pairs(pairs)}\x1f{pii_hash}\x1fenforce={enforce}")


def _summarise_redaction_input(path: Path) -> dict[str, int]:
    """Cheap counts the dashboard / cascade can show. Raises on any
    malformed row — the toolkit emitting corrupt JSON in
    redaction_input.jsonl is a real bug we surface rather than
    swallow."""
    total = 0
    by_reason: dict[str, int] = {}
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DSARPipelineError(f"malformed row {line_no} in {path}: {exc}") from exc
        reason = row.get("reason_code") or row.get("reason") or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"total_redactions": total, "by_reason": by_reason}


def _write_anchor(case_path: Path, upstream_hash: str, summary: dict) -> None:
    """Write working/redact_complete.json atomically with the cascade
    fields the orchestrator's STAGE_ARTEFACTS registry expects."""
    out_path = case_path / "working" / "redact_complete.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    obj = {
        "completed": True,
        "upstream_hash": upstream_hash,
        "summary": summary,
        "schema_version": SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
