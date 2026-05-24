"""Conductor-owned people_register adapter — Stage 3 (one of three).

Bridges to the toolkit's ``dsar_pipeline.people_register.build_people_register``.
That function reads ``working/<ref>_tags.json`` files, clusters the
detected person mentions via embeddings, and writes
``working/people_register.json``. The conductor's resume cascade +
module agent look for ``working/person_index.json`` with an
``upstream_hash`` field; this adapter calls the toolkit's builder,
adds the upstream hash + producer fields, and writes the
conductor-shaped artefact.

The builder is a normal Python entry (no cwd/global state coupling
like ingest/detect), so the adapter calls it directly and uses an
injectable ``builder_fn`` for hermetic tests.

**Retirement contract.** When the toolkit ships a thin Python entry
``dsar_pipeline.people_register.run_for_case(case_path)`` that
writes ``person_index.json`` with the conductor's expected shape,
this adapter retires.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import sha256_file

PRODUCER_VERSION = "dsar_orchestrator.adapters.people_register 0.3.0"
SCHEMA_VERSION = "1.0"

# Injectable builder: (working_dir) -> dict (matches toolkit's
# build_people_register return shape).
BuilderFn = Callable[[Path], dict[str, Any]]


def _default_builder() -> BuilderFn:
    """Lazy-resolve ``dsar_pipeline.people_register.build_people_register``."""
    try:
        mod = importlib.import_module("dsar_pipeline.people_register")
    except ImportError as exc:
        raise DSARPipelineError(
            "dsar_pipeline.people_register is not installed. The "
            "conductor's people-register adapter needs it to cluster "
            "person mentions. Install dsar-toolkit (pip install -e "
            "~/projects/dsar-toolkit/) and retry."
        ) from exc

    def run(working_dir: Path) -> dict[str, Any]:
        return mod.build_people_register(working_dir)

    return run


def run_for_case(cfg: CaseConfig, *, builder_fn: BuilderFn | None = None) -> None:
    """Drive the toolkit's builder; write the conductor's
    ``working/person_index.json``."""
    if builder_fn is None:
        builder_fn = _default_builder()

    working = cfg.case_path / "working"
    working.mkdir(parents=True, exist_ok=True)

    try:
        result = builder_fn(working)
    except Exception as exc:
        raise DSARPipelineError(
            f"case={cfg.case_no}: people_register builder failed: {exc}"
        ) from exc

    upstream_hash = _hash_embeddings(cfg.case_path)
    _write_person_index(working, result, upstream_hash)


def _hash_embeddings(case_path: Path) -> str:
    """Upstream for people_register is the embeddings JSONL (mirrors
    stages._hash_embeddings)."""
    p = case_path / "working" / "embeddings.jsonl"
    return sha256_file(p) if p.exists() else ""


def _write_person_index(
    working: Path,
    builder_result: dict[str, Any],
    upstream_hash: str,
) -> None:
    """Write ``working/person_index.json`` atomically.

    Layout: copy the toolkit's clusters + alias_to_id through, add the
    cascade fields the conductor expects.
    """
    out_path = working / "person_index.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    obj = {
        "clusters": builder_result.get("clusters", []),
        "alias_to_id": builder_result.get("alias_to_id", {}),
        "threshold": builder_result.get("threshold"),
        "model": builder_result.get("model"),
        "generated_at": builder_result.get("generated_at"),
        "upstream_hash": upstream_hash,
        "schema_version": SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
