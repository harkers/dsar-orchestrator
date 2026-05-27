"""#109 — two-panel redaction viewer (console-side overlay).

The viewer projects overlays from the existing ``<case>/working/<ref>_tags.json``
files (produced by ``pii_tagger_mini`` or the toolkit's ``detect.py``) at
render time. No toolkit changes; no new persisted artefacts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    case = tmp_path / "case01"
    (case / "working").mkdir(parents=True)
    (case / "audit").mkdir(parents=True)
    (case / "working" / "data_subject.json").write_text(
        json.dumps({"case_id": "CASE01", "full_name": "Jane Test"})
    )
    return case


def _write_tag_file(case_dir: Path, ref: str, entities: list[dict], **top_level) -> None:
    payload = {
        "ref": ref,
        "filename": top_level.get("filename", f"{ref}.docx"),
        "entity_count": len(entities),
        "redact_count": sum(1 for e in entities if e.get("redact") is True),
        "flag_count": sum(1 for e in entities if e.get("redact") == "flag"),
        "entities": entities,
    }
    payload.update(top_level)
    (case_dir / "working" / f"{ref}_tags.json").write_text(json.dumps(payload))


def test_classify_code_maps_classifications_to_v3_codes() -> None:
    from dsar_orchestrator.local_broker.redaction_viewer import classify_code

    assert classify_code({"classification": "data_subject", "redact": False}) == "DS"
    assert classify_code({"classification": "third_party", "redact": True}) == "TP"
    assert classify_code({"classification": "organisation", "redact": True}) == "CC"


def test_classify_code_flag_overrides_classification() -> None:
    """redact=='flag' means operator triage needed — always 'NR' regardless
    of underlying classification."""
    from dsar_orchestrator.local_broker.redaction_viewer import classify_code

    assert classify_code({"classification": "third_party", "redact": "flag"}) == "NR"
    assert classify_code({"classification": "data_subject", "redact": "flag"}) == "NR"
    assert classify_code({"classification": "organisation", "redact": "flag"}) == "NR"


def test_classify_code_unknown_classification_defaults_to_nr() -> None:
    """Unmapped classification → NR (operator should triage)."""
    from dsar_orchestrator.local_broker.redaction_viewer import classify_code

    assert classify_code({"classification": "contextual", "redact": False}) == "NR"
    assert classify_code({"classification": "", "redact": True}) == "NR"


def test_build_overlay_returns_entities_sorted_by_start(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.redaction_viewer import build_overlay

    _write_tag_file(
        case_dir,
        "doc_a",
        [
            {
                "text": "Bob",
                "start": 40,
                "end": 43,
                "classification": "third_party",
                "redact": True,
            },
            {
                "text": "Alice",
                "start": 10,
                "end": 15,
                "classification": "third_party",
                "redact": True,
            },
            {
                "text": "Acme",
                "start": 25,
                "end": 29,
                "classification": "organisation",
                "redact": True,
            },
        ],
    )

    overlay = build_overlay(case_dir, "doc_a")
    assert overlay["exists"] is True
    assert overlay["doc_ref"] == "doc_a"
    assert overlay["filename"] == "doc_a.docx"
    starts = [e["start"] for e in overlay["entities"]]
    assert starts == sorted(starts)
    assert [e["text"] for e in overlay["entities"]] == ["Alice", "Acme", "Bob"]
    assert [e["code"] for e in overlay["entities"]] == ["TP", "CC", "TP"]


def test_build_overlay_missing_tag_file_returns_empty_overlay(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.redaction_viewer import build_overlay

    overlay = build_overlay(case_dir, "nonexistent_ref")
    assert overlay == {
        "doc_ref": "nonexistent_ref",
        "filename": "",
        "exists": False,
        "entities": [],
    }


def test_build_overlay_skips_entries_missing_offsets(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.redaction_viewer import build_overlay

    _write_tag_file(
        case_dir,
        "doc_b",
        [
            {"text": "Eve", "classification": "third_party", "redact": True},
            {
                "text": "Mallory",
                "start": 5,
                "end": 12,
                "classification": "third_party",
                "redact": True,
            },
        ],
    )
    overlay = build_overlay(case_dir, "doc_b")
    assert len(overlay["entities"]) == 1
    assert overlay["entities"][0]["text"] == "Mallory"


def test_build_overlay_corrupt_tag_file_returns_empty(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.redaction_viewer import build_overlay

    (case_dir / "working" / "doc_c_tags.json").write_text("not valid json {{{")
    overlay = build_overlay(case_dir, "doc_c")
    assert overlay["exists"] is False
    assert overlay["entities"] == []


def test_render_redacted_html_emits_spans_for_redacted_entities(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.redaction_viewer import (
        build_overlay,
        render_redacted_html,
    )

    text = "Hello Alice, please contact Bob about Acme."
    _write_tag_file(
        case_dir,
        "doc_d",
        [
            {
                "text": "Alice",
                "start": 6,
                "end": 11,
                "classification": "third_party",
                "redact": True,
            },
            {
                "text": "Bob",
                "start": 28,
                "end": 31,
                "classification": "third_party",
                "redact": True,
            },
            {
                "text": "Acme",
                "start": 38,
                "end": 42,
                "classification": "organisation",
                "redact": True,
            },
        ],
    )
    overlay = build_overlay(case_dir, "doc_d")
    html_out = render_redacted_html(text, overlay)

    assert '<span data-code="TP" data-start="6" data-end="11">[TP]</span>' in html_out
    assert '<span data-code="TP" data-start="28" data-end="31">[TP]</span>' in html_out
    assert '<span data-code="CC" data-start="38" data-end="42">[CC]</span>' in html_out
    # Outside-overlay text preserved verbatim
    assert "Hello " in html_out
    assert ", please contact " in html_out
    # Original entity text NOT present (replaced by spans)
    assert "Alice" not in html_out
    assert "Bob" not in html_out
    assert "Acme" not in html_out


def test_render_redacted_html_does_not_redact_preserve_entities(case_dir: Path) -> None:
    """Entities with redact=False (e.g. data_subject) appear verbatim in
    the redacted view — the toolkit doesn't redact them either."""
    from dsar_orchestrator.local_broker.redaction_viewer import (
        build_overlay,
        render_redacted_html,
    )

    text = "Jane Test wrote to Bob."
    _write_tag_file(
        case_dir,
        "doc_e",
        [
            {
                "text": "Jane Test",
                "start": 0,
                "end": 9,
                "classification": "data_subject",
                "redact": False,
            },
            {
                "text": "Bob",
                "start": 19,
                "end": 22,
                "classification": "third_party",
                "redact": True,
            },
        ],
    )
    overlay = build_overlay(case_dir, "doc_e")
    html_out = render_redacted_html(text, overlay)

    assert "Jane Test" in html_out
    assert "Bob" not in html_out
    assert "[TP]" in html_out


def test_render_redacted_html_emits_nr_span_for_flagged_entity(case_dir: Path) -> None:
    """redact='flag' produces an NR-coded span so the operator can spot
    ambiguous flags in the redacted view."""
    from dsar_orchestrator.local_broker.redaction_viewer import (
        build_overlay,
        render_redacted_html,
    )

    text = "Discussed with WidgetCo today."
    _write_tag_file(
        case_dir,
        "doc_f",
        [
            {
                "text": "WidgetCo",
                "start": 15,
                "end": 23,
                "classification": "organisation",
                "redact": "flag",
            },
        ],
    )
    overlay = build_overlay(case_dir, "doc_f")
    html_out = render_redacted_html(text, overlay)
    assert '<span data-code="NR" data-start="15" data-end="23">[NR]</span>' in html_out


def test_render_redacted_html_escapes_outside_overlay_text(case_dir: Path) -> None:
    from dsar_orchestrator.local_broker.redaction_viewer import (
        build_overlay,
        render_redacted_html,
    )

    text = "<script>alert(1)</script> meets Bob."
    _write_tag_file(
        case_dir,
        "doc_g",
        [
            {
                "text": "Bob",
                "start": 32,
                "end": 35,
                "classification": "third_party",
                "redact": True,
            },
        ],
    )
    overlay = build_overlay(case_dir, "doc_g")
    html_out = render_redacted_html(text, overlay)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_redaction_viewer_route_returns_html_with_both_panes(case_dir: Path) -> None:
    """End-to-end: the /redaction-viewer/<ref> route renders both panes."""
    from dsar_orchestrator.operator_console import (
        CaseContext,
        render_redaction_viewer,
    )

    (case_dir / "working" / "doc_h.txt").write_text("Hello Alice and Bob.")
    _write_tag_file(
        case_dir,
        "doc_h",
        [
            {
                "text": "Alice",
                "start": 6,
                "end": 11,
                "classification": "third_party",
                "redact": True,
            },
            {
                "text": "Bob",
                "start": 16,
                "end": 19,
                "classification": "third_party",
                "redact": True,
            },
        ],
    )

    ctx = CaseContext(case_dir=case_dir)
    body = render_redaction_viewer(ctx, "doc_h")

    assert "doc_h" in body
    # Left pane shows original text verbatim
    assert "Hello Alice and Bob." in body
    # Right pane shows redacted spans
    assert '<span data-code="TP"' in body
    # Both-pane container marker
    assert 'class="pane-original"' in body or "pane-original" in body
    assert 'class="pane-redacted"' in body or "pane-redacted" in body


def test_redaction_viewer_route_missing_text_file_renders_with_empty_left_pane(
    case_dir: Path,
) -> None:
    from dsar_orchestrator.operator_console import (
        CaseContext,
        render_redaction_viewer,
    )

    # tag file exists but text file doesn't
    _write_tag_file(case_dir, "doc_i", [])
    ctx = CaseContext(case_dir=case_dir)
    body = render_redaction_viewer(ctx, "doc_i")
    assert "doc_i" in body
    assert "missing" in body.lower() or "no text" in body.lower()


def test_redaction_viewer_route_gated_by_redact_phase() -> None:
    """ROUTE_REQUIRED_PHASE / ROUTE_PREFIX_REQUIRED_PHASE blocks the route
    until the case has reached 'redact' phase."""
    from dsar_orchestrator.operator_console import is_route_accessible

    pre_redact_state = {"current_stage": "context_running"}
    allowed, msg = is_route_accessible(pre_redact_state, "/redaction-viewer/doc_z")
    assert allowed is False, f"expected blocked pre-redact; got allowed={allowed} msg={msg!r}"

    in_redact_state = {"current_stage": "redaction_qc_a_running"}
    allowed, msg = is_route_accessible(in_redact_state, "/redaction-viewer/doc_z")
    assert allowed is True
