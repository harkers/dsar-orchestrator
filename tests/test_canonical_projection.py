"""PR-1 scaffold — cross-repo canonical-projection snapshot tests.

Asserts that orchestrator's `pipeline.STAGE_ORDER` and `SUB_STAGES_BY_STAGE`
match the canonical projections in `dsar-toolkit`'s
`dsar_pipeline.stages_canonical` module.

Status: STUB. The canonical manifest lives in `dsar-toolkit` (toolkit-side
companion PR — separate). Until that ships and the orchestrator's pinned
toolkit version bumps to include it, this test module imports the
canonical lazily via `pytest.importorskip` and the suite skips. Once
canonical is available, the assertions activate and any drift between
orchestrator's hand-coded constants and the canonical fails CI.

See docs/superpowers/specs/2026-05-28-canonical-stage-manifest-design-v1.md.
"""

from __future__ import annotations

import pytest

stages_canonical = pytest.importorskip(
    "dsar_pipeline.stages_canonical",
    reason=(
        "canonical manifest not yet shipped in dsar-toolkit; "
        "see docs/superpowers/specs/2026-05-28-canonical-stage-manifest-design-v1.md"
    ),
)

from dsar_orchestrator.pipeline import STAGE_ORDER, SUB_STAGES_BY_STAGE  # noqa: E402


def test_automated_projection_matches_orchestrator_stage_order() -> None:
    assert tuple(stages_canonical.automated_stages()) == tuple(STAGE_ORDER)


def test_sub_stages_match_derived() -> None:
    assert SUB_STAGES_BY_STAGE == stages_canonical.derive_orchestrator_sub_stages_by_stage()


def test_canonical_validates() -> None:
    stages_canonical.validate_canonical()
