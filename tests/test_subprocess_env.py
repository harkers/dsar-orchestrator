"""Tests for subprocess_env.build_subprocess_env (issue #15)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dsar_orchestrator.subprocess_env import build_subprocess_env


def test_prepends_venv_bin_to_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    env = build_subprocess_env()
    interp_bin = str(Path(sys.executable).parent)
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == interp_bin
    # Pre-existing PATH preserved after the venv bin.
    assert "/usr/local/bin" in parts
    assert "/usr/bin" in parts


def test_idempotent_when_venv_bin_already_first(monkeypatch):
    interp_bin = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", interp_bin + os.pathsep + "/usr/bin")
    env = build_subprocess_env()
    # Should NOT double-prepend.
    assert env["PATH"] == interp_bin + os.pathsep + "/usr/bin"


def test_handles_empty_path(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    env = build_subprocess_env()
    interp_bin = str(Path(sys.executable).parent)
    assert env["PATH"] == interp_bin


def test_returns_copy_not_os_environ_reference():
    env = build_subprocess_env()
    env["DSAR_TEST_SENTINEL_KEY"] = "test-value"
    assert "DSAR_TEST_SENTINEL_KEY" not in os.environ


def test_preserves_other_env_vars(monkeypatch):
    monkeypatch.setenv("DSAR_TEST_KEEP_ME", "hello")
    env = build_subprocess_env()
    assert env.get("DSAR_TEST_KEEP_ME") == "hello"
