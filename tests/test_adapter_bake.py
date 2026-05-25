"""Tests for the bake adapter — `adapters.bake`.

Adapter shells out to `dsar-bake --case <id>`. Subprocess runner is
injectable so tests are hermetic.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import bake as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
    )


def _ok_completed(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")


def test_invokes_dsar_bake_with_case_flag(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    (case_path / "working").mkdir(parents=True)
    (case_path / "working" / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc1.pdf").write_text("baked")

    captured = []

    def runner(argv, env, cwd):
        captured.append((tuple(argv), Path(cwd)))
        return _ok_completed(argv)

    adapter.run_for_case(_make_cfg(case_path), runner=runner)
    assert captured == [(("dsar-bake", "--case", "700100"), case_path)]


def test_writes_manifest_with_upstream_hash(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    (case_path / "working").mkdir(parents=True)
    (case_path / "working" / "redaction_input.jsonl").write_text('{"a":1}\n')
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc1.pdf").write_text("redacted-content")

    adapter.run_for_case(_make_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))

    manifest_path = case_path / "working" / "redact_v4" / "bake_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["completed"] is True
    assert manifest["upstream_hash"]  # non-empty
    assert manifest["schema_version"] == "1.0"
    assert "producer_version" in manifest


def test_raises_on_subprocess_nonzero_exit(tmp_path: Path) -> None:
    case_path = tmp_path / "700100"
    (case_path / "working").mkdir(parents=True)

    def failing_runner(argv, env, cwd):
        return subprocess.CompletedProcess(
            args=argv, returncode=2, stdout="", stderr="bake exploded\n"
        )

    with pytest.raises(DSARPipelineError, match="bake CLI exited 2"):
        adapter.run_for_case(_make_cfg(case_path), runner=failing_runner)


def test_raises_when_redacted_dir_missing_after_bake(tmp_path: Path) -> None:
    """If bake reports success but `redacted/` is empty, that's a real
    error — the adapter must surface it."""
    case_path = tmp_path / "700100"
    (case_path / "working").mkdir(parents=True)
    # NOTE: no redacted/ dir created — bake's "success" is a lie

    with pytest.raises(DSARPipelineError, match="redacted/ missing"):
        adapter.run_for_case(_make_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))


# ─── issue #18: synthetic-flag auto-resolve ────────────────────────


def _make_synth_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
        synthetic=True,
    )


def _seed_tags(case_path: Path, ref: str, entities: list[dict]) -> Path:
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    tags_path = working / f"{ref}_tags.json"
    tags_path.write_text(json.dumps({"ref": ref, "entities": entities}))
    return tags_path


def test_synthetic_case_auto_resolves_flag_entries(tmp_path: Path) -> None:
    """Issue #18: synthetic cases have no operator to resolve flags.
    The bake adapter rewrites redact:'flag' to redact:false before
    invoking dsar-bake (which delegates to legacy redact_all that
    refuses to ship while flags remain unresolved)."""
    case_path = tmp_path / "900001"
    tags = _seed_tags(
        case_path,
        "D001",
        [
            {"text": "a@x", "type": "email", "redact": "flag"},
            {"text": "b@x", "type": "email", "redact": False},
            {"text": "c@x", "type": "email", "redact": True},
        ],
    )
    (case_path / "working" / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    adapter.run_for_case(_make_synth_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))

    rewritten = json.loads(tags.read_text())
    redacts = [e["redact"] for e in rewritten["entities"]]
    assert redacts == [False, False, True]  # flag → False; others untouched


def test_non_synthetic_case_halts_on_pending_flags(tmp_path: Path) -> None:
    """Real operator cases (cfg.synthetic=False, resolve_flags_as=None)
    halt with an actionable message before invoking bake when pending
    flags exist. Issue #26."""
    from dsar_orchestrator.exceptions import PipelineHalt

    case_path = tmp_path / "900002"
    tags = _seed_tags(
        case_path,
        "D001",
        [{"text": "a@x", "type": "email", "redact": "flag"}],
    )
    (case_path / "working" / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    with pytest.raises(PipelineHalt, match="pending detect-stage flags"):
        adapter.run_for_case(_make_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))

    # Flags NOT rewritten (halt was raised before any helper ran).
    unchanged = json.loads(tags.read_text())
    assert unchanged["entities"][0]["redact"] == "flag"


def test_synthetic_case_no_tags_files_is_noop(tmp_path: Path) -> None:
    """Synthetic case with no *_tags.json files (rare; pre-detect) —
    helper exits cleanly without touching anything."""
    case_path = tmp_path / "900003"
    (case_path / "working").mkdir(parents=True)
    (case_path / "working" / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    # Just needs to not raise.
    adapter.run_for_case(_make_synth_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))


def test_synthetic_case_clears_register_notes(tmp_path: Path) -> None:
    """Issue #18 round 2: legacy redact_all checks register.json's
    `notes` field for 'flagged for review' too, not just the per-entity
    redact field. Synthetic helper must clear both."""
    case_path = tmp_path / "900004"
    working = case_path / "working"
    working.mkdir(parents=True)
    # Per Contract A: register is a flat list.
    register = [
        {"ref": "D001", "filename": "D001.txt", "notes": "6 items flagged for review"},
        {"ref": "D002", "filename": "D002.txt", "notes": ""},
        {"ref": "D003", "filename": "D003.txt", "notes": "1 items flagged for review"},
    ]
    (working / "register.json").write_text(json.dumps(register))
    (working / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    adapter.run_for_case(_make_synth_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))

    rewritten = json.loads((working / "register.json").read_text())
    assert all(d["notes"] == "" for d in rewritten)


def test_non_synthetic_case_register_notes_trigger_halt(tmp_path: Path) -> None:
    """Real operator cases (cfg.synthetic=False, resolve_flags_as=None)
    halt when register.json::notes mentions 'flagged for review'.
    Issue #26."""
    from dsar_orchestrator.exceptions import PipelineHalt

    case_path = tmp_path / "900005"
    working = case_path / "working"
    working.mkdir(parents=True)
    register = [{"ref": "D001", "filename": "D001.txt", "notes": "6 items flagged for review"}]
    (working / "register.json").write_text(json.dumps(register))
    (working / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    with pytest.raises(PipelineHalt, match="pending detect-stage flags"):
        adapter.run_for_case(_make_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))

    # Notes NOT cleared by the halt path.
    unchanged = json.loads((working / "register.json").read_text())
    assert unchanged[0]["notes"] == "6 items flagged for review"


# ─── issue #26: operator opt-in via --resolve-flags-as ─────────────


def _make_cfg_with_resolve(case_path: Path, resolve_flags_as: str) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
        synthetic=False,
        resolve_flags_as=resolve_flags_as,
    )


def test_resolve_flags_as_false_resolves_to_false(tmp_path: Path) -> None:
    case_path = tmp_path / "900100"
    tags = _seed_tags(
        case_path,
        "D001",
        [
            {"text": "a@x", "type": "email", "redact": "flag"},
            {"text": "b@x", "type": "email", "redact": True},
        ],
    )
    (case_path / "working" / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    adapter.run_for_case(
        _make_cfg_with_resolve(case_path, "false"),
        runner=lambda *a, **k: _ok_completed(a[0]),
    )

    rewritten = json.loads(tags.read_text())
    redacts = [e["redact"] for e in rewritten["entities"]]
    assert redacts == [False, True]  # flag → False; True untouched


def test_resolve_flags_as_true_resolves_to_true(tmp_path: Path) -> None:
    case_path = tmp_path / "900101"
    tags = _seed_tags(
        case_path,
        "D001",
        [{"text": "a@x", "type": "email", "redact": "flag"}],
    )
    (case_path / "working" / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    adapter.run_for_case(
        _make_cfg_with_resolve(case_path, "true"),
        runner=lambda *a, **k: _ok_completed(a[0]),
    )

    rewritten = json.loads(tags.read_text())
    assert rewritten["entities"][0]["redact"] is True


def test_real_case_with_no_flags_does_not_halt(tmp_path: Path) -> None:
    """When operator has already resolved all flags (no flag entries +
    no 'flagged for review' notes), the pre-bake gate is a no-op."""
    case_path = tmp_path / "900102"
    _seed_tags(
        case_path,
        "D001",
        [{"text": "a@x", "type": "email", "redact": True}],
    )
    (case_path / "working" / "redaction_input.jsonl").write_text("")
    (case_path / "redacted").mkdir()
    (case_path / "redacted" / "doc.pdf").write_text("baked")

    # Should NOT raise — no flags to resolve.
    adapter.run_for_case(_make_cfg(case_path), runner=lambda *a, **k: _ok_completed(a[0]))


def test_count_pending_flags_counts_both_sources(tmp_path: Path) -> None:
    """The pre-bake gate counts entity-level flags AND register notes."""
    from dsar_orchestrator.adapters.bake import _count_pending_flags

    case_path = tmp_path / "900103"
    working = case_path / "working"
    working.mkdir(parents=True)
    _seed_tags(
        case_path,
        "D001",
        [
            {"text": "a", "redact": "flag"},
            {"text": "b", "redact": "flag"},
            {"text": "c", "redact": False},
        ],
    )
    register = [
        {"ref": "D001", "notes": "2 items flagged for review"},
        {"ref": "D002", "notes": ""},
        {"ref": "D003", "notes": "1 items flagged for review"},
    ]
    (working / "register.json").write_text(json.dumps(register))

    entity_count, notes_count = _count_pending_flags(case_path)
    assert entity_count == 2  # two redact:"flag" entries
    assert notes_count == 2  # two docs with "flagged for review"
