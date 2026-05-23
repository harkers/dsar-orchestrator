"""Tests for the redact adapter — `adapters.redact`.

Adapter shells out to ``dsar-redact``; tests inject a fake runner so
subprocess never actually fires.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dsar_orchestrator.adapters import redact as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


def _make_cfg(case_path: Path, *, pii_mode: str = "shadow") -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
        pii_classify_mode=pii_mode,
    )


def _seed_case(tmp_path: Path) -> Path:
    case_path = tmp_path / "600100"
    working = case_path / "working"
    working.mkdir(parents=True)
    (working / "d1_tags.json").write_text(json.dumps({"ref": "d1", "in_scope": True}))
    (working / "pii_collection.jsonl").write_text(
        '{"ref":"d1","entities":[],"upstream_hash":"u"}\n'
    )
    return case_path


def _fake_runner_success(
    case_path: Path,
    rows: list[dict] | None = None,
):
    if rows is None:
        rows = [
            {"ref": "d1", "spans": [], "reason_code": "pii"},
            {"ref": "d2", "spans": [], "reason_code": "exemption"},
        ]

    def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        (case_path / "working" / "redaction_input.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")
        )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    return run


# ─── happy path ────────────────────────────────────────────────────


def test_writes_completion_anchor(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))
    assert (case_path / "working" / "redact_complete.json").exists()


def test_completion_anchor_has_required_fields(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))

    obj = json.loads((case_path / "working" / "redact_complete.json").read_text())
    assert obj["completed"] is True
    assert "upstream_hash" in obj
    assert obj["schema_version"] == "1.0"
    assert obj["producer_version"].startswith("dsar_orchestrator.adapters.redact")
    assert "summary" in obj


def test_summary_counts_redactions_by_reason(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    runner = _fake_runner_success(
        case_path,
        rows=[
            {"ref": "a", "spans": [], "reason_code": "pii"},
            {"ref": "b", "spans": [], "reason_code": "pii"},
            {"ref": "c", "spans": [], "reason_code": "exemption"},
        ],
    )
    adapter.run_for_case(_make_cfg(case_path), runner=runner)
    obj = json.loads((case_path / "working" / "redact_complete.json").read_text())
    assert obj["summary"]["total_redactions"] == 3
    assert obj["summary"]["by_reason"] == {"pii": 2, "exemption": 1}


def test_upstream_hash_depends_on_pii_collection(tmp_path: Path) -> None:
    """Mutating pii_collection.jsonl must change the upstream_hash so
    the cascade re-runs downstream."""
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))
    first = json.loads((case_path / "working" / "redact_complete.json").read_text())[
        "upstream_hash"
    ]

    (case_path / "working" / "pii_collection.jsonl").write_text(
        '{"ref":"d1","entities":[{"surface":"X"}],"upstream_hash":"u2"}\n'
    )
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))
    second = json.loads((case_path / "working" / "redact_complete.json").read_text())[
        "upstream_hash"
    ]
    assert first != second


def test_upstream_hash_depends_on_enforce_flag(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path, pii_mode="shadow"),
        runner=_fake_runner_success(case_path),
    )
    shadow_hash = json.loads((case_path / "working" / "redact_complete.json").read_text())[
        "upstream_hash"
    ]

    adapter.run_for_case(
        _make_cfg(case_path, pii_mode="enforce"),
        runner=_fake_runner_success(case_path),
    )
    enforce_hash = json.loads((case_path / "working" / "redact_complete.json").read_text())[
        "upstream_hash"
    ]
    assert shadow_hash != enforce_hash


def test_runner_receives_case_and_env(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    captured: dict = {}

    def capturing_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        (case_path / "working" / "redaction_input.jsonl").write_text(
            '{"ref":"d1","spans":[],"reason_code":"pii"}\n'
        )
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(_make_cfg(case_path), runner=capturing_runner)
    assert captured["argv"][0] == "dsar-redact"
    assert "--case" in captured["argv"]
    assert case_path.name in captured["argv"]
    assert captured["env"]["DSAR_CASE_ROOT"] == str(case_path.parent)


def test_empty_redaction_input_is_valid(tmp_path: Path) -> None:
    """A case with no PII found anywhere → empty redaction_input.jsonl
    is still a valid run."""
    case_path = _seed_case(tmp_path)

    def empty_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        (case_path / "working" / "redaction_input.jsonl").write_text("")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    adapter.run_for_case(_make_cfg(case_path), runner=empty_runner)
    obj = json.loads((case_path / "working" / "redact_complete.json").read_text())
    assert obj["summary"]["total_redactions"] == 0


# ─── error handling ────────────────────────────────────────────────


def test_raises_when_subprocess_fails(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def failing(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=argv, returncode=2, stdout="", stderr="ERROR: redact aborted"
        )

    with pytest.raises(DSARPipelineError, match="exited 2"):
        adapter.run_for_case(_make_cfg(case_path), runner=failing)


def test_raises_when_redaction_input_not_produced(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def silent(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="redaction_input.jsonl"):
        adapter.run_for_case(_make_cfg(case_path), runner=silent)


def test_raises_on_malformed_row(tmp_path: Path) -> None:
    """A corrupt row in redaction_input.jsonl indicates a real
    toolkit bug; the adapter must fail loud rather than bucket the
    parse failure into a green anchor."""
    case_path = _seed_case(tmp_path)

    def garbage_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        (case_path / "working" / "redaction_input.jsonl").write_text(
            '{"ref":"a","spans":[],"reason_code":"pii"}\nnot valid json\n'
        )
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with pytest.raises(DSARPipelineError, match="malformed row"):
        adapter.run_for_case(_make_cfg(case_path), runner=garbage_runner)


# ─── atomic write ──────────────────────────────────────────────────


def test_no_temp_file_leftover(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(_make_cfg(case_path), runner=_fake_runner_success(case_path))
    working = case_path / "working"
    assert not any(p.suffix == ".tmp" for p in working.iterdir())
