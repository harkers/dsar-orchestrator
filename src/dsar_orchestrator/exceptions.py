"""Typed exceptions raised by the orchestrator.

Per the orchestration spec § Failure handling, the orchestrator never
silently degrades. Every failure surfaces a typed exception with
case-no, stage, and a recovery instruction.
"""


class DSARPipelineError(Exception):
    """Base for all orchestrator-raised errors."""


class PipelineHalt(DSARPipelineError):
    """Stage 8 (Phase 6 verify-pdf) flagged a failure. Case stays
    in working/, never reaches output/. Raised once per case run."""


class BudgetExceededError(DSARPipelineError):
    """Phase 4 PII classifier hit the DSAR_PII_BUDGET_USD cap mid-case.
    Operator decides to raise the cap or abandon the case."""


class UpstreamHashMismatch(DSARPipelineError):
    """An artefact's recorded upstream_hash doesn't match the current
    upstream state. Message includes the artefact path + the re-run
    instruction that would resolve it."""


class PeopleRegisterBuildError(PipelineHalt):
    """Spec §2.1: build_people_register didn't produce a usable register."""


class PeopleRegisterEmptyError(PipelineHalt):
    """Spec §2.1: register has zero third-party clusters on a non-empty
    communicant corpus — the case-301770 silent-empty class of bug."""
