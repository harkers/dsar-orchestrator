"""Tests for src/dsar_orchestrator/verify.py (spec §4.1 G + §4.4)."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path


def _seed_prompt_registry(tmp_path: Path) -> tuple[Path, str, str]:
    """Build a minimal in-tmp prompt registry + archive. Returns
    (prompts_dir, canonical_seal, effective_sha)."""
    import hashlib

    from dsar_pipeline.gates.prompt_loader import (
        _normalise_whitespace,
        compute_seal,
    )

    prompts = tmp_path / "prompts"
    archive = prompts / "_archive" / "durant.system"
    archive.mkdir(parents=True)
    body = "Test body of durant.system prompt.\n"
    meta = {
        "prompt_id": "durant.system",
        "version": "1.0.0",
        "droppable_blocks": [],
    }
    seal = compute_seal(meta, body)
    # effective sha = sha256 of LF-normalised body (same path used in
    # PromptLoader.load → asset.effective_sha256).
    effective = hashlib.sha256(_normalise_whitespace(body).encode("utf-8")).hexdigest()
    asset_text = (
        f'---\nprompt_id: "durant.system"\nversion: "1.0.0"\n'
        f'seal_sha256: "{seal}"\ndroppable_blocks: []\n---\n{body}'
    )
    (prompts / "durant.system.md").write_text(asset_text, encoding="utf-8")
    # Archive
    with gzip.GzipFile(archive / "1.0.0.md.gz", "wb", mtime=0) as gz:
        gz.write(asset_text.encode("utf-8"))
    (prompts / "_registry.json").write_text(
        json.dumps(
            {
                "durant.system": [
                    {
                        "version": "1.0.0",
                        "seal_sha256": seal,
                        "archived_at": "2026-05-26",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return prompts, seal, effective


def _seed_case(
    case_dir: Path,
    primary_rows: list[dict],
    recheck_rows: list[dict] | None = None,
) -> None:
    (case_dir / "working").mkdir(parents=True, exist_ok=True)
    with open(case_dir / "working" / "durant_verdicts.jsonl", "w", encoding="utf-8") as f:
        for r in primary_rows:
            f.write(json.dumps(r) + "\n")
    if recheck_rows is not None:
        with open(
            case_dir / "working" / "durant_underdisclosure_recheck.jsonl",
            "w",
            encoding="utf-8",
        ) as f:
            for r in recheck_rows:
                f.write(json.dumps(r) + "\n")


def test_verify_prompt_versions_fresh_case_returns_ok(tmp_path, monkeypatch):
    prompts, seal, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr("dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(
        case,
        primary_rows=[
            {
                "doc_ref": "r1",
                "prompt_id": "durant.system",
                "prompt_canonical_seal_sha256": seal,
                "prompt_applied_strips": [],
                "prompt_effective_sha256": effective,
            }
        ],
    )
    from dsar_orchestrator.verify import verify_prompt_versions

    result = verify_prompt_versions(case)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.errors == []


def test_verify_prompt_versions_planted_drift_exits_2(tmp_path, monkeypatch):
    """If the audit row's effective_sha256 doesn't match the archive's
    replayed body → exit 2."""
    prompts, seal, _ = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr("dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(
        case,
        primary_rows=[
            {
                "doc_ref": "r1",
                "prompt_id": "durant.system",
                "prompt_canonical_seal_sha256": seal,
                "prompt_applied_strips": [],
                "prompt_effective_sha256": "0" * 64,  # planted drift
            }
        ],
    )
    from dsar_orchestrator.verify import verify_prompt_versions

    result = verify_prompt_versions(case)
    assert result.ok is False
    assert result.exit_code == 2
    assert any("effective_sha256" in e for e in result.errors)


def test_verify_prompt_versions_unknown_seal_exits_2(tmp_path, monkeypatch):
    prompts, _, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr("dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(
        case,
        primary_rows=[
            {
                "doc_ref": "r1",
                "prompt_id": "durant.system",
                "prompt_canonical_seal_sha256": "f" * 64,  # not in registry
                "prompt_applied_strips": [],
                "prompt_effective_sha256": effective,
            }
        ],
    )
    from dsar_orchestrator.verify import verify_prompt_versions

    result = verify_prompt_versions(case)
    assert result.ok is False
    assert result.exit_code == 2


def test_verify_prompt_versions_id_mismatch_exits_2(tmp_path, monkeypatch):
    """Audit row's prompt_id != registry's prompt_id for the matching seal."""
    prompts, seal, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr("dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(
        case,
        primary_rows=[
            {
                "doc_ref": "r1",
                "prompt_id": "WRONG.id",
                "prompt_canonical_seal_sha256": seal,
                "prompt_applied_strips": [],
                "prompt_effective_sha256": effective,
            }
        ],
    )
    from dsar_orchestrator.verify import verify_prompt_versions

    result = verify_prompt_versions(case)
    assert result.ok is False
    assert result.exit_code == 2


def test_verify_prompt_versions_older_version_warn_unless_strict(tmp_path, monkeypatch):
    """Registry has v1.0.0 AND v1.1.0; case audit refs v1.0.0. Without
    --strict → warning + exit 0. With --strict → exit 2."""
    import hashlib

    from dsar_pipeline.gates.prompt_loader import (
        _normalise_whitespace,
        compute_seal,
    )

    prompts, seal_v1, effective_v1 = _seed_prompt_registry(tmp_path)
    body_v11 = "Body of durant.system v1.1.0.\n"
    meta_v11 = {
        "prompt_id": "durant.system",
        "version": "1.1.0",
        "droppable_blocks": [],
    }
    seal_v11 = compute_seal(meta_v11, body_v11)
    _eff_v11 = hashlib.sha256(_normalise_whitespace(body_v11).encode("utf-8")).hexdigest()
    asset_text_v11 = (
        f'---\nprompt_id: "durant.system"\nversion: "1.1.0"\n'
        f'seal_sha256: "{seal_v11}"\ndroppable_blocks: []\n---\n{body_v11}'
    )
    (prompts / "durant.system.md").write_text(asset_text_v11, encoding="utf-8")
    archive = prompts / "_archive" / "durant.system"
    with gzip.GzipFile(archive / "1.1.0.md.gz", "wb", mtime=0) as gz:
        gz.write(asset_text_v11.encode("utf-8"))
    registry = json.loads((prompts / "_registry.json").read_text())
    registry["durant.system"].append(
        {
            "version": "1.1.0",
            "seal_sha256": seal_v11,
            "archived_at": "2026-05-27",
        }
    )
    (prompts / "_registry.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr("dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(
        case,
        primary_rows=[
            {
                "doc_ref": "r1",
                "prompt_id": "durant.system",
                "prompt_canonical_seal_sha256": seal_v1,  # old
                "prompt_applied_strips": [],
                "prompt_effective_sha256": effective_v1,
            }
        ],
    )
    from dsar_orchestrator.verify import verify_prompt_versions

    result = verify_prompt_versions(case, strict=False)
    assert result.exit_code == 0
    assert result.warnings  # at least one
    result_strict = verify_prompt_versions(case, strict=True)
    assert result_strict.exit_code == 2


def test_verify_prompt_versions_includes_recheck_jsonl(tmp_path, monkeypatch):
    """Recheck JSONL rows are verified too."""
    prompts, seal, effective = _seed_prompt_registry(tmp_path)
    monkeypatch.setattr("dsar_pipeline.gates.prompt_loader._PROMPTS_DIR", prompts)
    case = tmp_path / "case"
    _seed_case(
        case,
        primary_rows=[
            {
                "doc_ref": "r1",
                "prompt_id": "durant.system",
                "prompt_canonical_seal_sha256": seal,
                "prompt_applied_strips": [],
                "prompt_effective_sha256": effective,
            }
        ],
        recheck_rows=[
            {
                "doc_ref": "r1",
                "prompt_id": "durant.system",
                "prompt_canonical_seal_sha256": "f" * 64,  # planted
                "prompt_applied_strips": [],
                "prompt_effective_sha256": effective,
            }
        ],
    )
    from dsar_orchestrator.verify import verify_prompt_versions

    result = verify_prompt_versions(case)
    assert result.exit_code == 2


def test_verify_fitness_report_passes_when_fresh_passing(tmp_path, monkeypatch):
    """A recent passing report under <report-dir> → OK."""
    case = tmp_path / "case"
    case.mkdir()
    (case / "case_config.json").write_text(
        json.dumps(
            {
                "case_no": "TEST",
                "case_scope": "x",
                "fitness_check_deployment_id": "test_deploy",
                "fitness_check_max_report_age_days": 30,
            }
        ),
        encoding="utf-8",
    )
    report_root = tmp_path / "reports"
    deploy = report_root / "test_deploy"
    deploy.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    (deploy / f"{now.replace(':', '_')}.json").write_text(
        json.dumps(
            {
                "report_id": "abc",
                "generated_at": now,
                "deployment_id": "test_deploy",
                "passed": True,
                "fails": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    from dsar_orchestrator.verify import verify_fitness_report

    result = verify_fitness_report(case)
    assert result.ok is True
    assert result.exit_code == 0


def test_verify_fitness_report_fails_when_no_report(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    (case / "case_config.json").write_text(
        json.dumps(
            {
                "case_no": "TEST",
                "case_scope": "x",
                "fitness_check_deployment_id": "test_deploy_missing",
            }
        ),
        encoding="utf-8",
    )
    report_root = tmp_path / "reports_empty"
    report_root.mkdir()
    monkeypatch.setenv("DSAR_FITNESS_REPORT_ROOT", str(report_root))
    from dsar_orchestrator.verify import verify_fitness_report

    result = verify_fitness_report(case)
    assert result.ok is False
    assert result.exit_code != 0
