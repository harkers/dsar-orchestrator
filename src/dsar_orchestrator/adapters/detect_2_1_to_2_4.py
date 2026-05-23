"""Conductor-owned detect adapter — Stage 2.1 → 2.4.

Bridges to the toolkit's ``dsar_pipeline.detect`` module which runs
spaCy + Presidio + regex detectors and writes one
``working/<ref>_tags.json`` file per doc. The conductor's resume
cascade prefers a single ``working/detect_entities.jsonl`` (one row
per ref), so this adapter aggregates the per-ref tag files into that
shape on top of the toolkit's actual output.

Like the ingest adapter, the toolkit's detect derives CASE_DIR from
cwd, so the adapter invokes it as ``python -m dsar_pipeline.detect
<subject_name>`` with cwd=case_path.

**Retirement contract.** When the toolkit ships a thin Python entry
``dsar_pipeline.detect.run_for_case(case_path, subject_name)`` that
writes ``detect_entities.jsonl`` directly with the conductor's
expected shape, this adapter retires.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import compute_register_hash

PRODUCER_VERSION = "dsar_orchestrator.adapters.detect_2_1_to_2_4 0.1.0"
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


def run_for_case(cfg: CaseConfig, *, runner: RunnerFn | None = None) -> None:
    """Drive the toolkit's detect; aggregate to detect_entities.jsonl."""
    if runner is None:
        runner = _default_runner()

    env = dict(os.environ)
    env["DSAR_CASE_ROOT"] = str(cfg.case_path.parent)

    subject_name = cfg.subject_identifier.primary_name if cfg.subject_identifier else ""
    argv = [sys.executable, "-m", "dsar_pipeline.detect"]
    if subject_name:
        argv.append(subject_name)

    completed = runner(argv, env, cfg.case_path)
    if completed.returncode != 0:
        stderr = (completed.stderr or "")[-2000:]
        raise DSARPipelineError(
            f"case={cfg.case_no}: detect module exited "
            f"{completed.returncode}. stderr tail:\n{stderr}"
        )

    working = cfg.case_path / "working"
    tag_files = sorted(working.glob("*_tags.json"))
    if not tag_files:
        raise DSARPipelineError(
            f"case={cfg.case_no}: detect completed but no <ref>_tags.json "
            f"files were produced in {working}."
        )

    upstream_hash = _hash_register(cfg.case_path)
    _aggregate_to_detect_entities(working, tag_files, upstream_hash)


def _hash_register(case_path: Path) -> str:
    """Upstream for detect is the register (mirrors stages._hash_register
    which delegates to hash_chain.compute_register_hash — hashes
    register.json + each ref's text_path)."""
    register_path = case_path / "working" / "register.json"
    if not register_path.exists():
        return ""
    return compute_register_hash(register_path)


def _aggregate_to_detect_entities(
    working: Path,
    tag_files: list[Path],
    upstream_hash: str,
) -> None:
    """Write ``working/detect_entities.jsonl`` atomically.

    One row per tag file; each row carries the ref + the toolkit's
    per-ref tag payload + provenance fields.
    """
    out_path = working / "detect_entities.jsonl"
    tmp_path = out_path.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for tag_path in tag_files:
            ref = tag_path.stem.removesuffix("_tags")
            tags: Any
            try:
                tags = json.loads(tag_path.read_text())
            except json.JSONDecodeError as exc:
                # The toolkit producing a corrupt tag file is a real
                # bug — fail loud so the operator sees it, rather
                # than emitting a sentinel row that downstream agents
                # accept as valid.
                raise DSARPipelineError(f"malformed tag file at {tag_path}: {exc}") from exc
            # The module agent for detect_2_1_to_2_4 expects an
            # ``entities`` field per row. Project it out of the
            # toolkit's tag payload (default to empty list when the
            # tag file doesn't carry one).
            entities = tags.get("entities", []) if isinstance(tags, dict) else []
            row = {
                "ref": ref,
                "entities": entities,
                "tags": tags,
                "upstream_hash": upstream_hash,
                "schema_version": SCHEMA_VERSION,
                "producer_version": PRODUCER_VERSION,
            }
            f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
