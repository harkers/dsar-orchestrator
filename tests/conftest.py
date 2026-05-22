"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def case_root(tmp_path: Path) -> Path:
    """A minimal valid case directory."""
    root = tmp_path / "300001"
    (root / "source").mkdir(parents=True)
    (root / "working").mkdir()
    (root / "redacted").mkdir()
    (root / "output").mkdir()

    config = {
        "case_no": "300001",
        "case_scope": (
            "All personal data about James Carter, Senior Analyst, Finance dept, 2022-2025."
        ),
        "subject_identifier": {
            "primary_name": "James Carter",
            "dob": "1985-03-12",
            "employee_id": "FIN-0241",
            "aliases": ["J. Carter", "Jim Carter"],
            "disambiguation_notes": "NOT James Marshall (Operations).",
        },
        "rerank_mode": "shadow",
        "rerank_threshold": 0.01,
        "pii_classify_mode": "shadow",
        "pii_budget_usd": 5.0,
    }
    (root / "case_config.json").write_text(json.dumps(config, indent=2))
    return root


@pytest.fixture
def audit_root(tmp_path: Path) -> Path:
    """An isolated ~/.dsar-audit/ root for tests."""
    root = tmp_path / "dsar-audit"
    root.mkdir()
    return root
