"""Tests for the scope-classify adapter — `adapters.scope_classify`.

Adapter shells out to `dsar-scope-check`; tests inject a fake runner
so subprocess never actually fires.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import scope_classify as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


def _make_cfg(case_path: Path, *, rerank_mode: str = "shadow") -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
        rerank_mode=rerank_mode,
    )


def _seed_case(tmp_path: Path, *, with_rerank: bool = False) -> Path:
    case_path = tmp_path / "300900"
    working = case_path / "working"
    working.mkdir(parents=True)
    # Shadow-mode upstream is cosine_prefilter.jsonl
    (working / "cosine_prefilter.jsonl").write_text(
        '{"ref":"d1","cosine_score":0.5,"passes":true,"upstream_hash":"u"}\n'
    )
    if with_rerank:
        (working / "scope_rerank.jsonl").write_text(
            '{"ref":"d1","rerank_score":0.1,"would_drop":false,'
            '"mode":"enforce","upstream_hash":"u"}\n'
        )
    return case_path


def _fake_runner_success(case_path: Path, verdicts: list[dict] | None = None):
    """Build a runner that writes scope_verdicts.jsonl and returns
    rc=0. verdicts defaults to one present row."""
    if verdicts is None:
        verdicts = [{"ref": "d1", "scope_verdict": "present"}]

    def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        (case_path / "working" / "scope_verdicts.jsonl").write_text(
            "\n".join(json.dumps(v) for v in verdicts) + "\n"
        )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    return run


# ─── happy path ────────────────────────────────────────────────────


def test_writes_completion_anchor(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))
    assert (case_path / "working" / "scope_classify_complete.jsonl").exists()


def test_completion_row_has_required_fields(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))

    row = json.loads(
        (case_path / "working" / "scope_classify_complete.jsonl").read_text().splitlines()[0]
    )
    assert row["completed"] is True
    assert "upstream_hash" in row
    assert row["schema_version"] == "1.0"
    assert row["producer_version"].startswith("dsar_orchestrator.adapters.scope_classify")
    assert "summary" in row


def test_upstream_hash_matches_cosine_prefilter_in_shadow(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))

    from dsar_orchestrator.hash_chain import sha256_file

    expected = sha256_file(case_path / "working" / "cosine_prefilter.jsonl")
    row = json.loads(
        (case_path / "working" / "scope_classify_complete.jsonl").read_text().splitlines()[0]
    )
    assert row["upstream_hash"] == expected


def test_upstream_hash_matches_scope_rerank_in_enforce(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, with_rerank=True)
    adapter.run_for_case(
        _make_cfg(case_path, rerank_mode="enforce"),
        runner=_fake_runner_success(case_path),
    )

    from dsar_orchestrator.hash_chain import sha256_file

    expected = sha256_file(case_path / "working" / "scope_rerank.jsonl")
    row = json.loads(
        (case_path / "working" / "scope_classify_complete.jsonl").read_text().splitlines()[0]
    )
    assert row["upstream_hash"] == expected


def test_summary_counts_verdicts(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    runner = _fake_runner_success(
        case_path,
        verdicts=[
            {"ref": "a", "scope_verdict": "present"},
            {"ref": "b", "scope_verdict": "present"},
            {"ref": "c", "scope_verdict": "not_present"},
            {"ref": "d", "scope_verdict": "ambiguous"},
        ],
    )
    adapter.run_for_case(_make_cfg(case_path), runner=runner)

    row = json.loads(
        (case_path / "working" / "scope_classify_complete.jsonl").read_text().splitlines()[0]
    )
    assert row["summary"] == {"present": 2, "not_present": 1, "ambiguous": 1}


def test_runner_receives_case_and_env(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    captured: dict = {}

    def capturing_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        (case_path / "working" / "scope_verdicts.jsonl").write_text(
            '{"ref":"d1","scope_verdict":"present"}\n'
        )
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(_make_cfg(case_path), runner=capturing_runner)

    assert captured["argv"][0] == "dsar-scope-check"
    assert "--case" in captured["argv"]
    assert case_path.name in captured["argv"]
    assert captured["env"]["DSAR_CASE_ROOT"] == str(case_path.parent)


# ─── error handling ────────────────────────────────────────────────


def test_raises_when_upstream_missing(tmp_path: Path) -> None:
    case_path = tmp_path / "300900"
    (case_path / "working").mkdir(parents=True)
    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="upstream missing"):
        adapter.run_for_case(cfg, runner=lambda argv, env: subprocess.CompletedProcess(argv, 0))


def test_raises_when_subprocess_fails(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def failing(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=argv, returncode=2, stdout="", stderr="ERROR: missing case_context.json"
        )

    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="exited 2"):
        adapter.run_for_case(cfg, runner=failing)


def test_raises_when_verdicts_not_produced(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def silent(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        # rc=0 but no scope_verdicts.jsonl written
        return subprocess.CompletedProcess(args=argv, returncode=0)

    cfg = _make_cfg(case_path)
    with pytest.raises(DSARPipelineError, match="scope_verdicts.jsonl was not produced"):
        adapter.run_for_case(cfg, runner=silent)


def test_raises_on_malformed_verdict_row(tmp_path: Path) -> None:
    """A corrupt row in scope_verdicts.jsonl indicates a real toolkit
    bug; the adapter must fail loud rather than bucket the parse
    failure into a green anchor."""
    case_path = _seed_case(tmp_path)

    def garbage_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        (case_path / "working" / "scope_verdicts.jsonl").write_text(
            '{"ref":"a","scope_verdict":"present"}\nnot valid json\n'
        )
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="malformed verdict row"):
        adapter.run_for_case(_make_cfg(case_path), runner=garbage_runner)


# ─── atomic write ──────────────────────────────────────────────────


def test_no_temp_file_leftover(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))
    working = case_path / "working"
    assert not any(p.suffix == ".tmp" for p in working.iterdir())
