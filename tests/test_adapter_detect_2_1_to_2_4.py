"""Tests for the detect adapter — `adapters.detect_2_1_to_2_4`.

Adapter shells out to ``python -m dsar_pipeline.detect`` then
aggregates the toolkit's per-ref ``<ref>_tags.json`` files into
``working/detect_entities.jsonl``. Tests inject a fake runner so
subprocess never fires.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import detect_2_1_to_2_4 as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="James Carter"),
    )


def _seed_case(tmp_path: Path, *, refs: list[str] | None = None) -> Path:
    if refs is None:
        refs = ["d1", "d2"]
    case_path = tmp_path / "200100"
    working = case_path / "working"
    working.mkdir(parents=True)
    src = case_path / "source"
    src.mkdir(parents=True)
    register_refs: list[dict] = []
    for r in refs:
        (src / f"{r}.txt").write_text(f"doc {r}")
        register_refs.append({"ref": r, "text_path": f"source/{r}.txt"})
    register = {
        "case_no": case_path.name,
        "refs": register_refs,
        "upstream_hash": "fake-register-hash",
    }
    (working / "register.json").write_text(json.dumps(register))
    return case_path


def _fake_runner_writes_tags(case_path: Path, *, refs: list[str]):
    def run(argv, env, cwd) -> subprocess.CompletedProcess:
        working = case_path / "working"
        for r in refs:
            (working / f"{r}_tags.json").write_text(
                json.dumps({"ref": r, "entities": [], "in_scope": True})
            )
        return subprocess.CompletedProcess(args=argv, returncode=0)

    return run


# ─── happy path ────────────────────────────────────────────────────


def test_runner_called_with_module_and_subject(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, refs=["d1"])
    captured: dict = {}

    def capturing(argv, env, cwd):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        (case_path / "working" / "d1_tags.json").write_text("{}")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(_make_cfg(case_path), runner=capturing)
    assert captured["argv"][:3] == [sys.executable, "-m", "dsar_pipeline.detect"]
    assert "James Carter" in captured["argv"]
    assert captured["cwd"] == case_path


def test_aggregates_one_row_per_tag_file(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, refs=["d1", "d2", "d3"])
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_fake_runner_writes_tags(case_path, refs=["d1", "d2", "d3"]),
    )
    rows = [
        json.loads(line)
        for line in (case_path / "working" / "detect_entities.jsonl").read_text().splitlines()
        if line
    ]
    assert [r["ref"] for r in rows] == ["d1", "d2", "d3"]


def test_row_carries_tag_payload_and_provenance(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, refs=["d1"])
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_fake_runner_writes_tags(case_path, refs=["d1"]),
    )
    row = json.loads((case_path / "working" / "detect_entities.jsonl").read_text().splitlines()[0])
    assert row["ref"] == "d1"
    assert row["tags"]["in_scope"] is True
    assert row["schema_version"] == "1.0"
    assert row["producer_version"].startswith("dsar_orchestrator.adapters.detect_2_1_to_2_4")
    assert "upstream_hash" in row


def test_upstream_hash_matches_register(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, refs=["d1"])
    # Seed the per-ref text file that compute_register_hash needs.
    register = json.loads((case_path / "working" / "register.json").read_text())
    src = case_path / "source"
    src.mkdir(exist_ok=True)
    (src / "d1.txt").write_text("doc one")
    register["refs"] = [{"ref": "d1", "text_path": "source/d1.txt"}]
    (case_path / "working" / "register.json").write_text(json.dumps(register))
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_fake_runner_writes_tags(case_path, refs=["d1"]),
    )

    from dsar_orchestrator.hash_chain import compute_register_hash

    expected = compute_register_hash(case_path / "working" / "register.json")
    row = json.loads((case_path / "working" / "detect_entities.jsonl").read_text().splitlines()[0])
    assert row["upstream_hash"] == expected


def test_raises_on_malformed_tag_file(tmp_path: Path) -> None:
    """A garbage <ref>_tags.json indicates a real toolkit bug; the
    adapter must fail loud rather than silently emit a sentinel row
    that downstream agents would accept as valid."""
    case_path = _seed_case(tmp_path, refs=["d1"])

    def runner(argv, env, cwd):
        (case_path / "working" / "d1_tags.json").write_text("{not valid json}")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="malformed tag file"):
        adapter.run_for_case(_make_cfg(case_path), runner=runner)


def test_subject_omitted_when_missing(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, refs=["d1"])
    cfg = CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=None,
    )
    captured: dict = {}

    def capturing(argv, env, cwd):
        captured["argv"] = list(argv)
        (case_path / "working" / "d1_tags.json").write_text("{}")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(cfg, runner=capturing)
    assert captured["argv"] == [sys.executable, "-m", "dsar_pipeline.detect"]


# ─── error handling ────────────────────────────────────────────────


def test_raises_when_subprocess_fails(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def failing(argv, env, cwd):
        return subprocess.CompletedProcess(
            args=argv, returncode=2, stdout="", stderr="ERROR: detect broken"
        )

    with pytest.raises(DSARPipelineError, match="detect module exited 2"):
        adapter.run_for_case(_make_cfg(case_path), runner=failing)


def test_raises_when_no_tag_files_produced(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def silent(argv, env, cwd):
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="no <ref>_tags.json files"):
        adapter.run_for_case(_make_cfg(case_path), runner=silent)


# ─── atomic write ──────────────────────────────────────────────────


def test_no_temp_file_leftover(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, refs=["d1"])
    adapter.run_for_case(
        _make_cfg(case_path),
        runner=_fake_runner_writes_tags(case_path, refs=["d1"]),
    )
    working = case_path / "working"
    assert not any(p.suffix == ".tmp" for p in working.iterdir())
