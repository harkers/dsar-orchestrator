"""pipeline.jsonl audit emission + StageBanner context manager.

The orchestrator writes one row per stage transition to
`~/.dsar-audit/<case>/pipeline.jsonl`. Rows include stage name, start
+ end timestamps, duration, outcome (ok/failed), and optional notes.
Schema: `docs/audit_schemas/pipeline.schema.json`.

Atomic-write contract per the orchestration spec § Operational
semantics: every row append is flushed + fsync'd before returning.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dsar_orchestrator import __version__ as ORCHESTRATOR_VERSION

SCHEMA_VERSION = "1.0"
PRODUCER = f"dsar_orchestrator {ORCHESTRATOR_VERSION}"


@dataclass
class RunReport:
    """Returned by `pipeline.run()` summarising the case run."""

    case_no: str
    stages_run: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": "run_complete",
            "case": self.case_no,
            "stages_run": self.stages_run,
            "stages_skipped": self.stages_skipped,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "schema_version": SCHEMA_VERSION,
            "producer_version": PRODUCER,
        }


class PipelineAuditor:
    """Writes `~/.dsar-audit/<case>/pipeline.jsonl`.

    One auditor instance per case run. Tracks stages-run + stages-
    skipped for the final RunReport. Every write is atomic
    (flush + fsync) so a process crash leaves no half-written row.
    """

    def __init__(self, case_no: str, audit_root: Path | None = None):
        self.case_no = case_no
        base = audit_root or (Path.home() / ".dsar-audit")
        self.audit_dir = base / case_no
        self.audit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_path = self.audit_dir / "pipeline.jsonl"
        self.run_started_at = datetime.now(timezone.utc)
        self.stages_run: list[str] = []
        self.stages_skipped: list[str] = []
        self.halted = False
        self.halt_reason = ""

    def write(self, row: dict[str, Any]) -> None:
        """Append one JSON row to pipeline.jsonl. Atomic per row."""
        row.setdefault("schema_version", SCHEMA_VERSION)
        row.setdefault("producer_version", PRODUCER)
        row.setdefault("case", self.case_no)
        line = json.dumps(row, sort_keys=True) + "\n"
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def note(self, kind: str, message: str, **extra: Any) -> None:
        """Write a free-form note row (not a stage transition)."""
        self.write({"event": "note", "kind": kind, "message": message, **extra})

    def mark_skipped(self, stage: str, reason: str) -> None:
        self.stages_skipped.append(stage)
        self.write({"event": "stage_skipped", "stage": stage, "reason": reason})

    def mark_halted(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def finalise(self) -> RunReport:
        ended = datetime.now(timezone.utc)
        report = RunReport(
            case_no=self.case_no,
            stages_run=list(self.stages_run),
            stages_skipped=list(self.stages_skipped),
            started_at=self.run_started_at.isoformat(),
            finished_at=ended.isoformat(),
            duration_s=(ended - self.run_started_at).total_seconds(),
            halted=self.halted,
            halt_reason=self.halt_reason,
        )
        self.write(report.as_dict())
        return report


@contextmanager
def StageBanner(auditor: PipelineAuditor, stage: str, *, stream=None):
    """Context manager wrapping a stage's execution.

    Writes start + end rows to pipeline.jsonl. Prints a one-line
    banner to `stream` (default stderr) at start + end so the
    operator sees progress.

    On exception: writes a failed-end row + re-raises (preserving
    the typed exception so the orchestrator's caller sees it).
    """
    stream = stream or sys.stderr
    start = datetime.now(timezone.utc)
    auditor.stages_run.append(stage)
    print(
        f"[{start.isoformat()}] case={auditor.case_no} stage={stage:<22} start",
        file=stream,
    )
    auditor.write({"event": "stage_start", "stage": stage, "ts": start.isoformat()})

    try:
        yield
    except Exception as e:
        end = datetime.now(timezone.utc)
        duration_s = (end - start).total_seconds()
        print(
            f"[{end.isoformat()}] case={auditor.case_no} stage={stage:<22} "
            f"FAILED after {duration_s:.1f}s: {type(e).__name__}: {e}",
            file=stream,
        )
        auditor.write(
            {
                "event": "stage_end",
                "stage": stage,
                "ts": end.isoformat(),
                "duration_s": duration_s,
                "outcome": "failed",
                "error_type": type(e).__name__,
                "error_message": str(e),
            }
        )
        raise

    end = datetime.now(timezone.utc)
    duration_s = (end - start).total_seconds()
    print(
        f"[{end.isoformat()}] case={auditor.case_no} stage={stage:<22} done in {duration_s:.1f}s",
        file=stream,
    )
    auditor.write(
        {
            "event": "stage_end",
            "stage": stage,
            "ts": end.isoformat(),
            "duration_s": duration_s,
            "outcome": "ok",
        }
    )
