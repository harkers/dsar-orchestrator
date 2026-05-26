"""Tests for the post-Phase-5 subparser-based CLI (spec §10.2).

Covers:
  - Back-compat: ``dsar-conductor --case X`` (no subcommand) routes to ``run``.
  - Explicit ``dsar-conductor run --case X`` works.
  - ``--auto-fitness`` flag on ``run`` is parsed + invokes the inline canary.
  - ``--force-skip-fitness "<reason>"`` sets the
    ``DSAR_FORCE_SKIP_FITNESS_REASON`` env var; empty reason is rejected.
  - ``dsar-conductor verify --check {prompt-versions, fitness-report}``
    dispatches to the appropriate verifier and propagates ``--strict``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock


def _seed_case(case_dir: Path) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case_config.json").write_text(
        json.dumps(
            {
                "case_no": "TEST",
                "case_scope": "x",
                "fitness_check_enabled": False,
            }
        ),
        encoding="utf-8",
    )


def test_default_subcommand_is_run_backcompat(tmp_path):
    """`dsar-conductor --case X` (no subcommand) still works = run."""
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main

    with mock.patch("dsar_orchestrator.cli.run") as run_mock:
        rc = main(["--case", "TEST", "--case-root", str(case), "--check"])
    assert rc == 0
    run_mock.assert_called_once()
    _, kwargs = run_mock.call_args
    assert kwargs.get("case_no") == "TEST"


def test_explicit_run_subcommand_works(tmp_path):
    """`dsar-conductor run --case X` works (same as default)."""
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main

    with mock.patch("dsar_orchestrator.cli.run") as run_mock:
        rc = main(["run", "--case", "TEST", "--case-root", str(case), "--check"])
    assert rc == 0
    run_mock.assert_called_once()


def test_run_subcommand_auto_fitness_flag_parsed(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main

    with (
        mock.patch("dsar_orchestrator.cli.run") as _run_mock,
        mock.patch("dsar_orchestrator.cli._inline_fitness_canary") as _canary_mock,
    ):
        _canary_mock.return_value = 0
        rc = main(
            [
                "run",
                "--case",
                "TEST",
                "--case-root",
                str(case),
                "--check",
                "--auto-fitness",
            ]
        )
    assert rc == 0
    _canary_mock.assert_called_once()


def test_run_subcommand_force_skip_fitness_sets_env(tmp_path, monkeypatch):
    """--force-skip-fitness "<reason>" sets a config override on cfg."""
    monkeypatch.delenv("DSAR_FORCE_SKIP_FITNESS_REASON", raising=False)
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main

    with mock.patch("dsar_orchestrator.cli.run") as _run_mock:
        rc = main(
            [
                "run",
                "--case",
                "TEST",
                "--case-root",
                str(case),
                "--check",
                "--force-skip-fitness",
                "operator pilot",
            ]
        )
    assert rc == 0
    import os

    assert os.environ.get("DSAR_FORCE_SKIP_FITNESS_REASON") == "operator pilot"


def test_run_subcommand_force_skip_rejects_empty(tmp_path):
    """--force-skip-fitness "" is rejected (non-blank required)."""
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main

    rc = main(
        [
            "run",
            "--case",
            "TEST",
            "--case-root",
            str(case),
            "--check",
            "--force-skip-fitness",
            "",
        ]
    )
    assert rc != 0


def test_verify_subcommand_prompt_versions_dispatches(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    from dsar_orchestrator.verify import VerifyResult

    with mock.patch("dsar_orchestrator.cli.verify_prompt_versions") as v_mock:
        v_mock.return_value = VerifyResult(ok=True, exit_code=0)
        rc = main(
            [
                "verify",
                "--case",
                "TEST",
                "--case-root",
                str(case),
                "--check",
                "prompt-versions",
            ]
        )
    assert rc == 0
    v_mock.assert_called_once()


def test_verify_subcommand_fitness_report_dispatches(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    from dsar_orchestrator.verify import VerifyResult

    with mock.patch("dsar_orchestrator.cli.verify_fitness_report") as v_mock:
        v_mock.return_value = VerifyResult(ok=True, exit_code=0)
        rc = main(
            [
                "verify",
                "--case",
                "TEST",
                "--case-root",
                str(case),
                "--check",
                "fitness-report",
            ]
        )
    assert rc == 0
    v_mock.assert_called_once()


def test_verify_subcommand_strict_propagates(tmp_path):
    case = tmp_path / "case"
    _seed_case(case)
    from dsar_orchestrator.cli import main
    from dsar_orchestrator.verify import VerifyResult

    with mock.patch("dsar_orchestrator.cli.verify_prompt_versions") as v_mock:
        v_mock.return_value = VerifyResult(ok=False, exit_code=2)
        rc = main(
            [
                "verify",
                "--case",
                "TEST",
                "--case-root",
                str(case),
                "--check",
                "prompt-versions",
                "--strict",
            ]
        )
    assert rc == 2
    _args, kwargs = v_mock.call_args
    assert kwargs.get("strict") is True
