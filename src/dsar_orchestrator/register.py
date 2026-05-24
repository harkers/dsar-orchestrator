"""register.json shape helpers (Contract A — issue #8).

Real toolkit writes ``working/register.json`` as a flat list of file-record
dicts (one per source file). Conductor metadata (``upstream_hash`` for the
resume cascade, ``schema_version``, ``producer_version``) lives in a sibling
``working/register_meta.json`` written by the conductor's ingest adapter —
keeps the conductor's cascade machinery alive without mutating the toolkit's
artefact.

This is a leaf module: stdlib + pathlib only, no conductor deps. Both
``hash_chain`` and ``module_agents`` import from here.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path


def read_register(register_path: Path) -> list[dict]:
    """Read the toolkit's `register.json` as a flat list of entries.

    Each entry is a dict with at least ``ref`` (and typically ``path``,
    ``filename``, ``hash``, ``extracted_chars``, etc.).
    """
    with open(register_path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(
            f"register.json at {register_path} expected to be a list of entries, "
            f"got {type(data).__name__}. The conductor's Contract A (issue #8) "
            f"requires the toolkit's flat-list shape."
        )
    return data


def text_path_for_ref(case_path: Path, ref: str) -> Path:
    """Conventional location of extracted text for a ref: `working/<ref>.txt`.

    The toolkit's ingest writes extracted text per ref to this path. Used
    by embed + by upstream-hash computation in :mod:`hash_chain`.
    """
    return case_path / "working" / f"{ref}.txt"


def read_register_meta(case_path: Path) -> dict | None:
    """Read the conductor-owned `working/register_meta.json` sibling file.

    Returns the dict on success, or ``None`` if the file doesn't exist
    (e.g., ingest ran via the real toolkit CLI without the conductor's
    adapter to stamp meta).
    """
    meta_path = case_path / "working" / "register_meta.json"
    if not meta_path.exists():
        return None
    return json.loads(meta_path.read_text())


def write_register_meta(case_path: Path, **fields: object) -> None:
    """Atomically write `working/register_meta.json` with conductor-owned
    metadata: ``upstream_hash``, ``schema_version``, ``producer_version``.

    Atomic = temp + os.replace + fsync. Idempotent: overwrites prior meta.
    """
    working = case_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    meta_path = working / "register_meta.json"
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".register_meta.", suffix=".json", dir=str(working))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(dict(fields), f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, meta_path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
