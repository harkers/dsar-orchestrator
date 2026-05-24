"""Conductor-owned ingest adapter — Stage 1.

Bridges to the toolkit's ``dsar_pipeline.ingest`` module. The toolkit
ships an ``ingest(data_subject_name, case_number_override)`` Python
entry but it derives ``CASE_DIR`` from cwd, so we invoke it via
``python -m dsar_pipeline.ingest <subject_name>`` with cwd=case_path.

The ingest stage walks ``<case>/source/``, extracts text via the
ingest_v3 bridge layer, assigns ref numbers, and writes
``working/register.json`` as a **flat list of file-record dicts**
(toolkit's shape). This adapter validates the register was produced
and stamps conductor-owned metadata (``upstream_hash`` over source
tree + schema/producer versions) into a sibling file
``working/register_meta.json`` so the cascade can detect downstream
invalidation without mutating the toolkit's artefact.

**Retirement contract.** When the toolkit ships a thin Python entry
``dsar_pipeline.ingest.run_for_case(case_path, subject_name)`` that
writes the conductor-meta sidecar itself, this adapter retires.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import hash_pairs, sha256_file, write_register_meta

PRODUCER_VERSION = "dsar_orchestrator.adapters.ingest 0.1.1"
SCHEMA_VERSION = "1.0"

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
    """Write conductor-owned metadata to ``working/register_meta.json``.

    Per Contract A (issue #8): the toolkit's register.json is a flat
    list and is NOT mutated here. The conductor's cascade reads
    upstream_hash from the sibling meta file instead.

    Always overwrites the meta file (cheap; ingest just ran, source
    tree is the canonical upstream).
    """
    src = case_path / "source"
    pairs: list[tuple[str, str]] = []
    if src.exists():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(src))
                pairs.append((rel, sha256_file(p)))
    write_register_meta(
        case_path,
        upstream_hash=hash_pairs(pairs),
        schema_version=SCHEMA_VERSION,
        producer_version=PRODUCER_VERSION,
    )
