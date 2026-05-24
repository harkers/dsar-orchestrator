"""Conductor-owned bake adapter — Stage 7 in v5.0.

Bridges to the toolkit's ``dsar-bake`` CLI. Reads
``working/redaction_input.jsonl`` (produced by the redact stage) and
applies redactions to the source files, writing to ``<case>/redacted/``.

This adapter was extracted from ``adapters/export.py`` in v5.0 (rollout
B phase 1) — previously bake ran inside the export adapter, which meant
the verifier couldn't see ``redacted/`` until after verify already ran.
v5.0 promotes bake to its own coarse stage between ``redact`` and
``verify_pdf``.

**Retirement contract.** When the toolkit ships
``dsar_pipeline.bake.run_for_case(case_path)`` (no toolkit issue yet),
this adapter retires.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import sha256_file

PRODUCER_VERSION = "dsar_orchestrator.adapters.bake 0.2.0"
SCHEMA_VERSION = "1.0"
DEFAULT_BAKE_CLI = "dsar-bake"

# runner(argv, env, cwd) -> CompletedProcess
RunnerFn = Callable[[list[str], dict[str, str], Path], subprocess.CompletedProcess]


def _default_runner() -> RunnerFn:
    def run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )

    return run


def run_for_case(
    cfg: CaseConfig,
    *,
    runner: RunnerFn | None = None,
    bake_cli: str = DEFAULT_BAKE_CLI,
) -> None:
    """Drive `dsar-bake --case <id>`; write the cascade anchor manifest."""
    if runner is None:
        runner = _default_runner()

    env = dict(os.environ)
    env["DSAR_CASE_ROOT"] = str(cfg.case_path.parent)

    argv = [bake_cli, "--case", cfg.case_no]
    result = runner(argv, env, cfg.case_path)
    if result.returncode != 0:
        stderr = (result.stderr or "")[-2000:]
        raise DSARPipelineError(
            f"case={cfg.case_no}: bake CLI exited {result.returncode}. stderr tail:\n{stderr}"
        )

    redacted_dir = cfg.case_path / "redacted"
    if not redacted_dir.exists() or not any(redacted_dir.iterdir()):
        raise DSARPipelineError(
            f"case={cfg.case_no}: bake CLI succeeded but redacted/ missing or empty at {redacted_dir}."
        )

    upstream_hash = _hash_redaction_input(cfg.case_path / "working" / "redaction_input.jsonl")
    _write_manifest(cfg.case_path, upstream_hash)


def _hash_redaction_input(plan_path: Path) -> str:
    """Upstream for bake: the redaction plan written by redact stage."""
    return sha256_file(plan_path) if plan_path.exists() else ""


def _write_manifest(case_path: Path, upstream_hash: str) -> None:
    """Atomically write ``working/redact_v4/bake_manifest.json``."""
    out_dir = case_path / "working" / "redact_v4"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "bake_manifest.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    obj = {
        "completed": True,
        "upstream_hash": upstream_hash,
        "schema_version": SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
