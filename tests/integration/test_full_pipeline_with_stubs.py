"""End-to-end pipeline tests using in-test toolkit stubs.

These tests install fake toolkit modules in ``sys.modules`` so the
orchestrator's lazy imports resolve to the stubs in
``tests/_toolkit_stubs/stubs.py``. The stubs write realistic
artefacts (with correct ``upstream_hash`` fields) so the resume
cascade and audit log behave end-to-end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dsar_orchestrator.pipeline import run


@pytest.fixture
def with_toolkit_stubs(monkeypatch, tmp_path: Path):
    """Install toolkit stubs in sys.modules for the duration of the test.

    Also redirects ~/.dsar-audit/ into tmp_path so test runs don't
    pollute the operator's real audit directory.
    """
    from tests._toolkit_stubs.stubs import all_stubs

    for name, mod in all_stubs().items():
        monkeypatch.setitem(sys.modules, name, mod)
        # Also register the package itself so 'import dsar_embed' works
        # even if only 'dsar_embed.core' was registered.
        if "." in name:
            pkg_name = name.split(".")[0]
            if pkg_name not in sys.modules:
                import types

                pkg = types.ModuleType(pkg_name)
                pkg.__path__ = []  # mark as a package
                monkeypatch.setitem(sys.modules, pkg_name, pkg)

    # Redirect ~/.dsar-audit/ into tmp_path so we don't pollute the real one.
    monkeypatch.setenv("HOME", str(tmp_path))

    # Mock the scope-classify adapter's subprocess runner so tests
    # don't try to invoke `dsar-scope-check`. The runner writes a
    # minimal scope_verdicts.jsonl that the adapter then reads to
    # build its cascade anchor.
    import subprocess as _subprocess

    from dsar_orchestrator.adapters import scope_classify as _scope_classify

    def _fake_scope_check_runner(argv, env):
        # Parse out the case_no + case_root from argv/env
        case_no = argv[argv.index("--case") + 1]
        case_root = Path(env.get("DSAR_CASE_ROOT", ""))
        case_path = case_root / case_no
        verdicts_path = case_path / "working" / "scope_verdicts.jsonl"
        # Read register.json (written by ingest stub) so we have refs
        register_path = case_path / "working" / "register.json"
        if register_path.exists():
            register = json.loads(register_path.read_text())
            refs = [r["ref"] for r in register.get("refs", [])]
        else:
            refs = []
        verdicts_path.write_text(
            "\n".join(json.dumps({"ref": r, "scope_verdict": "present"}) for r in refs)
            + ("\n" if refs else "")
        )
        return _subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(_scope_classify, "_default_runner", lambda: _fake_scope_check_runner)

    yield tmp_path


@pytest.fixture
def staged_case(with_toolkit_stubs):
    """Create a minimal case directory under the redirected $HOME with a
    valid case_config.json + a couple of source docs."""
    case_no = "300200"
    case_path = with_toolkit_stubs / "dsars" / "cases" / case_no
    case_path.mkdir(parents=True)

    src = case_path / "source"
    src.mkdir()
    (src / "doc1.txt").write_text("hello world from the test")
    (src / "doc2.txt").write_text("a second doc for the test corpus")

    (case_path / "redacted").mkdir()
    (case_path / "output").mkdir()

    config = {
        "case_no": case_no,
        "case_scope": "test scope",
        "subject_identifier": {
            "primary_name": "Test Subject",
            "disambiguation_notes": "for testing",
        },
        "rerank_mode": "shadow",
        "pii_classify_mode": "shadow",
    }
    (case_path / "case_config.json").write_text(json.dumps(config))
    return case_path


# ─── full pipeline ─────────────────────────────────────────────────


def test_full_pipeline_completes_with_stubs(staged_case: Path) -> None:
    """Run the entire 8-stage pipeline against stub modules; assert all
    stages execute + write their artefacts + the audit log is
    populated."""
    case_no = staged_case.name
    report = run(case_no, case_root=staged_case)

    # All 8 stages should have run
    assert "ingest" in report.stages_run
    assert "stage_2_parallel" in report.stages_run
    assert "stage_3_parallel" in report.stages_run
    assert "scope_classify" in report.stages_run
    assert "pii_classify" in report.stages_run
    assert "redact" in report.stages_run
    assert "redact_verify" in report.stages_run
    assert "export" in report.stages_run

    # Artefacts written
    assert (staged_case / "working" / "register.json").exists()
    assert (staged_case / "working" / "embeddings.jsonl").exists()
    assert (staged_case / "working" / "detect_entities.jsonl").exists()
    assert (staged_case / "working" / "pii_discovery.jsonl").exists()
    assert (staged_case / "working" / "cosine_prefilter.jsonl").exists()
    assert (staged_case / "working" / "scope_rerank.jsonl").exists()
    assert (staged_case / "working" / "pii_collection.jsonl").exists()
    assert (staged_case / "working" / "redact_complete.json").exists()
    assert (staged_case / "output" / "manifest.json").exists()


def test_full_pipeline_emits_audit_log(staged_case: Path) -> None:
    case_no = staged_case.name
    run(case_no, case_root=staged_case)

    audit_log = Path.home() / ".dsar-audit" / case_no / "pipeline.jsonl"
    assert audit_log.exists()

    rows = [json.loads(line) for line in audit_log.read_text().splitlines() if line.strip()]
    events_by_stage: dict[str, list[str]] = {}
    for r in rows:
        if r.get("event") in {"stage_start", "stage_end"} and "stage" in r:
            events_by_stage.setdefault(r["stage"], []).append(r["event"])

    # Every stage should have start + end
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
        assert "stage_start" in events_by_stage.get(stage, [])
        assert "stage_end" in events_by_stage.get(stage, [])

    # Final row should be the run_complete event
    assert rows[-1]["event"] == "run_complete"
    assert rows[-1]["halted"] is False


def test_resume_after_partial_completion_skips_done_stages(staged_case: Path) -> None:
    """Run, then run again — the second run should skip stages whose
    artefacts are fresh."""
    case_no = staged_case.name
    run(case_no, case_root=staged_case)

    # Second run should see all artefacts fresh + skip everything
    audit_log = Path.home() / ".dsar-audit" / case_no / "pipeline.jsonl"
    pre_lines = len(audit_log.read_text().splitlines())

    second_report = run(case_no, case_root=staged_case)
    # Stages run on this second pass: nothing (all fresh) — but the
    # audit log gets a run_complete event regardless.
    assert "ingest" not in second_report.stages_run
    assert "embed" not in second_report.stages_run
    # Skipped list shows the coarse stages
    assert any(s == "ingest" for s in second_report.stages_skipped)

    post_lines = len(audit_log.read_text().splitlines())
    # Second run wrote just the run_complete entry + (maybe) some
    # stage_skipped entries
    assert post_lines > pre_lines


def test_force_flag_re_runs_everything(staged_case: Path) -> None:
    """After a successful run, ``--force`` re-runs all stages."""
    case_no = staged_case.name
    run(case_no, case_root=staged_case)
    second = run(case_no, case_root=staged_case, force=True)
    assert "ingest" in second.stages_run
    assert "export" in second.stages_run


def test_resume_after_source_mutation_re_runs_downstream(staged_case: Path) -> None:
    """Mutate source/ after a successful run — next run should detect
    the upstream change via the hash chain and re-run ingest + all
    downstream stages."""
    case_no = staged_case.name
    run(case_no, case_root=staged_case)

    # Mutate source
    (staged_case / "source" / "doc1.txt").write_text("mutated content")

    second = run(case_no, case_root=staged_case)
    # ingest re-runs because source tree changed
    assert "ingest" in second.stages_run
    # downstream-forced rule kicks in: all later stages re-run too
    assert "stage_2_parallel" in second.stages_run
    assert "export" in second.stages_run


def test_pipeline_check_does_not_call_stubs(staged_case: Path) -> None:
    """--check should not invoke any toolkit module — confirmed by
    asserting no artefacts get written."""
    case_no = staged_case.name
    run(case_no, case_root=staged_case, check=True)
    # No artefacts produced
    assert not (staged_case / "working" / "register.json").exists()
    assert not (staged_case / "working" / "embeddings.jsonl").exists()
