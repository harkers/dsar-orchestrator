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
