"""dsar-orchestrator — conductor for the dsar-toolkit modular pipeline.

See docs/superpowers/specs/2026-05-24-pipeline-orchestration-design-v5.md
for the authoritative design.

Contracts that govern this package's coupling to the toolkit are
documented in VERSIONING.md (§2 schema, §3 producer, §4 toolkit-coupling).
Contract A (#8) and Contract B (#10/#11/#12) are the established
precedents — read §4 before adding a new adapter or `_lazy_import`.
"""

__version__ = "0.4.3"

from dsar_orchestrator.exceptions import (
    BudgetExceededError,
    DSARPipelineError,
    PipelineHalt,
    UpstreamHashMismatch,
)

__all__ = [
    "__version__",
    "BudgetExceededError",
    "DSARPipelineError",
    "PipelineHalt",
    "UpstreamHashMismatch",
]
