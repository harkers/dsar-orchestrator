"""Tests for the export adapter — `adapters.export`.

Adapter shells out to ``dsar-bake`` then ``python -m
dsar_pipeline.export``; tests inject a fake runner so subprocess
never fires.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import export as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
    )


def _seed_case(tmp_path: Path) -> Path:
    case_path = tmp_path / "800100"
    (case_path / "working").mkdir(parents=True)
    (case_path / "redacted").mkdir(parents=True)
    return case_path


def _two_stage_runner(
    case_path: Path,
    *,
    redacted_files: list[str] | None = None,
    output_files: list[str] | None = None,
):
    """Fake runner that handles both `dsar-bake` and the export module.

    - bake writes ``redacted/<ref>.txt`` files (mimics PDF redaction)
    - export writes ``output/<ref>.pdf`` files (mimics packaging)
    """
    if redacted_files is None:
        redacted_files = ["d1.txt", "d2.txt"]
    if output_files is None:
        output_files = ["d1.pdf", "d2.pdf"]

    def run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
        if argv[0] == "dsar-bake":
            redacted = case_path / "redacted"
            redacted.mkdir(parents=True, exist_ok=True)
            for name in redacted_files:
                (redacted / name).write_text("[REDACTED]\n")
        else:
            output = case_path / "output"
            output.mkdir(parents=True, exist_ok=True)
            for name in output_files:
                (output / name).write_text("(packaged)\n")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    return run


# ─── happy path ────────────────────────────────────────────────────


def test_writes_output_manifest(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_two_stage_runner(case_path))
    assert (case_path / "output" / "manifest.json").exists()


def test_manifest_has_required_fields(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_two_stage_runner(case_path))

    obj = json.loads((case_path / "output" / "manifest.json").read_text())
    assert obj["completed"] is True
    assert "upstream_hash" in obj
    assert obj["schema_version"] == "1.0"
    assert obj["producer_version"].startswith("dsar_orchestrator.adapters.export")
    assert "summary" in obj


def test_summary_counts_files_by_extension(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_two_stage_runner(
            case_path,
            output_files=["a.pdf", "b.pdf", "c.txt", "manifest.md"],
        ),
    )
    obj = json.loads((case_path / "output" / "manifest.json").read_text())
    # manifest.json itself ends up in output too, so totals reflect that.
    assert obj["summary"]["total_files"] >= 4
    by_ext = obj["summary"]["by_extension"]
    assert by_ext.get(".pdf") == 2
    assert by_ext.get(".txt") == 1
    assert by_ext.get(".md") == 1


def test_upstream_hash_depends_on_redacted_tree(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_two_stage_runner(case_path, redacted_files=["d1.txt"]),
    )
    first = json.loads((case_path / "output" / "manifest.json").read_text())["upstream_hash"]

    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_two_stage_runner(case_path, redacted_files=["d1.txt", "d2.txt"]),
    )
    second = json.loads((case_path / "output" / "manifest.json").read_text())["upstream_hash"]
    assert first != second


def test_runner_called_for_bake_then_export(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    calls: list[list[str]] = []

    def capturing(argv, env, cwd):
        calls.append(list(argv))
        if argv[0] == "dsar-bake":
            (case_path / "redacted" / "d1.txt").write_text("x")
        else:
            (case_path / "output").mkdir(parents=True, exist_ok=True)
            (case_path / "output" / "d1.pdf").write_text("p")
        assert cwd == case_path
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(_make_cfg(case_path), runner=capturing)
    assert calls[0][0] == "dsar-bake"
    assert calls[0][1] == "--case"
    assert calls[0][2] == case_path.name
    assert calls[1] == [sys.executable, "-m", "dsar_pipeline.export"]


# ─── error handling ────────────────────────────────────────────────


def test_raises_when_bake_fails(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def failing_bake(argv, env, cwd):
        return subprocess.CompletedProcess(
            args=argv, returncode=2, stdout="", stderr="ERROR: bake aborted"
        )

    with pytest.raises(DSARPipelineError, match="bake CLI exited 2"):
        adapter.run_for_case(_make_cfg(case_path), runner=failing_bake)


def test_raises_when_export_fails(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def runner(argv, env, cwd):
        if argv[0] == "dsar-bake":
            (case_path / "redacted" / "d1.txt").write_text("x")
            return subprocess.CompletedProcess(args=argv, returncode=0)
        return subprocess.CompletedProcess(
            args=argv, returncode=3, stdout="", stderr="ERROR: export broken"
        )

    with pytest.raises(DSARPipelineError, match="export module exited 3"):
        adapter.run_for_case(_make_cfg(case_path), runner=runner)


def test_raises_when_output_dir_missing(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def runner(argv, env, cwd):
        # bake + export both return 0 but no output/ dir gets written
        if argv[0] == "dsar-bake":
            (case_path / "redacted" / "d1.txt").write_text("x")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="output/ directory missing"):
        adapter.run_for_case(_make_cfg(case_path), runner=runner)


# ─── atomic write ──────────────────────────────────────────────────


def test_no_temp_file_leftover(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_two_stage_runner(case_path))
    output = case_path / "output"
    assert not any(p.suffix == ".tmp" for p in output.iterdir())
