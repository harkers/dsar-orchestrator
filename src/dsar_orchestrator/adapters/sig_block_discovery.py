"""Conductor adapter for sig_block_discovery_stage (Phase 3 Task 2).

Bridges to the toolkit's
``dsar_pipeline.sig_block_discovery_stage.run_sig_block_discovery``.
Runs the regex post-pass that catches signature-block content the
structured RFC822 + sig-region extractor missed (case-301770 leak
class) and merges results back into working/people_register.json.

Retirement: when the toolkit ships a thin
``dsar_pipeline.sig_block_discovery_stage.run_for_case(case_path)``
entry that matches the conductor's adapter contract, this adapter
retires.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from dsar_orchestrator.config import CaseConfig

PRODUCER_VERSION = "dsar_orchestrator.adapters.sig_block_discovery 0.1.0"

RunFn = Callable[[Path], dict[str, Any]]


def run_for_case(cfg: CaseConfig, *, run_fn: RunFn | None = None) -> None:
    """Invoke the toolkit's run_sig_block_discovery on the case dir."""
    if run_fn is None:
        from dsar_pipeline.sig_block_discovery_stage import run_sig_block_discovery

        run_fn = run_sig_block_discovery
    run_fn(cfg.case_path)
