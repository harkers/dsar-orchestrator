"""Conductor-owned ingest adapter — Stage 1.

Bridges to the toolkit's ``dsar_pipeline.ingest`` module. The toolkit
ships an ``ingest(data_subject_name, case_number_override)`` Python
entry but it derives ``CASE_DIR`` from cwd, so we invoke it via
``python -m dsar_pipeline.ingest <subject_name>`` with cwd=case_path.

The ingest stage walks ``<case>/source/``, extracts text via the
ingest_v3 bridge layer, assigns ref numbers, and writes
``working/register.json``. This adapter validates the register was
produced and, if the toolkit hasn't already, stamps an
``upstream_hash`` field over the source tree so the conductor's
resume cascade can correctly detect downstream invalidation.

**Retirement contract.** When the toolkit ships a thin Python entry
``dsar_pipeline.ingest.run_for_case(case_path, subject_name)`` that
writes register.json with the conductor's expected hash field, this
adapter retires.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import hash_pairs, sha256_file

PRODUCER_VERSION = "dsar_orchestrator.adapters.ingest 0.1.0"

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
            timeout=1800,
            check=False,
        )

    return run


def run_for_case(cfg: CaseConfig, *, runner: RunnerFn | None = None) -> None:
    """Drive the toolkit's ingest; validate + augment register.json."""
    if runner is None:
        runner = _default_runner()

    env = dict(os.environ)
    env["DSAR_CASE_ROOT"] = str(cfg.case_path.parent)

    subject_name = cfg.subject_identifier.primary_name if cfg.subject_identifier else ""
    argv = [sys.executable, "-m", "dsar_pipeline.ingest"]
    if subject_name:
        argv.append(subject_name)

    completed = runner(argv, env, cfg.case_path)
    if completed.returncode != 0:
        stderr = (completed.stderr or "")[-2000:]
        raise DSARPipelineError(
            f"case={cfg.case_no}: ingest module exited "
            f"{completed.returncode}. stderr tail:\n{stderr}"
        )

    register_path = cfg.case_path / "working" / "register.json"
    if not register_path.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: ingest completed but register.json "
            f"was not produced at {register_path}."
        )

    _ensure_upstream_hash(cfg.case_path, register_path)


def _ensure_upstream_hash(case_path: Path, register_path: Path) -> None:
    """Stamp ``upstream_hash`` over the source tree if the toolkit
    didn't write one. Idempotent: trust the toolkit's hash if present.
    Atomic write via temp+rename."""
    try:
        register = json.loads(register_path.read_text())
    except json.JSONDecodeError as exc:
        raise DSARPipelineError(
            f"register.json at {register_path} is not valid JSON: {exc}"
        ) from exc

    if register.get("upstream_hash"):
        return

    src = case_path / "source"
    pairs: list[tuple[str, str]] = []
    if src.exists():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(src))
                pairs.append((rel, sha256_file(p)))
    register["upstream_hash"] = hash_pairs(pairs)
    register["producer_version"] = PRODUCER_VERSION

    tmp_path = register_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(register, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, register_path)
