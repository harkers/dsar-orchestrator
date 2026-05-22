"""Local-LLM-driven analyser for case audit logs.

Reads structured logs under ``~/.dsar-audit/<case>/`` + the case's
``working/`` directory, asks a local LLM (via mlx-broker) to surface
issues, and writes findings back to the audit tree. Critical findings
write a block flag the orchestrator checks at next-run startup.

**Stays on the box.** Routes through ``mlx-broker`` on
``http://127.0.0.1:8090`` by default — no external API calls. No
client document text ever reaches the analyser; only the structured
audit metadata (refs, hashes, scores, timestamps, error messages).
"""

__all__ = [
    "analyse_case",
    "AnalysisFinding",
    "AnalysisReport",
    "Severity",
]

from dsar_orchestrator.log_analyser.core import analyse_case
from dsar_orchestrator.log_analyser.schemas import (
    AnalysisFinding,
    AnalysisReport,
    Severity,
)
