"""End-to-end pipeline test against a 100-doc synthetic case.

Drives the full orchestrator + toolkit stubs against a freshly-
synthesized 100-doc case. Validates:

- The whole 8-stage pipeline completes
- Every stage's module agent reports ok (the stubs produce valid
  artefacts; the agents validate them)
- Audit logs land where they should
- The synthetic truth class distribution matches what we expect
- Resume cascade behaves on a second run (everything skipped)
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from dsar_orchestrator.pipeline import run
from dsar_orchestrator.synthesis import synthesize_case


@pytest.fixture
def synthetic_100_case(tmp_path: Path, monkeypatch):
    """Generate a 100-doc case + install toolkit stubs + redirect HOME."""
    from tests._toolkit_stubs.stubs import all_stubs

    for name, mod in all_stubs().items():
        monkeypatch.setitem(sys.modules, name, mod)
        if "." in name:
            pkg_name = name.split(".")[0]
            if pkg_name not in sys.modules:
                pkg = types.ModuleType(pkg_name)
                pkg.__path__ = []
                monkeypatch.setitem(sys.modules, pkg_name, pkg)

    monkeypatch.setenv("HOME", str(tmp_path))
    case_dir_root = tmp_path / "dsars" / "cases"
    case_dir_root.mkdir(parents=True)

    # Mock the scope-classify adapter's subprocess runner — see
    # tests/integration/test_full_pipeline_with_stubs.py for the
    # equivalent in the other integration suite.
    import subprocess as _subprocess

    from dsar_orchestrator.adapters import scope_classify as _scope_classify

    def _fake_scope_check_runner(argv, env):
        case_no = argv[argv.index("--case") + 1]
        case_root = Path(env.get("DSAR_CASE_ROOT", ""))
        case_path = case_root / case_no
        register_path = case_path / "working" / "register.json"
        refs = []
        if register_path.exists():
            register = json.loads(register_path.read_text())
            refs = [r["ref"] for r in register.get("refs", [])]
        verdicts_path = case_path / "working" / "scope_verdicts.jsonl"
        verdicts_path.write_text(
            "\n".join(json.dumps({"ref": r, "scope_verdict": "present"}) for r in refs)
            + ("\n" if refs else "")
        )
        return _subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(_scope_classify, "_default_runner", lambda: _fake_scope_check_runner)

    # Mock the redact adapter's subprocess runner. Same trick as in
    # tests/integration/test_full_pipeline_with_stubs.py.
    from dsar_orchestrator.adapters import redact as _redact

    def _fake_redact_runner(argv, env):
        case_no = argv[argv.index("--case") + 1]
        case_root = Path(env.get("DSAR_CASE_ROOT", ""))
        case_path = case_root / case_no
        working = case_path / "working"
        working.mkdir(parents=True, exist_ok=True)
        register_path = working / "register.json"
        refs = []
        if register_path.exists():
            register = json.loads(register_path.read_text())
            refs = [r["ref"] for r in register.get("refs", [])]
        (working / "redaction_input.jsonl").write_text(
            "\n".join(json.dumps({"ref": r, "spans": [], "reason_code": "pii"}) for r in refs)
            + ("\n" if refs else "")
        )
        redacted_dir = case_path / "redacted"
        redacted_dir.mkdir(parents=True, exist_ok=True)
        for r in refs:
            (redacted_dir / f"{r}.txt").write_text("[REDACTED]\n")
        return _subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(_redact, "_default_runner", lambda: _fake_redact_runner)

    result = synthesize_case("800500", case_dir_root)
    return result


def _read_audit_jsonl(case_no: str, name: str) -> list[dict]:
    p = Path.home() / ".dsar-audit" / case_no / name
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_synthesize_then_pipeline_full_run(synthetic_100_case) -> None:
    case = synthetic_100_case
    # First confirm the synthesis shape we expect
    assert case.doc_count == 100
    assert case.by_truth_class == {
        "gold": 30,
        "mid": 12,
        "decoy": 10,
        "off_finance": 13,
        "off_topic": 35,
    }

    # Drive the orchestrator end-to-end
    report = run(case.case_no, case_root=case.case_path)

    # All 8 coarse stages ran
    for stage in (
        "ingest",
        "stage_2_parallel",
        "stage_3_parallel",
        "scope_classify",
        "pii_classify",
        "redact",
        "redact_verify",
        "export",
    ):
        assert stage in report.stages_run, f"stage {stage} did not run"

    # pipeline.jsonl audit log captures all of them
    pipeline_rows = _read_audit_jsonl(case.case_no, "pipeline.jsonl")
    stages_with_end = {
        r["stage"]
        for r in pipeline_rows
        if r.get("event") == "stage_end" and r.get("outcome") == "ok"
    }
    assert {
        "ingest",
        "stage_2_parallel",
        "stage_3_parallel",
        "scope_classify",
        "pii_classify",
        "redact",
        "redact_verify",
        "export",
    } <= stages_with_end


def test_synthetic_case_all_module_agents_pass(synthetic_100_case) -> None:
    """Every in-process module agent should report ok against the
    stub-produced artefacts. If any reports critical, the pipeline
    halts; if warning, the row is recorded but the run continues."""
    case = synthetic_100_case
    run(case.case_no, case_root=case.case_path)

    checks = _read_audit_jsonl(case.case_no, "module_checks.jsonl")
    # Every sub-stage that ran should have a check row
    sub_stages_with_rows = {row["sub_stage"] for row in checks}
    # All 12 stage agents should fire (some are skipped via cfg flags
    # but those still record an info-class row via the agent's own
    # short-circuit)
    expected = {
        "ingest",
        "embed",
        "detect_2_1_to_2_4",
        "pii_discovery",
        "people_register",
        "scope_prefilter",
        "rerank",
        "scope_classify",
        "pii_classify",
        "redact",
        "redact_verify",
        "export",
    }
    assert expected <= sub_stages_with_rows

    # No agent should have raised a critical halt — the toolkit stubs
    # are designed to produce valid artefacts that satisfy every
    # agent's checks.
    criticals = [r for r in checks if r.get("severity") == "critical"]
    assert criticals == [], f"agents reported criticals: {criticals}"


def test_synthetic_case_resume_skips_everything_on_second_run(
    synthetic_100_case,
) -> None:
    """Second pipeline.run() should find all artefacts fresh and skip
    every coarse stage."""
    case = synthetic_100_case
    first = run(case.case_no, case_root=case.case_path)
    assert len(first.stages_run) == 8

    second = run(case.case_no, case_root=case.case_path)
    # Everything is fresh; nothing actually re-ran
    assert second.stages_run == []
    # Every coarse stage shows up in the "skipped via cascade" set
    skipped = {s for s, _reason in []} | set(second.stages_skipped)
    assert "ingest" in skipped
    assert "export" in skipped


def test_synthetic_case_force_reruns_everything(synthetic_100_case) -> None:
    """--force on the second pass must re-run every stage despite all
    artefacts being fresh."""
    case = synthetic_100_case
    run(case.case_no, case_root=case.case_path)
    second = run(case.case_no, case_root=case.case_path, force=True)
    assert "ingest" in second.stages_run
    assert "export" in second.stages_run


def test_synthetic_case_check_does_not_invoke_stages(synthetic_100_case) -> None:
    """--check on a fresh case (nothing run yet) should print the plan
    without producing any artefacts."""
    case = synthetic_100_case
    report = run(case.case_no, case_root=case.case_path, check=True)
    # check mode populates stages_skipped with "would have run"
    assert "ingest" in report.stages_skipped
    # But no artefacts written
    assert not (case.case_path / "working" / "embeddings.jsonl").exists()
    assert not (case.case_path / "working" / "register.json").exists()
