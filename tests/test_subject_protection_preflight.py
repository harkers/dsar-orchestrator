"""Tests for the spec §1.6 conductor subject-protection cross-check."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dsar_orchestrator.exceptions import SubjectInDenylistPipelineError
from dsar_orchestrator.pipeline import _run_subject_protection_preflight


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


def _seed_denylist(case_path: Path) -> None:
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    (working / "third_party_denylist.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "controller": "ProPharma",
                "populated_at": "2026-05-28T09:00:00Z",
                "operator_id": "op-1",
                "entries": [
                    {
                        "canonical_name": "Alice Other",
                        "redact": True,
                        "operator_note": "",
                        "people_register_cluster_id": "abc123",
                    }
                ],
            }
        )
    )
    (working / "data_subject.json").write_text(
        json.dumps(
            {
                "full_name": "Subject A",
                "aliases": [],
                "email": "s@x.com",
                "additional_emails": [],
                "subject_protected_phrases": [],
            }
        )
    )


def test_skipped_when_denylist_missing(tmp_path: Path) -> None:
    """No third_party_denylist.json yet -> silent skip + note."""
    (tmp_path / "working").mkdir()
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _run_subject_protection_preflight(cfg, auditor)
    assert any("skipped" in m.lower() for _, m in auditor.notes)


def test_passes_when_no_subject_match(tmp_path: Path, monkeypatch) -> None:
    """Stub the adapter to return orthogonal vectors -> no fuzzy match -> OK."""
    _seed_denylist(tmp_path)
    from dsar_pipeline import tei_embed_model_adapter as tem

    class _StubAdapter:
        manifest_signature_id = "a" * 64

        def embed(self, texts):
            # Fixed-dim (16) unit vectors, slot chosen by hash of text so
            # distinct strings map to different basis slots (orthogonal).
            # Dimension is constant across calls regardless of batch size.
            import hashlib

            dim = 16
            out = []
            for t in texts:
                slot = int(hashlib.md5(t.encode()).hexdigest(), 16) % dim
                v = [0.0] * dim
                v[slot] = 1.0
                out.append(v)
            return out

    monkeypatch.setattr(tem, "TeiEmbedModelAdapter", lambda: _StubAdapter())
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _run_subject_protection_preflight(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)


def test_raises_when_denylist_contains_subject(tmp_path: Path, monkeypatch) -> None:
    """Stub embed -> identical vectors -> cosine 1.0 -> SubjectInDenylistError."""
    working = tmp_path / "working"
    working.mkdir()
    (working / "third_party_denylist.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "controller": "ProPharma",
                "populated_at": "2026-05-28T09:00:00Z",
                "operator_id": "op-1",
                "entries": [
                    {
                        "canonical_name": "Subject A",
                        "redact": True,
                        "operator_note": "",
                        "people_register_cluster_id": "x",
                    }
                ],
            }
        )
    )
    (working / "data_subject.json").write_text(
        json.dumps(
            {
                "full_name": "Subject A",
                "aliases": [],
                "email": "s@x.com",
                "additional_emails": [],
                "subject_protected_phrases": [],
            }
        )
    )
    from dsar_pipeline import tei_embed_model_adapter as tem

    class _IdenticalAdapter:
        manifest_signature_id = "a" * 64

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(tem, "TeiEmbedModelAdapter", lambda: _IdenticalAdapter())
    cfg = _make_cfg(tmp_path)
    with pytest.raises(SubjectInDenylistPipelineError, match="cross-check failed"):
        _run_subject_protection_preflight(cfg, _StubAuditor())


def test_soft_warns_when_tei_unavailable(tmp_path: Path, monkeypatch) -> None:
    """TEI server down -> SOFT WARNING + audit note; does NOT raise."""
    _seed_denylist(tmp_path)
    from dsar_pipeline import tei_embed_model_adapter as tem

    class _UnavailableAdapter:
        @property
        def manifest_signature_id(self):
            raise tem.EmbedModelUnavailableError("connection refused")

        def embed(self, texts):
            raise tem.EmbedModelUnavailableError("connection refused")

    monkeypatch.setattr(tem, "TeiEmbedModelAdapter", lambda: _UnavailableAdapter())
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _run_subject_protection_preflight(cfg, auditor)  # must not raise
    assert any("WARN" in m and "TEI" in m for _, m in auditor.notes)


def test_soft_warns_when_adapter_construction_raises(tmp_path: Path, monkeypatch) -> None:
    """DeepSeek convergent jury finding: if the TeiEmbedModelAdapter
    CONSTRUCTOR raises (e.g. a future implementation that probes TEI
    eagerly), that must also surface as a soft warning — NOT crash the
    preflight."""
    _seed_denylist(tmp_path)
    from dsar_pipeline import tei_embed_model_adapter as tem

    def _construct_raises():
        raise tem.EmbedModelUnavailableError("TEI down at construction")

    monkeypatch.setattr(tem, "TeiEmbedModelAdapter", _construct_raises)
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _run_subject_protection_preflight(cfg, auditor)  # must not raise
    assert any("WARN" in m and "TEI" in m for _, m in auditor.notes)
