"""Tests for the spec §2.1 people-register preflight gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dsar_orchestrator.exceptions import (
    PeopleRegisterBuildError,
    PeopleRegisterEmptyError,
)
from dsar_orchestrator.pipeline import (
    _corpus_has_communicants,
    _run_people_register_preflight,
)


@dataclass
class _StubAuditor:
    """Test double for PipelineAuditor — satisfies StageBanner + note()."""

    case_no: str = "TEST"
    notes: list[tuple[str, str]] = field(default_factory=list)
    stages_run: list[str] = field(default_factory=list)

    def note(self, stage: str, message: str, **_extra) -> None:
        self.notes.append((stage, message))

    def write(self, row: dict) -> None:  # noqa: ARG002
        pass

    def mark_skipped(self, stage: str, reason: str) -> None:
        pass

    def mark_halted(self, reason: str) -> None:
        pass


def _make_cfg(case_path: Path, *, enabled: bool = True, skip_reason: str | None = None):
    """Build a minimal CaseConfig stub. Import lazily to honour test isolation."""
    from dsar_orchestrator.config import CaseConfig

    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        people_register_enabled=enabled,
        force_skip_people_register_reason=skip_reason,
        fitness_check_enabled=False,  # don't run sibling preflight
    )


# ---- _corpus_has_communicants ----


def test_communicants_true_with_mailbox_owner(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "register.json").write_text(
        json.dumps(
            [
                {"ref": "r1", "mailbox_owner_email": "alice@example.com"},
            ]
        )
    )
    assert _corpus_has_communicants(tmp_path) is True


def test_communicants_true_with_source_kind(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "register.json").write_text(
        json.dumps(
            [
                {
                    "ref": "r1",
                    "mailbox_owner_email": None,
                    "source_kind": "exchange_loose",
                },
            ]
        )
    )
    assert _corpus_has_communicants(tmp_path) is True


def test_communicants_true_with_eml_file(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "register.json").write_text(
        json.dumps(
            [
                {"ref": "r1", "mailbox_owner_email": None, "filename": "msg.eml"},
            ]
        )
    )
    assert _corpus_has_communicants(tmp_path) is True


def test_communicants_false_when_register_missing(tmp_path: Path) -> None:
    assert _corpus_has_communicants(tmp_path) is False


def test_communicants_false_on_empty_register(tmp_path: Path) -> None:
    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "register.json").write_text("[]")
    assert _corpus_has_communicants(tmp_path) is False


def test_communicants_false_on_unstructured_docs(tmp_path: Path) -> None:
    """A .pdf with no source_kind doesn't signal communicants."""
    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "register.json").write_text(
        json.dumps(
            [
                {"ref": "r1", "filename": "random.pdf"},
            ]
        )
    )
    assert _corpus_has_communicants(tmp_path) is False


# ---- _run_people_register_preflight ----


def test_preflight_skipped_when_disabled(tmp_path: Path) -> None:
    """people_register_enabled=False -> no-op + audit note."""
    cfg = _make_cfg(tmp_path, enabled=False)
    auditor = _StubAuditor()
    _run_people_register_preflight(cfg, auditor)
    assert any("skipped by config" in m for _, m in auditor.notes)


def test_preflight_force_skip_emits_audit_event(tmp_path: Path) -> None:
    """force_skip_people_register_reason='synthetic-test' -> bypass + audit."""
    (tmp_path / "working").mkdir()
    cfg = _make_cfg(tmp_path, skip_reason="synthetic-test, no real corpus")
    auditor = _StubAuditor()
    _run_people_register_preflight(cfg, auditor)
    assert any("force_skip_people_register" in m for _, m in auditor.notes)


def test_preflight_passes_on_valid_register(tmp_path: Path) -> None:
    """A register with third-party clusters passes the preflight."""
    working = tmp_path / "working"
    working.mkdir()
    (working / "register.json").write_text(
        json.dumps(
            [
                {
                    "ref": "r1",
                    "mailbox_owner_email": "alice@example.com",
                    "mailbox_owner_display": "Alice",
                    "source_kind": "exchange",
                }
            ]
        )
    )
    (working / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com", "aliases": []})
    )
    (working / "people_register.json").write_text(
        json.dumps(
            [
                {
                    "canonical_name": "Alice",
                    "emails": ["alice@example.com"],
                    "phones": [],
                    "titles": [],
                    "source_refs": ["r1"],
                    "correlation_ids": [],
                    "mention_count": 1,
                    "distinct_doc_count": 1,
                    "confidence_score": 1.0,
                    "discovered_by": "x",
                    "is_data_subject": False,
                    "is_subject_confidence": 0.0,
                    "subject_centricity_score": 0.0,
                    "text_quality_summary": "unknown",
                }
            ]
        )
    )
    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _run_people_register_preflight(cfg, auditor)
    assert any("OK" in m for _, m in auditor.notes)


def test_preflight_empty_register_with_communicants_raises(tmp_path: Path) -> None:
    """Spec §2.1: empty register + non-empty communicant corpus = case-301770
    silent-empty class -> PeopleRegisterEmptyError."""
    working = tmp_path / "working"
    working.mkdir()
    (working / "register.json").write_text(
        json.dumps(
            [
                {
                    "ref": "r1",
                    "mailbox_owner_email": None,
                    "source_kind": "exchange_loose",
                },
            ]
        )
    )
    (working / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com", "aliases": []})
    )
    (working / "people_register.json").write_text("[]")  # empty register

    cfg = _make_cfg(tmp_path)
    with pytest.raises(
        PeopleRegisterEmptyError,
        match="case-301770|silent|misdetect|extraction failure",
    ):
        _run_people_register_preflight(cfg, _StubAuditor())


def test_preflight_empty_register_with_empty_corpus_passes(tmp_path: Path) -> None:
    """Empty corpus + empty register is FINE — nothing to redact."""
    working = tmp_path / "working"
    working.mkdir()
    (working / "register.json").write_text("[]")
    (working / "people_register.json").write_text("[]")
    (working / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com", "aliases": []})
    )

    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _run_people_register_preflight(cfg, auditor)
    # Doesn't raise; doesn't necessarily emit OK (no third-party clusters)


def test_preflight_auto_builds_missing_register(tmp_path: Path, monkeypatch) -> None:
    """If people_register.json doesn't exist, the preflight calls
    build_people_register. We mock it to simulate successful build."""
    working = tmp_path / "working"
    working.mkdir()
    (working / "register.json").write_text(
        json.dumps(
            [
                {
                    "ref": "r1",
                    "mailbox_owner_email": "alice@example.com",
                    "source_kind": "exchange",
                },
            ]
        )
    )
    (working / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com", "aliases": []})
    )

    def fake_build(case_dir):
        (case_dir / "working" / "people_register.json").write_text(
            json.dumps(
                [
                    {
                        "canonical_name": "Alice",
                        "emails": ["alice@example.com"],
                        "phones": [],
                        "titles": [],
                        "source_refs": ["r1"],
                        "correlation_ids": [],
                        "mention_count": 1,
                        "distinct_doc_count": 1,
                        "confidence_score": 1.0,
                        "discovered_by": "x",
                        "is_data_subject": False,
                        "is_subject_confidence": 0.0,
                        "subject_centricity_score": 0.0,
                        "text_quality_summary": "unknown",
                    }
                ]
            )
        )

    import dsar_pipeline.build_people_register as bpr_mod

    monkeypatch.setattr(bpr_mod, "build_people_register", fake_build)

    cfg = _make_cfg(tmp_path)
    auditor = _StubAuditor()
    _run_people_register_preflight(cfg, auditor)
    # Build was triggered; OK note recorded
    assert (working / "people_register.json").exists()


def test_preflight_build_failure_raises(tmp_path: Path, monkeypatch) -> None:
    """If build_people_register raises OR doesn't produce a file ->
    PeopleRegisterBuildError."""
    (tmp_path / "working").mkdir()
    (tmp_path / "working" / "register.json").write_text("[]")
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "aliases": [], "email": "s@x.com"})
    )

    def fake_build(case_dir):
        # No-op — never writes the register
        return

    import dsar_pipeline.build_people_register as bpr_mod

    monkeypatch.setattr(bpr_mod, "build_people_register", fake_build)

    cfg = _make_cfg(tmp_path)
    with pytest.raises(PeopleRegisterBuildError):
        _run_people_register_preflight(cfg, _StubAuditor())
