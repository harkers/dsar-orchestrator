"""Tests for the people_register adapter — `adapters.people_register`.

Adapter calls the toolkit's ``build_people_register(working_dir)``
and writes the conductor's ``working/person_index.json`` with the
expected cascade fields. Builder is injected so tests are hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dsar_orchestrator.adapters import people_register as adapter
from dsar_orchestrator.config import CaseConfig, SubjectIdentifier
from dsar_orchestrator.exceptions import DSARPipelineError


def _make_cfg(case_path: Path) -> CaseConfig:
    return CaseConfig(
        case_no=case_path.name,
        case_path=case_path,
        case_scope="t",
        subject_identifier=SubjectIdentifier(primary_name="t"),
    )


def _seed_case(tmp_path: Path, *, with_embeddings: bool = True) -> Path:
    case_path = tmp_path / "300100"
    working = case_path / "working"
    working.mkdir(parents=True)
    if with_embeddings:
        (working / "embeddings.jsonl").write_text('{"ref":"d1","vector":[0.1]}\n')
    return case_path


def _builder_returning(result: dict[str, Any]):
    def run(working: Path) -> dict[str, Any]:
        return result

    return run


# ─── happy path ────────────────────────────────────────────────────


def test_writes_person_index(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        builder_fn=_builder_returning(
            {"clusters": [{"id": 1, "canonical": "James"}], "alias_to_id": {"James": 1}}
        ),
    )
    assert (case_path / "working" / "person_index.json").exists()


def test_person_index_has_required_fields(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        builder_fn=_builder_returning(
            {
                "clusters": [{"id": 1}],
                "alias_to_id": {"x": 1},
                "threshold": 0.75,
                "model": "test-model",
                "generated_at": "2026-05-22T00:00:00Z",
            }
        ),
    )
    obj = json.loads((case_path / "working" / "person_index.json").read_text())
    assert obj["clusters"] == [{"id": 1}]
    assert obj["alias_to_id"] == {"x": 1}
    assert obj["threshold"] == 0.75
    assert obj["model"] == "test-model"
    assert obj["generated_at"] == "2026-05-22T00:00:00Z"
    assert "upstream_hash" in obj
    assert obj["schema_version"] == "1.0"
    assert obj["producer_version"].startswith("dsar_orchestrator.adapters.people_register")


def test_upstream_hash_matches_embeddings_file(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        builder_fn=_builder_returning({"clusters": [], "alias_to_id": {}}),
    )

    from dsar_orchestrator.hash_chain import sha256_file

    expected = sha256_file(case_path / "working" / "embeddings.jsonl")
    obj = json.loads((case_path / "working" / "person_index.json").read_text())
    assert obj["upstream_hash"] == expected


def test_upstream_hash_empty_when_no_embeddings(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path, with_embeddings=False)
    adapter.run_for_case(
        _make_cfg(case_path),
        builder_fn=_builder_returning({"clusters": [], "alias_to_id": {}}),
    )
    obj = json.loads((case_path / "working" / "person_index.json").read_text())
    assert obj["upstream_hash"] == ""


def test_handles_builder_returning_minimal_dict(tmp_path: Path) -> None:
    """Builder might return only `clusters` — adapter must default the
    other fields rather than KeyError."""
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        builder_fn=_builder_returning({"clusters": []}),
    )
    obj = json.loads((case_path / "working" / "person_index.json").read_text())
    assert obj["clusters"] == []
    assert obj["alias_to_id"] == {}


def test_builder_receives_working_dir(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    captured: dict = {}

    def capturing(working: Path) -> dict[str, Any]:
        captured["working"] = working
        return {"clusters": []}

    adapter.run_for_case(_make_cfg(case_path), builder_fn=capturing)
    assert captured["working"] == case_path / "working"


# ─── error handling ────────────────────────────────────────────────


def test_wraps_builder_exceptions(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)

    def bad_builder(working: Path) -> dict[str, Any]:
        raise RuntimeError("clustering failed: OOM")

    with pytest.raises(DSARPipelineError, match="people_register builder failed"):
        adapter.run_for_case(_make_cfg(case_path), builder_fn=bad_builder)


# ─── atomic write ──────────────────────────────────────────────────


def test_no_temp_file_leftover(tmp_path: Path) -> None:
    case_path = _seed_case(tmp_path)
    adapter.run_for_case(
        _make_cfg(case_path),
        builder_fn=_builder_returning({"clusters": []}),
    )
    working = case_path / "working"
    assert not any(p.suffix == ".tmp" for p in working.iterdir())
