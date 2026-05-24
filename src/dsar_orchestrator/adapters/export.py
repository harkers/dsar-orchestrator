"""Conductor-owned export adapter — Stage 9.

Bridges to ``dsar_pipeline.export`` module. Bake is a separate
preceding stage (Stage 7 / ``adapters.bake``); by the time this
adapter runs, ``<case>/redacted/`` already exists.

Runs ``python -m dsar_pipeline.export`` with cwd=case dir.  Converts
``redacted/`` to final PDF/A deliverables in ``<case>/output/``, plus
the toolkit's ``manifest.md`` summary file.

The adapter then writes its own ``output/manifest.json`` (cascade
anchor — distinct from the toolkit's ``manifest.md`` summary file)
with ``upstream_hash`` over the redacted tree, so resumes correctly
invalidate when redacted output changes.

**Retirement contract.** When the toolkit ships a thin Python entry
``dsar_pipeline.export.run_for_case(case_path)`` that drives export +
writes a JSON manifest, this adapter retires.
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

PRODUCER_VERSION = "dsar_orchestrator.adapters.export 0.3.0"
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
            timeout=3600,
            check=False,
        )

    return run


def run_for_case(
    cfg: CaseConfig,
    *,
    runner: RunnerFn | None = None,
) -> None:
    """Run the export module; write the cascade anchor manifest."""
    if runner is None:
        runner = _default_runner()

    env = dict(os.environ)
    env["DSAR_CASE_ROOT"] = str(cfg.case_path.parent)

    export_argv = [sys.executable, "-m", "dsar_pipeline.export"]
    export_result = runner(export_argv, env, cfg.case_path)
    if export_result.returncode != 0:
        stderr = (export_result.stderr or "")[-2000:]
        raise DSARPipelineError(
            f"case={cfg.case_no}: export module exited "
            f"{export_result.returncode}. stderr tail:\n{stderr}"
        )

    output_dir = cfg.case_path / "output"
    if not output_dir.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: export completed but output/ directory missing at {output_dir}."
        )

    upstream_hash = _hash_redacted_tree(cfg.case_path / "redacted")
    summary = _summarise_output_dir(output_dir)
    _write_manifest(output_dir, upstream_hash, summary)


def _hash_redacted_tree(redacted_dir: Path) -> str:
    """Mirror stages._hash_redacted_dir: pairs of (rel-path, sha256)."""
    if not redacted_dir.exists():
        return ""
    pairs: list[tuple[str, str]] = []
    for p in sorted(redacted_dir.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(redacted_dir))
            pairs.append((rel, sha256_file(p)))
    return hash_pairs(pairs)


def _summarise_output_dir(output_dir: Path) -> dict:
    """Cheap counts the dashboard can show."""
    files = [p for p in output_dir.rglob("*") if p.is_file()]
    by_ext: dict[str, int] = {}
    for p in files:
        ext = p.suffix.lower() or "(no ext)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    return {"total_files": len(files), "by_extension": by_ext}


def _write_manifest(output_dir: Path, upstream_hash: str, summary: dict) -> None:
    """Atomically write ``output/manifest.json`` (cascade anchor)."""
    out_path = output_dir / "manifest.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    obj = {
        "completed": True,
        "upstream_hash": upstream_hash,
        "summary": summary,
        "schema_version": SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
