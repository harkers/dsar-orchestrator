"""dsar-orchestrator — conductor for the dsar-toolkit modular pipeline.

See docs/superpowers/specs/2026-05-22-pipeline-orchestration-design-v2.md
for the authoritative design.
"""

__version__ = "0.1.0"

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
