"""Case config — load + validate from `case_config.json` + env vars.

Per the orchestration spec, the orchestrator reads config ONCE at
start, validates, then passes values into each module's
`core.<fn>(case, mode=..., ...)`. The orchestrator never re-reads
mid-run.

Config precedence (highest to lowest):
1. Operator-override file (`~/.dsar-rerank-mode`, `~/.dsar-pii-mode`)
   — for mode env vars only
2. Environment variable (e.g., `RERANK_MODE`, `PII_CLASSIFY_MODE`)
3. `case_config.json` field
4. Hard-coded default

Phase 4 prereq validation lives here: cases that run Phase 4 must
declare `subject_identifier`. The validator refuses with a clear
message otherwise.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_RERANK_MODES = {"off", "shadow", "enforce"}
VALID_PII_MODES = {"off", "shadow", "enforce"}


@dataclass
class SubjectIdentifier:
    """Anchor that distinguishes the data subject from third parties
    with similar names. Required for Phase 4."""

    primary_name: str
    dob: str | None = None
    employee_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    disambiguation_notes: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> SubjectIdentifier | None:
        if d is None:
            return None
        return cls(
            primary_name=d.get("primary_name", ""),
            dob=d.get("dob"),
            employee_id=d.get("employee_id"),
            aliases=list(d.get("aliases", [])),
            disambiguation_notes=d.get("disambiguation_notes", ""),
        )


@dataclass
class CaseConfig:
    """Per-case runtime config consumed by the orchestrator."""

    case_no: str
    case_path: Path
    case_scope: str = ""
    subject_identifier: SubjectIdentifier | None = None

    # Phase 2 (reranker)
    rerank_mode: str = "shadow"
    rerank_threshold: float = 0.01
    rerank_top_n: int = 20
    rerank_sample_rate: float = 0.05

    # Phase 4 (LLM PII classifier)
    pii_classify_mode: str = "shadow"
    pii_budget_usd: float = 10.0

    # Phase 5 (PII discovery — Path A)
    discovery_enabled: bool = True

    # Phase 6 (redact-verify — Path B)
    redact_verify_enabled: bool = True

    # Cross-cutting
    llm_concurrency: int = 5


def _read_override_file(name: str) -> str | None:
    """Read the contents of a ~/.dsar-<name>-mode file, if present."""
    p = Path.home() / f".dsar-{name}-mode"
    if not p.exists():
        return None
    return p.read_text().strip() or None


def _resolve_mode(
    config_value: str,
    env_var: str,
    override_name: str,
    valid: set[str],
) -> str:
    """Apply the override → env → config precedence for a mode field."""
    override = _read_override_file(override_name)
    if override is not None:
        if override not in valid:
            raise ValueError(
                f"~/.dsar-{override_name}-mode contains {override!r}; "
                f"expected one of {sorted(valid)}"
            )
        return override

    env_value = os.environ.get(env_var)
    if env_value is not None:
        if env_value not in valid:
            raise ValueError(f"${env_var}={env_value!r}; expected one of {sorted(valid)}")
        return env_value

    if config_value not in valid:
        raise ValueError(
            f"case_config rerank/pii mode {config_value!r}; expected one of {sorted(valid)}"
        )
    return config_value


def load_case_config(case_no: str, case_root: Path | None = None) -> CaseConfig:
    """Load `case_config.json` from the case directory + apply env-var
    overrides. Returns a fully-resolved CaseConfig.

    Default case root: `~/dsars/cases/<case_no>/`.
    """
    case_path = case_root or (Path.home() / "dsars" / "cases" / case_no)
    if not case_path.exists():
        raise FileNotFoundError(f"Case directory not found: {case_path}")

    config_path = case_path / "case_config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"No case_config.json at {config_path}. Create one with at "
            f'least: {{"case_no": {case_no!r}, "case_scope": "..."}}'
        )

    with open(config_path) as f:
        raw = json.load(f)

    raw_rerank_mode = raw.get("rerank_mode", "shadow")
    raw_pii_mode = raw.get("pii_classify_mode", "shadow")

    cfg = CaseConfig(
        case_no=case_no,
        case_path=case_path,
        case_scope=raw.get("case_scope", ""),
        subject_identifier=SubjectIdentifier.from_dict(raw.get("subject_identifier")),
        rerank_mode=_resolve_mode(raw_rerank_mode, "RERANK_MODE", "rerank", VALID_RERANK_MODES),
        rerank_threshold=float(
            os.environ.get("RERANK_THRESHOLD", raw.get("rerank_threshold", 0.01))
        ),
        rerank_top_n=int(os.environ.get("RERANK_TOP_N", raw.get("rerank_top_n", 20))),
        rerank_sample_rate=float(
            os.environ.get("RERANK_SAMPLE_RATE", raw.get("rerank_sample_rate", 0.05))
        ),
        pii_classify_mode=_resolve_mode(raw_pii_mode, "PII_CLASSIFY_MODE", "pii", VALID_PII_MODES),
        pii_budget_usd=float(
            os.environ.get("DSAR_PII_BUDGET_USD", raw.get("pii_budget_usd", 10.0))
        ),
        discovery_enabled=_resolve_bool("DISCOVERY_ENABLED", raw.get("discovery_enabled", True)),
        redact_verify_enabled=_resolve_bool(
            "REDACT_VERIFY_ENABLED", raw.get("redact_verify_enabled", True)
        ),
        llm_concurrency=int(os.environ.get("DSAR_LLM_CONCURRENCY", raw.get("llm_concurrency", 5))),
    )
    return cfg


def _resolve_bool(env_var: str, config_value: bool) -> bool:
    env_value = os.environ.get(env_var)
    if env_value is None:
        return bool(config_value)
    return env_value.lower() in ("1", "true", "yes", "on")


def validate_phase_4_prereqs(cfg: CaseConfig) -> None:
    """Asserts subject_identifier is present + well-formed when Phase 4
    will run. Raises ValueError with a clear remediation message if not.
    """
    if cfg.pii_classify_mode == "off":
        return

    if cfg.subject_identifier is None:
        raise ValueError(
            f"Phase 4 PII classifier requires `subject_identifier` in "
            f"case_config.json for case {cfg.case_no}.\n"
            f"Add a JSON object with at minimum:\n"
            f'  {{"primary_name": "<full name>", '
            f'"disambiguation_notes": "<notes>"}}\n'
            f"…or set PII_CLASSIFY_MODE=off to skip Phase 4 for this case."
        )

    if not cfg.subject_identifier.primary_name.strip():
        raise ValueError(
            f"subject_identifier.primary_name is required and cannot be empty (case {cfg.case_no})."
        )
