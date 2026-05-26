"""Tests for the dsar-durant-recheck CLI promotion (#111 sub-3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


@pytest.fixture
def case_root(tmp_path: Path) -> Path:
    (tmp_path / "working").mkdir()
    (tmp_path / "audit").mkdir()
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "Jane Doe", "email": "jane@example.com"})
    )
    return tmp_path


def _seed_inputs(
    case_root: Path,
    *,
    excluded: list[tuple[str, str]],
    extra: list[tuple[str, str]] | None = None,
) -> None:
    """Write register + durant_verdicts.jsonl + per-doc text files."""
    register = []
    rows = []
    for ref, body in excluded:
        text_file = case_root / "working" / f"{ref}.txt"
        text_file.write_text(body, encoding="utf-8")
        register.append({"ref": ref, "path": f"/src/{ref}.eml", "text_file": str(text_file)})
        rows.append({"doc_ref": ref, "durant_verdict": "work_context_only", "rationale": "orig"})
    for ref, body in extra or []:
        text_file = case_root / "working" / f"{ref}.txt"
        text_file.write_text(body, encoding="utf-8")
        register.append({"ref": ref, "path": f"/src/{ref}.eml", "text_file": str(text_file)})
        rows.append({"doc_ref": ref, "durant_verdict": "biographical", "rationale": "orig"})
    (case_root / "working" / "register.json").write_text(json.dumps(register))
    with (case_root / "working" / "durant_verdicts.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# --- module ---


def test_module_importable_and_constants() -> None:
    import dsar_orchestrator.local_broker.durant_recheck as mod

    assert hasattr(mod, "run")
    assert hasattr(mod, "main")
    assert hasattr(mod, "recheck_one")
    assert mod.ALLOWED_VERDICTS == (
        "confirmed_work_context_only",
        "reclassify_to_biographical",
        "reclassify_to_ambiguous",
    )


def test_case_root_resolution(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker.durant_recheck import _resolve_case_root

    assert _resolve_case_root(tmp_path) == tmp_path
    monkeypatch.setenv("DSAR_CASE_ROOT", str(tmp_path))
    assert _resolve_case_root(None) == tmp_path
    monkeypatch.delenv("DSAR_CASE_ROOT")
    monkeypatch.chdir(tmp_path)
    assert _resolve_case_root(None) == tmp_path


# --- recheck_one ---


def test_recheck_one_happy_path(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_recheck

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "recheck_verdict": "reclassify_to_biographical",
                                "rationale": "Subject is the active actor.",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_recheck, "_post", fake_post)
    result = durant_recheck.recheck_one(
        case_id="CASE-100",
        doc_ref="doc-001",
        text="email body",
        subject_summary="name='Jane'",
        original_rationale="missed in orig pass",
    )
    assert result["recheck_verdict"] == "reclassify_to_biographical"
    assert result["original_verdict"] == "work_context_only"
    assert result["original_rationale"] == "missed in orig pass"
    assert "error_state" not in result


def test_recheck_one_unknown_verdict_defaults_to_ambiguous_not_confirm(
    monkeypatch,
) -> None:
    """Under-disclosure safety: 'I'm not sure' MUST escalate, never silently
    confirm the original exclusion."""
    from dsar_orchestrator.local_broker import durant_recheck

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"recheck_verdict": "definitely_yes", "rationale": "x"}
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_recheck, "_post", fake_post)
    result = durant_recheck.recheck_one(
        case_id="CASE-100",
        doc_ref="doc-001",
        text="body",
        subject_summary="x",
        original_rationale="orig",
    )
    assert result["recheck_verdict"] == "reclassify_to_ambiguous"


def test_recheck_one_does_not_include_original_rationale_in_prompt(
    monkeypatch,
) -> None:
    """Confirmation-bias guard: original_rationale must stay out of the
    user prompt sent to the model."""
    from dsar_orchestrator.local_broker import durant_recheck

    captured_user: dict[str, str] = {}

    def fake_post(_system: str, user: str, **_kwargs) -> dict:
        captured_user["user"] = user
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"recheck_verdict": "confirmed_work_context_only", "rationale": "ok"}
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_recheck, "_post", fake_post)
    secret = "DURANT_SECRET_RATIONALE_TOKEN"
    durant_recheck.recheck_one(
        case_id="CASE-100",
        doc_ref="doc-001",
        text="body",
        subject_summary="x",
        original_rationale=secret,
    )
    assert secret not in captured_user["user"], (
        "confirmation-bias guard broke: original_rationale leaked into prompt"
    )


def test_recheck_one_network_error(monkeypatch) -> None:
    import urllib.error

    from dsar_orchestrator.local_broker import durant_recheck

    def fake_post(*_args, **_kwargs):
        raise urllib.error.URLError("broker down")

    monkeypatch.setattr(durant_recheck, "_post", fake_post)
    result = durant_recheck.recheck_one(
        case_id="CASE-100",
        doc_ref="doc-001",
        text="body",
        subject_summary="x",
        original_rationale="orig",
    )
    assert result["recheck_verdict"] == "reclassify_to_ambiguous"
    assert result["error_state"] == "model_unreachable"


# --- _excluded_durant_refs ---


def test_excluded_refs_picks_only_work_context_only(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_recheck import _excluded_durant_refs

    _seed_inputs(
        case_root,
        excluded=[("ex-1", "b1"), ("ex-2", "b2")],
        extra=[("bio-1", "b3")],
    )
    refs = _excluded_durant_refs(case_root / "working" / "durant_verdicts.jsonl")
    assert {r[0] for r in refs} == {"ex-1", "ex-2"}


# --- run() end-to-end ---


def test_run_processes_all_excluded(case_root: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_recheck

    _seed_inputs(
        case_root,
        excluded=[("ex-1", "b1"), ("ex-2", "b2")],
        extra=[("bio-1", "b3")],
    )

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "recheck_verdict": "confirmed_work_context_only",
                                "rationale": "ok",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_recheck, "_post", fake_post)
    rc = durant_recheck.run(case_root)
    assert rc == 0
    out = case_root / "working" / "durant_underdisclosure_recheck.jsonl"
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    refs = {r["doc_ref"] for r in rows}
    assert refs == {"ex-1", "ex-2"}  # biographical-orig doc NOT rechecked
    assert all(r["recheck_verdict"] == "confirmed_work_context_only" for r in rows)


def test_run_skips_already_rechecked(case_root: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import durant_recheck

    _seed_inputs(case_root, excluded=[("ex-1", "b1"), ("ex-2", "b2")])
    out = case_root / "working" / "durant_underdisclosure_recheck.jsonl"
    out.write_text(
        json.dumps(
            {
                "doc_ref": "ex-1",
                "recheck_verdict": "confirmed_work_context_only",
                "rationale": "prior",
            }
        )
        + "\n"
    )

    def fake_post(*_args, **_kwargs) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "recheck_verdict": "reclassify_to_biographical",
                                "rationale": "new",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(durant_recheck, "_post", fake_post)
    durant_recheck.run(case_root)
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    by_ref = {r["doc_ref"]: r for r in rows}
    assert by_ref["ex-1"]["rationale"] == "prior"  # skipped, not re-rechecked
    assert by_ref["ex-2"]["recheck_verdict"] == "reclassify_to_biographical"


def test_run_returns_1_on_missing_inputs(case_root: Path) -> None:
    from dsar_orchestrator.local_broker.durant_recheck import run

    assert run(case_root) == 1
