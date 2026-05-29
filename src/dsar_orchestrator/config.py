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
from typing import Literal, Optional

VALID_RERANK_MODES = {"off", "shadow", "enforce"}
VALID_PII_MODES = {"off", "shadow", "enforce"}

# Phase 6 — people-register-hardening Literal sets
_VALID_PII_JURY_SAMPLING: frozenset[str] = frozenset({"full", "tiered", "spot_check"})
_VALID_PII_JURY_DISAGREEMENT: frozenset[str] = frozenset({"operator_review", "redact_safer"})


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

    # Test / synthetic case marker. Set by dsar-synthesize-case in the
    # generated case_config.json. The bake adapter consults this to
    # auto-resolve detect-stage flag entries (which have no human in
    # the loop for synthetic data). Real operator cases default False
    # and must resolve flags explicitly. See dsar-orchestrator#18.
    synthetic: bool = False

    # Operator opt-in for non-interactive flag resolution on real cases.
    # When None (default), the bake adapter's pre-check halts with an
    # actionable message listing pending flags. When set to "true" or
    # "false", the adapter auto-resolves all pending flags to that
    # value before invoking bake — operator explicitly accepts the
    # default. Set via --resolve-flags-as CLI flag or
    # DSAR_RESOLVE_FLAGS_AS env var. See dsar-orchestrator#26.
    resolve_flags_as: str | None = None

    # Phase 5 — model-fitness canary pre-flight (spec §4.4 + §10.2).
    #
    # YAML schema (all keys optional; defaults preserve current behaviour):
    #
    #   {
    #     ...,
    #     "fitness_check_enabled": true,          # default true; gate is ON
    #     "fitness_check_canary_path": null,      # default ~/.dsar/canary_sets/<deployment_id>
    #     "fitness_check_max_report_age_days": 30,
    #     "force_skip_fitness_reason": ""          # non-blank string bypasses + audits
    #   }
    #
    # The pre-flight halts the run if a matching fresh+passing fitness
    # report does not exist. Operators can:
    #   - opt out per-case via `fitness_check_enabled: false`
    #   - bypass with audit via `force_skip_fitness_reason: "<reason>"` (non-blank)
    #   - run the canary inline via the CLI's `--auto-fitness` flag
    fitness_check_enabled: bool = True
    fitness_check_canary_path: Path | None = None
    fitness_check_max_report_age_days: int = 30
    force_skip_fitness_reason: str = ""

    # Phase 6 — people-register-hardening (spec §2.2)
    #
    # YAML schema (all keys optional; defaults preserve current behaviour):
    #
    #   {
    #     ...,
    #     "people_register_enabled": true,
    #     "force_skip_people_register_reason": null,
    #     "pii_jury_dual_juror": false,
    #     "pii_jury_sampling": "tiered",
    #     "pii_jury_disagreement_policy": "operator_review",
    #     "subject_protection_cache_max_mb": 50
    #   }

    # Master switch for Phase 6 preflight. False = preflight skipped entirely
    # (useful for synthetic-test cases that have no real people-register).
    people_register_enabled: bool = True

    # Explicit operator bypass (mirrors force_skip_fitness_reason).
    # Non-empty string → preflight emits SKIP audit event and proceeds.
    # None (or empty string) means no skip; preflight runs normally.
    force_skip_people_register_reason: Optional[str] = None

    # Explicit operator opt-in for dual-juror mode (Phase 5 Task 3).
    pii_jury_dual_juror: bool = False

    # Sampling policy for the PII jury.
    # "tiered"     → spec §1.8 16-cell JURY_SAMPLE_RATES matrix (default).
    # "full"       → rate=1.0 across all bins.
    # "spot_check" → rate=0.05 across all bins.
    pii_jury_sampling: Literal["full", "tiered", "spot_check"] = "tiered"

    # Disagreement resolution policy for the PII jury.
    # "operator_review" → manual triage (current Phase 5 behaviour).
    # "redact_safer"    → auto-resolve by picking the stricter verdict (v2).
    pii_jury_disagreement_policy: Literal["operator_review", "redact_safer"] = "operator_review"

    # LRU trim cap for gates/subject_protection.py cache (spec §1.6). Must be > 0.
    subject_protection_cache_max_mb: int = 50


def _validate_literal(name: str, value: str, valid: frozenset[str]) -> str:
    """Raise ValueError if *value* is not in *valid*; return *value* otherwise."""
    if value not in valid:
        raise ValueError(f"case_config field {name!r}={value!r}; expected one of {sorted(valid)}")
    return value


def _validate_cache_mb(value: int) -> int:
    """Raise ValueError if *value* is not > 0."""
    if value <= 0:
        raise ValueError(
            f"case_config field 'subject_protection_cache_max_mb'={value!r}; must be > 0"
        )
    return value


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
        # Deprecated in v0.4.0 (Contract B / #10). Kept as no-op for one
        # release; removal target = v0.5.0. The pii_discovery stage no
        # longer exists.
        discovery_enabled=bool(raw.get("discovery_enabled", True)),
        redact_verify_enabled=_resolve_bool(
            "REDACT_VERIFY_ENABLED", raw.get("redact_verify_enabled", True)
        ),
        llm_concurrency=int(os.environ.get("DSAR_LLM_CONCURRENCY", raw.get("llm_concurrency", 5))),
        synthetic=bool(raw.get("synthetic", False)),
        resolve_flags_as=_normalise_resolve_flags(
            os.environ.get("DSAR_RESOLVE_FLAGS_AS", raw.get("resolve_flags_as"))
        ),
        fitness_check_enabled=bool(raw.get("fitness_check_enabled", True)),
        fitness_check_canary_path=(
            Path(raw["fitness_check_canary_path"]).expanduser()
            if raw.get("fitness_check_canary_path")
            else None
        ),
        fitness_check_max_report_age_days=int(raw.get("fitness_check_max_report_age_days", 30)),
        force_skip_fitness_reason=str(
            os.environ.get(
                "DSAR_FORCE_SKIP_FITNESS_REASON",
                raw.get("force_skip_fitness_reason", "") or "",
            )
        ),
        # Phase 6 — people-register-hardening
        people_register_enabled=bool(raw.get("people_register_enabled", True)),
        force_skip_people_register_reason=raw.get("force_skip_people_register_reason") or None,
        pii_jury_dual_juror=bool(raw.get("pii_jury_dual_juror", False)),
        pii_jury_sampling=_validate_literal(
            "pii_jury_sampling",
            raw.get("pii_jury_sampling", "tiered"),
            _VALID_PII_JURY_SAMPLING,
        ),
        pii_jury_disagreement_policy=_validate_literal(
            "pii_jury_disagreement_policy",
            raw.get("pii_jury_disagreement_policy", "operator_review"),
            _VALID_PII_JURY_DISAGREEMENT,
        ),
        subject_protection_cache_max_mb=_validate_cache_mb(
            int(raw.get("subject_protection_cache_max_mb", 50))
        ),
    )
    return cfg


def _normalise_resolve_flags(value: object) -> str | None:
    """Coerce raw value to one of {None, "true", "false"}; raise on
    anything else. Issue #26."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    sv = str(value).strip().lower()
    if sv in ("", "none"):
        return None
    if sv in ("true", "1", "yes", "on"):
        return "true"
    if sv in ("false", "0", "no", "off"):
        return "false"
    raise ValueError(f"resolve_flags_as must be one of None/true/false (got {value!r})")


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
