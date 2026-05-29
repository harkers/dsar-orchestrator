"""Tests for the spec §2.5 threat-model content verifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dsar_orchestrator.exceptions import (
    ThreatModelIncompleteError,
    ThreatModelMissingError,
)
from dsar_orchestrator.pipeline import _verify_threat_model


@dataclass
class _StubAuditor:
    notes: list[tuple[str, str]] = field(default_factory=list)
    stages_run: list[str] = field(default_factory=list)
    case_no: str = "case-001"

    def note(self, stage, message):
        self.notes.append((stage, message))

    def write(self, *a, **kw):
        pass


def _make_cfg(case_path: Path):
    from dsar_orchestrator.config import CaseConfig

    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        fitness_check_enabled=False,
    )


_VALID_THREAT_MODEL = """# Threat model

## Embed endpoint
Local mlx-broker on 127.0.0.1:8090. No public DNS exposure. Subject
embeddings never leave the case directory or transit the network.

## Isolation posture
Per-engagement sparse-bundle mount at /Volumes/<client>/. Pipeline
binds only to local sockets. No shared state across engagements.

## Denylist scope
Per-case third_party_denylist.json, operator-curated via the
/people-register console. Not shared across engagements.

## Per-engagement data flow
Ingest -> people-register -> redact -> bake -> export. All artefacts
written to working/ within the sparse bundle. Nothing leaves the
bundle until the operator exports the deliverable pack.

## Subject identifier handling
data_subject.json holds the subject's full_name + aliases + emails +
optional subject_phones. The redactor's _build_denylist suppresses
these from every candidate source.
"""


def test_missing_file_raises(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ThreatModelMissingError, match="not found"):
        _verify_threat_model(cfg, _StubAuditor())


def test_valid_threat_model_passes(tmp_path: Path) -> None:
    working = tmp_path / "working"
    working.mkdir()
    (working / "threat_model.md").write_text(_VALID_THREAT_MODEL)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _verify_threat_model(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)


def test_missing_section_raises(tmp_path: Path) -> None:
    """Omitting one required section -> ThreatModelIncompleteError."""
    incomplete = _VALID_THREAT_MODEL.replace(
        "## Subject identifier handling\n",
        "## Some Unrelated Section\n",
        1,
    )
    working = tmp_path / "working"
    working.mkdir()
    (working / "threat_model.md").write_text(incomplete)
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ThreatModelIncompleteError, match="subject identifier handling"):
        _verify_threat_model(cfg, _StubAuditor())


def test_short_section_raises(tmp_path: Path) -> None:
    """A required section with <30 chars of content -> ThreatModelIncompleteError."""
    short = (
        "# Threat model\n"
        "## Embed endpoint\nlocal\n"  # <30 chars
        "## Isolation posture\nPer-engagement sparse-bundle mount with local-only sockets.\n"
        "## Denylist scope\nPer-case third_party_denylist.json operator-curated.\n"
        "## Per-engagement data flow\nIngest -> redact -> bake -> export within bundle.\n"
        "## Subject identifier handling\nSuppressed from every candidate via _build_denylist.\n"
    )
    working = tmp_path / "working"
    working.mkdir()
    (working / "threat_model.md").write_text(short)
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ThreatModelIncompleteError, match="under 30 chars"):
        _verify_threat_model(cfg, _StubAuditor())


def test_heading_normalisation_case_insensitive(tmp_path: Path) -> None:
    """Headings match regardless of case."""
    mixed_case = _VALID_THREAT_MODEL.replace("## Embed endpoint", "## EMBED ENDPOINT").replace(
        "## Subject identifier handling", "## subject identifier handling"
    )
    working = tmp_path / "working"
    working.mkdir()
    (working / "threat_model.md").write_text(mixed_case)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _verify_threat_model(cfg, auditor)  # must not raise
    assert any("OK" in m for _, m in auditor.notes)


def test_multiple_missing_sections_listed_in_error(tmp_path: Path) -> None:
    """Error message lists ALL missing sections, not just the first."""
    minimal = (
        "# Threat model\n"
        "## Embed endpoint\nLocal mlx-broker on 127.0.0.1:8090 — no public DNS exposure.\n"
        "## Isolation posture\nPer-engagement sparse-bundle mount at /Volumes/<client>/.\n"
    )
    working = tmp_path / "working"
    working.mkdir()
    (working / "threat_model.md").write_text(minimal)
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ThreatModelIncompleteError) as exc_info:
        _verify_threat_model(cfg, _StubAuditor())
    msg = str(exc_info.value)
    assert "denylist scope" in msg
    assert "per-engagement data flow" in msg
    assert "subject identifier handling" in msg


def test_h1_h2_h3_headings_all_recognised(tmp_path: Path) -> None:
    """The regex allows #, ##, ### (1-3 hash levels)."""
    h1_h3_mix = (
        "# Embed endpoint\n"
        "Local mlx-broker on 127.0.0.1:8090; no public DNS; embeddings stay local.\n"
        "### Isolation posture\n"
        "Per-engagement sparse-bundle mount at /Volumes/<client>/ with local sockets only.\n"
        "## Denylist scope\n"
        "Per-case third_party_denylist.json, operator-curated via /people-register.\n"
        "## Per-engagement data flow\n"
        "Ingest -> redact -> bake -> export all stay inside the sparse bundle.\n"
        "## Subject identifier handling\n"
        "data_subject.json identifiers suppressed via _build_denylist on every layer.\n"
    )
    working = tmp_path / "working"
    working.mkdir()
    (working / "threat_model.md").write_text(h1_h3_mix)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _verify_threat_model(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)


def test_h4_and_deeper_headings_ignored_in_section_count(tmp_path: Path) -> None:
    """A #### heading is NOT treated as a required-section heading
    (the regex caps at 3 hashes). If 'embed endpoint' only appears at
    #### level, it doesn't satisfy the requirement."""
    h4_only = _VALID_THREAT_MODEL.replace("## Embed endpoint", "#### Embed endpoint")
    working = tmp_path / "working"
    working.mkdir()
    (working / "threat_model.md").write_text(h4_only)
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ThreatModelIncompleteError, match="embed endpoint"):
        _verify_threat_model(cfg, _StubAuditor())
