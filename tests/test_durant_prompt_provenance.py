"""Provenance stamping for the sealed-prompt durant migration.

durant_pass/durant_recheck now load their system prompts from the toolkit's
sealed registry (durant.system / durant.recheck.system v1.1.0) and stamp the
seal fields on every verdict row. These tests pin that the fields land on both
the happy and error paths AND — the ultimate oracle — that a stamped row passes
the real `verify --check prompt-versions` gate against the live registry.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.needs_toolkit

from dsar_orchestrator.local_broker import durant_pass, durant_recheck  # noqa: E402
from dsar_orchestrator.verify import verify_prompt_versions  # noqa: E402

_PROV = {
    "prompt_id",
    "prompt_version",
    "prompt_canonical_seal_sha256",
    "prompt_effective_sha256",
    "prompt_applied_strips",
}


def _fake_post(content: str):
    return lambda *a, **k: {"choices": [{"message": {"content": content}}]}


def test_classify_one_stamps_provenance(monkeypatch):
    monkeypatch.setattr(
        durant_pass,
        "_post",
        _fake_post('{"durant_verdict":"biographical","rationale":"x"}'),
    )
    row = durant_pass.classify_one(case_id="c", doc_ref="r", text="t", subject_summary="s")
    assert set(row) >= _PROV
    assert row["prompt_id"] == "durant.system"
    assert row["prompt_version"] == "1.1.0"
    assert len(row["prompt_canonical_seal_sha256"]) == 64
    assert isinstance(row["prompt_applied_strips"], list)


def test_classify_one_error_path_keeps_provenance(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(durant_pass, "_post", boom)
    row = durant_pass.classify_one(case_id="c", doc_ref="r", text="t", subject_summary="s")
    # Provenance must be present even when the model call fails.
    assert set(row) >= _PROV
    assert row["error_state"] == "model_unreachable"


def test_recheck_one_stamps_provenance(monkeypatch):
    monkeypatch.setattr(
        durant_recheck,
        "_post",
        _fake_post('{"recheck_verdict":"confirmed_work_context_only","rationale":"x"}'),
    )
    row = durant_recheck.recheck_one(
        case_id="c",
        doc_ref="r",
        text="t",
        subject_summary="s",
        original_rationale="o",
    )
    assert set(row) >= _PROV
    assert row["prompt_id"] == "durant.recheck.system"
    assert row["prompt_version"] == "1.1.0"


def test_recheck_one_error_path_keeps_provenance(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(durant_recheck, "_post", boom)
    row = durant_recheck.recheck_one(
        case_id="c",
        doc_ref="r",
        text="t",
        subject_summary="s",
        original_rationale="o",
    )
    # Provenance present on the recheck error path too (stamped via **base).
    assert set(row) >= _PROV
    assert row["prompt_id"] == "durant.recheck.system"
    assert row.get("error_state") == "model_unreachable"


def test_stamped_rows_pass_verify_prompt_versions(tmp_path, monkeypatch):
    """End-to-end oracle: rows stamped by classify_one/recheck_one must pass
    verify_prompt_versions against the live sealed registry (exit 0). This
    proves the seal + effective_sha256 + applied_strips replay correctly —
    i.e. the body is byte-consistent with the archived sealed prompt."""
    monkeypatch.setattr(
        durant_pass,
        "_post",
        _fake_post('{"durant_verdict":"biographical","rationale":"x"}'),
    )
    monkeypatch.setattr(
        durant_recheck,
        "_post",
        _fake_post('{"recheck_verdict":"confirmed_work_context_only","rationale":"x"}'),
    )
    pass_row = durant_pass.classify_one(case_id="c", doc_ref="r1", text="t", subject_summary="s")
    recheck_row = durant_recheck.recheck_one(
        case_id="c",
        doc_ref="r1",
        text="t",
        subject_summary="s",
        original_rationale="o",
    )
    working = tmp_path / "working"
    working.mkdir()
    (working / "durant_verdicts.jsonl").write_text(json.dumps(pass_row) + "\n")
    (working / "durant_underdisclosure_recheck.jsonl").write_text(json.dumps(recheck_row) + "\n")

    result = verify_prompt_versions(tmp_path)
    assert result.exit_code == 0, result.errors
    assert result.ok
