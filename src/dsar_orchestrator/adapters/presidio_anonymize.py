"""Conductor adapter for the Presidio Anonymizer redaction stage.

Bridges to ``dsar_pipeline.presidio_anonymize_stage.run_presidio_anonymize``.
Always-on extra defense-in-depth: writes working/<ref>.anonymized.txt
per ref with cluster-aware labels ([PERSON_C<N>]) and subject
preservation.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from dsar_orchestrator.config import CaseConfig

PRODUCER_VERSION = "dsar_orchestrator.adapters.presidio_anonymize 0.1.0"

RunFn = Callable[[Path], dict[str, Any]]


def run_for_case(cfg: CaseConfig, *, run_fn: RunFn | None = None) -> None:
    """Invoke the toolkit's run_presidio_anonymize on the case dir."""
    if run_fn is None:
        from dsar_pipeline.presidio_anonymize_stage import run_presidio_anonymize

        run_fn = run_presidio_anonymize
    run_fn(cfg.case_path)
