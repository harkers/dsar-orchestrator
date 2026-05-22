"""Typed result objects for the log analyser."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def numeric(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


@dataclass
class AnalysisFinding:
    """One issue surfaced by the analyser."""

    severity: Severity
    category: str  # e.g. "stage_duration_outlier", "high_dispute_rate"
    message: str
    evidence: list[str] = field(default_factory=list)  # references to audit rows / artefacts
    recommendation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "message": self.message,
            "evidence": list(self.evidence),
            "recommendation": self.recommendation,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnalysisFinding:
        return cls(
            severity=Severity(d.get("severity", "info")),
            category=d.get("category", "unknown"),
            message=d.get("message", ""),
            evidence=list(d.get("evidence", [])),
            recommendation=d.get("recommendation", ""),
        )


@dataclass
class AnalysisReport:
    """The full analyser output for a case."""

    case_no: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model_alias: str = ""
    resolved_model: str = ""
    findings: list[AnalysisFinding] = field(default_factory=list)
    summary: str = ""

    @property
    def critical(self) -> list[AnalysisFinding]:
        return [f for f in self.findings if f.severity == Severity.CRITICAL]

    @property
    def has_blocking_issues(self) -> bool:
        return bool(self.critical)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_no": self.case_no,
            "generated_at": self.generated_at,
            "model_alias": self.model_alias,
            "resolved_model": self.resolved_model,
            "summary": self.summary,
            "findings": [f.as_dict() for f in self.findings],
            "counts": {
                "critical": len(self.critical),
                "warning": sum(1 for f in self.findings if f.severity == Severity.WARNING),
                "info": sum(1 for f in self.findings if f.severity == Severity.INFO),
                "total": len(self.findings),
            },
            "schema_version": "1.0",
        }

    def render_markdown(self) -> str:
        lines = [
            f"# Analysis report — case {self.case_no}",
            "",
            f"*Generated {self.generated_at} via `{self.model_alias}` (→ `{self.resolved_model}`)*",
            "",
            f"**Summary:** {self.summary or '(no summary)'}",
            "",
            f"**Findings:** {len(self.critical)} critical, "
            f"{sum(1 for f in self.findings if f.severity == Severity.WARNING)} warning, "
            f"{sum(1 for f in self.findings if f.severity == Severity.INFO)} info.",
            "",
        ]
        for sev in (Severity.CRITICAL, Severity.WARNING, Severity.INFO):
            ones = [f for f in self.findings if f.severity == sev]
            if not ones:
                continue
            lines.append(f"## {sev.value.title()}")
            lines.append("")
            for f in ones:
                lines.append(f"### {f.category}")
                lines.append("")
                lines.append(f.message)
                lines.append("")
                if f.evidence:
                    lines.append("**Evidence:**")
                    for e in f.evidence:
                        lines.append(f"- `{e}`")
                    lines.append("")
                if f.recommendation:
                    lines.append(f"**Recommendation:** {f.recommendation}")
                    lines.append("")
        return "\n".join(lines)
