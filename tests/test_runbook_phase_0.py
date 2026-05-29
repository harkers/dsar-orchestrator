"""Smoke tests for the Phase 6 Task 6 runbook + status-dashboard assets."""

from pathlib import Path

# Repo root derived from this test file's location — portable across
# clones, CI, and other machines (DeepSeek convergent jury finding).
REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "runbooks" / "RELEASE_TEMPLATE.md"
SNIPPET = REPO / "docs" / "runbooks" / "status-dashboard-people-register.sh.snippet"


def test_runbook_template_exists():
    assert RUNBOOK.exists()


def test_runbook_has_phase_0_section():
    content = RUNBOOK.read_text()
    assert "Phase 0 — People-register pre-flight (MANDATORY)" in content


def test_runbook_lists_5_required_threat_model_sections():
    content = RUNBOOK.read_text()
    for section in (
        "Embed endpoint",
        "Isolation posture",
        "Denylist scope",
        "Per-engagement data flow",
        "Subject identifier handling",
    ):
        assert section in content, f"missing {section!r}"


def test_runbook_references_verify_subcommand():
    content = RUNBOOK.read_text()
    assert "dsar-conductor verify" in content
    assert "--check people-register" in content


def test_runbook_includes_phase_6_closure_letter_line():
    content = RUNBOOK.read_text()
    assert "People-register pre-flight passed (Phase 0)" in content


def test_status_dashboard_snippet_exists():
    assert SNIPPET.exists()


def test_status_dashboard_snippet_references_register():
    content = SNIPPET.read_text()
    assert "people_register.json" in content
    assert "third_party_denylist.json" in content
    assert "pre-redact gate" in content


def test_status_dashboard_snippet_handles_missing_register():
    content = SNIPPET.read_text()
    assert "BLOCKED" in content
    assert "dsar-build-people-register" in content
