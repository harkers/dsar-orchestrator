"""Tests for the argparse CLI surface.

Implementation-thin: validate args parse correctly + the mutual-
exclusion rule. The actual orchestrator behaviour is tested in
test_pipeline_smoke.py.
"""

from __future__ import annotations

import pytest

from dsar_orchestrator.cli import build_parser


def test_parser_requires_case() -> None:
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_parser_minimal_args() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001"])
    assert args.case == "300001"
    assert args.from_stage is None
    assert args.through_stage is None
    assert args.only_stage is None
    assert args.check is False
    assert args.dry_run is False


def test_parser_check_flag() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001", "--check"])
    assert args.check is True


def test_parser_dry_run_flag() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001", "--dry-run"])
    assert args.dry_run is True


def test_parser_from_stage_choices() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001", "--from", "redact"])
    assert args.from_stage == "redact"


def test_parser_rejects_unknown_stage() -> None:
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["--case", "300001", "--from", "made_up_stage"])


def test_parser_only_stage() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001", "--only", "embed"])
    assert args.only_stage == "embed"


def test_parser_force_flag_defaults_false() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001"])
    assert args.force is False


def test_parser_force_flag_set() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001", "--force"])
    assert args.force is True


def test_parser_acknowledge_issues_defaults_false() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001"])
    assert args.acknowledge_issues is False


def test_parser_acknowledge_issues_set() -> None:
    p = build_parser()
    args = p.parse_args(["--case", "300001", "--acknowledge-issues"])
    assert args.acknowledge_issues is True
