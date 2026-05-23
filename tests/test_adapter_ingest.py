"""Tests for the ingest adapter — `adapters.ingest`.

Adapter shells out to ``python -m dsar_pipeline.ingest``; tests
inject a fake runner so subprocess never fires.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import ingest as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


def _make_cfg(case_path: Path, *, subject_name: str | None = "James Carter") -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=(SubjectIdentifier(primary_name=subject_name) if subject_name else None),
    )


def _seed_case(tmp_path: Path) -> Path:
    case_path = tmp_path / "100100"
    src = case_path / "source"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("hello")
    (src / "b.txt").write_text("world")
    return case_path


def _fake_runner_writes_register(
    case_path: Path,
    *,
    upstream_hash: str | None = None,
    refs: list[str] | None = None,
):
    if refs is None:
        refs = ["a", "b"]

    def run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
        working = case_path / "working"
        working.mkdir(parents=True, exist_ok=True)
        register: dict = {
            "case_no": case_path.name,
            "refs": [{"ref": r} for r in refs],
        }
        if upstream_hash is not None:
            register["upstream_hash"] = upstream_hash
        (working / "register.json").write_text(json.dumps(register))
        return subprocess.CompletedProcess(args=argv, returncode=0)

    return run


# ─── happy path ────────────────────────────────────────────────────


def test_runner_called_with_module_and_subject(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    captured: dict = {}

    def capturing(argv, env, cwd):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        (case_path / "working").mkdir(parents=True, exist_ok=True)
        (case_path / "working" / "register.json").write_text("{}")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(_make_cfg(case_path), runner=capturing)
    assert captured["argv"][:3] == [sys.executable, "-m", "dsar_pipeline.ingest"]
    assert "James Carter" in captured["argv"]
    assert captured["cwd"] == case_path


def test_subject_omitted_when_missing(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    captured: dict = {}

    def capturing(argv, env, cwd):
        captured["argv"] = list(argv)
        (case_path / "working").mkdir(parents=True, exist_ok=True)
        (case_path / "working" / "register.json").write_text("{}")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(_make_cfg(case_path, subject_name=None), runner=capturing)
    assert captured["argv"] == [sys.executable, "-m", "dsar_pipeline.ingest"]


def test_passes_existing_upstream_hash_through(tmp_path: Path) -> None:
    """If the toolkit's ingest already stamped upstream_hash, don't
    overwrite it."""
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_fake_runner_writes_register(case_path, upstream_hash="toolkit-hash"),
    )
    reg = json.loads((case_path / "working" / "register.json").read_text())
    assert reg["upstream_hash"] == "toolkit-hash"


def test_stamps_upstream_hash_when_toolkit_omits_it(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_fake_runner_writes_register(case_path),  # no upstream_hash
    )
    reg = json.loads((case_path / "working" / "register.json").read_text())
    assert reg["upstream_hash"]
    assert len(reg["upstream_hash"]) == 64  # sha256 hex
    assert reg["producer_version"].startswith("dsar_orchestrator.adapters.ingest")


def test_stamped_hash_changes_when_source_changes(tmp_path: Path) -> None:
    """If the toolkit didn't stamp a hash, the adapter's hash must
    reflect the source tree — otherwise the cascade misses
    invalidation."""
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_writes_register(case_path))
    first = json.loads((case_path / "working" / "register.json").read_text())["upstream_hash"]

    (case_path / "source" / "a.txt").write_text("MUTATED")
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_writes_register(case_path))
    second = json.loads((case_path / "working" / "register.json").read_text())["upstream_hash"]
    assert first != second


# ─── error handling ────────────────────────────────────────────────


def test_raises_when_subprocess_fails(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def failing(argv, env, cwd):
        return subprocess.CompletedProcess(
            args=argv, returncode=2, stdout="", stderr="ERROR: ingest broken"
        )

    with pytest.raises(DSARPipelineError, match="ingest module exited 2"):
        adapter.run_for_case(_make_cfg(case_path), runner=failing)


def test_raises_when_register_not_produced(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def silent(argv, env, cwd):
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="register.json"):
        adapter.run_for_case(_make_cfg(case_path), runner=silent)


def test_raises_on_invalid_register_json(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def garbage_runner(argv, env, cwd):
        working = case_path / "working"
        working.mkdir(parents=True, exist_ok=True)
        (working / "register.json").write_text("{not valid json}")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="not valid JSON"):
        adapter.run_for_case(_make_cfg(case_path), runner=garbage_runner)


# ─── atomic write ──────────────────────────────────────────────────


def test_no_temp_file_leftover(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_writes_register(case_path))
    working = case_path / "working"
    assert not any(p.suffix == ".tmp" for p in working.iterdir())
