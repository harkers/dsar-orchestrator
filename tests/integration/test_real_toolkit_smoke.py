"""Real-toolkit smoke test for the conductor (issue #8).

Exercises the conductor against the REAL dsar-toolkit (not hermetic stubs)
through the early stages: ingest + embed. Catches register.json shape
divergences that hermetic tests can't surface (which was exactly the
class of bug fixed in #8).

Gated behind `@pytest.mark.needs_toolkit`. CI doesn't select this marker
by default — run via `pytest -m needs_toolkit`. Local prerequisites:
- dsar-toolkit editable-installed
- TEI embed service reachable at http://127.0.0.1:8085
- mlx-broker not required for these stages

Stops after embed (which is the first stage to actually exercise the
register-shape contract). LLM stages (scope_classify, pii_classify)
need mlx-broker and are out of scope for this smoke.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from dsar_orchestrator.pipeline import run
from dsar_orchestrator.synthesis import synthesize_case


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Cheap TCP probe — does NOT validate the service speaks HTTP."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture
def real_synthetic_case(tmp_path):
    """Generate a 4-doc synthetic case under tmp_path/cases/."""
    case_root = tmp_path / "cases"
    case_root.mkdir(parents=True)
    return synthesize_case("700100", case_root, doc_count=4, seed=42)


@pytest.mark.needs_toolkit
def test_real_toolkit_ingest_through_embed(real_synthetic_case, monkeypatch) -> None:
    """End-to-end test against the real toolkit. Confirms:

    - The conductor's ingest adapter handles the toolkit's flat-list
      register shape (the bug #8 fixed)
    - register_meta.json sidecar gets written by the conductor's adapter
    - The embed stage can iterate the list-shape register, find each
      ref's extracted text at working/<ref>.txt, and successfully
      embed via real TEI
    """
    # Self-gate: skip if any required infra isn't available.
    if not _port_open("127.0.0.1", 8085):
        pytest.skip("TEI embed service not reachable on :8085")
    try:
        import dsar_pipeline  # noqa: F401
    except ImportError:
        pytest.skip("dsar-toolkit (dsar_pipeline) not installed")
    try:
        import spacy

        spacy.load("en_core_web_sm")
    except (ImportError, OSError):
        pytest.skip("spaCy en_core_web_sm model not installed (toolkit detect stage requires it)")

    case = real_synthetic_case
    # Redirect HOME so the audit log goes under tmp_path
    monkeypatch.setenv("HOME", str(case.case_path.parent.parent.parent))

    # Run up to (and including) stage_2_parallel — skip LLM stages.
    report = run(
        case.case_no,
        case_root=case.case_path,
        through_stage="stage_2_parallel",
    )

    # Ingest produced the toolkit-shape register.json (flat list)
    register_path = case.case_path / "working" / "register.json"
    assert register_path.exists()
    import json

    register = json.loads(register_path.read_text())
    assert isinstance(register, list), "register.json must be a flat list (Contract A / #8)"
    assert len(register) == 4
    assert all("ref" in entry for entry in register)

    # Conductor's adapter stamped the sibling meta file
    meta_path = case.case_path / "working" / "register_meta.json"
    assert meta_path.exists(), "ingest adapter should write register_meta.json sidecar"
    meta = json.loads(meta_path.read_text())
    assert len(meta["upstream_hash"]) == 64  # sha256 hex
    assert meta["schema_version"] == "1.0"

    # Embed wrote per-ref vectors
    embeddings_path = case.case_path / "working" / "embeddings.jsonl"
    assert embeddings_path.exists()
    lines = embeddings_path.read_text().splitlines()
    assert len(lines) == 4
    for line in lines:
        row = json.loads(line)
        assert "ref" in row
        assert "embedding" in row
        assert len(row["embedding"]) > 0  # actual vector from TEI

    # Stages ran (not skipped)
    assert "ingest" in report.stages_run
    assert "stage_2_parallel" in report.stages_run


@pytest.mark.needs_toolkit
def test_real_toolkit_ingest_only(real_synthetic_case, monkeypatch) -> None:
    """Narrow ingest-only smoke. Catches the exact bug in #8 without
    requiring spaCy / TEI / mlx-broker — just dsar-pipeline.ingest.
    Should run in any env where dsar-toolkit is editable-installed."""
    try:
        import dsar_pipeline  # noqa: F401
    except ImportError:
        pytest.skip("dsar-toolkit (dsar_pipeline) not installed")

    case = real_synthetic_case
    monkeypatch.setenv("HOME", str(case.case_path.parent.parent.parent))

    report = run(
        case.case_no,
        case_root=case.case_path,
        through_stage="ingest",
    )

    # Toolkit produced flat-list register
    import json

    register = json.loads((case.case_path / "working" / "register.json").read_text())
    assert isinstance(register, list)
    assert len(register) == 4

    # Conductor's adapter wrote the meta sidecar — this is the exact
    # code path that crashed with AttributeError before issue #8 fix.
    meta = json.loads((case.case_path / "working" / "register_meta.json").read_text())
    assert len(meta["upstream_hash"]) == 64

    assert "ingest" in report.stages_run
