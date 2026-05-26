"""Tests for stage-rail enforcement: forward routes are gated by the
current pipeline phase.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.needs_toolkit


# --- Helpers ---


def _state(stage: str) -> dict:
    return {"current_stage": stage, "history": []}


# --- current_phase_key ---


def test_current_phase_key_for_intake() -> None:
    from dsar_orchestrator.operator_console import current_phase_key

    assert current_phase_key(_state("intake_created")) == "discovery"


def test_current_phase_key_for_dedupe() -> None:
    from dsar_orchestrator.operator_console import current_phase_key

    assert current_phase_key(_state("dedupe_running")) == "discovery"


def test_current_phase_key_for_scope_check() -> None:
    from dsar_orchestrator.operator_console import current_phase_key

    assert current_phase_key(_state("scope_check_running")) == "filter"


def test_current_phase_key_for_redaction() -> None:
    from dsar_orchestrator.operator_console import current_phase_key

    assert current_phase_key(_state("redaction_running")) == "redact"


def test_current_phase_key_for_human_review() -> None:
    from dsar_orchestrator.operator_console import current_phase_key

    assert current_phase_key(_state("human_review_pending")) == "release"


def test_current_phase_key_for_closed() -> None:
    from dsar_orchestrator.operator_console import current_phase_key

    assert current_phase_key(_state("closed")) == "release"


def test_current_phase_key_for_unknown_stage_defaults_discovery() -> None:
    from dsar_orchestrator.operator_console import current_phase_key

    assert current_phase_key(_state("fictional_stage")) == "discovery"


# --- is_route_accessible ---


def test_landing_always_accessible() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    for stage in (
        "intake_created",
        "context_running",
        "redaction_running",
        "human_review_pending",
    ):
        allowed, msg = is_route_accessible(_state(stage), "/")
        assert allowed is True
        assert msg is None


def test_pipeline_drilldown_always_accessible() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    # Read-only drilldowns are gate-free
    for path in ("/pipeline", "/audit", "/file"):
        for stage in ("intake_created", "human_review_pending"):
            allowed, _ = is_route_accessible(_state(stage), path)
            assert allowed is True, f"{path} should be accessible in {stage}"


def test_unextractable_accessible_in_discovery() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    # Ingestion has run; failed extracts need triage during discovery onward.
    allowed, _ = is_route_accessible(_state("ingestion_qc_running"), "/unextractable")
    assert allowed is True


def test_leak_review_blocked_in_discovery() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, msg = is_route_accessible(_state("context_running"), "/leak-review")
    assert allowed is False
    assert "Redact" in msg
    assert "Discovery" in msg


def test_leak_review_accessible_in_redact() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, _ = is_route_accessible(_state("redaction_running"), "/leak-review")
    assert allowed is True


def test_blockers_blocked_in_discovery() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, msg = is_route_accessible(_state("context_running"), "/blockers")
    assert allowed is False
    assert "Release" in msg


def test_blockers_blocked_in_redact() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, msg = is_route_accessible(_state("redaction_running"), "/blockers")
    assert allowed is False
    assert "Release" in msg


def test_blockers_accessible_in_release_phase() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, _ = is_route_accessible(_state("human_review_pending"), "/blockers")
    assert allowed is True
    allowed, _ = is_route_accessible(_state("release_gate_running"), "/blockers")
    assert allowed is True


def test_closure_letter_blocked_in_redact() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, msg = is_route_accessible(_state("redaction_qc_b_running"), "/closure-letter")
    assert allowed is False
    assert "Release" in msg


def test_closure_letter_accessible_in_release() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, _ = is_route_accessible(_state("release_gate_running"), "/closure-letter")
    assert allowed is True


def test_release_check_blocked_in_filter_phase() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    allowed, msg = is_route_accessible(_state("scope_check_running"), "/release-check")
    assert allowed is False
    assert "Release" in msg


def test_blocked_message_names_current_and_required_phases() -> None:
    from dsar_orchestrator.operator_console import is_route_accessible

    _, msg = is_route_accessible(_state("intake_created"), "/leak-review")
    assert msg is not None
    assert "Discovery" in msg  # current
    assert "Redact" in msg  # required
