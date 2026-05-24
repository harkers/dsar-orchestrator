"""upstream_hash primitives — the chain that drives resume semantics.

Every artefact the pipeline writes carries an `upstream_hash` field
(SHA-256 of its upstream inputs). On read, downstream verifies. If the
recorded hash doesn't match the current upstream state, the read fails
loudly with a clear re-run instruction.

This module provides the primitives; per-artefact upstream definitions
live in each module's `core.<fn>()`. See orchestration spec v2 §
"Resume semantics — upstream_hash everywhere" + § "Concrete hash chain".
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from dsar_orchestrator.exceptions import UpstreamHashMismatch

# ─── register.json shape helpers (issue #8) ──────────────────────────
#
# Real toolkit writes `working/register.json` as a flat list of file-record
# dicts (one per source file). Conductor metadata (upstream_hash, schema/producer
# versions) lives in a sibling `working/register_meta.json` written by the
# conductor's ingest adapter — keeps the conductor's cascade machinery alive
# without mutating the toolkit's artefact.


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
    by embed + by upstream-hash computation in this module.
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


import contextlib  # noqa: E402 — kept near the function that uses it


def sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file's bytes. Streams in 64 KiB chunks
    so large files don't blow memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(64 * 1024):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def hash_pairs(items: Iterable[tuple[str, str]]) -> str:
    """Hash a collection of (label, sub-hash) pairs, sorted by label.

    Stable + commutative-in-input-order: callers can collect items in
    any order and still produce the same hash. Sort discipline lives
    here.
    """
    sorted_items = sorted(items, key=lambda x: x[0])
    h = hashlib.sha256()
    for label, sub in sorted_items:
        h.update(label.encode("utf-8"))
        h.update(b"\x1f")  # ASCII unit separator
        h.update(sub.encode("utf-8"))
        h.update(b"\x1e")  # ASCII record separator
    return h.hexdigest()


def compute_register_hash(register_path: Path) -> str:
    """Compute the canonical upstream hash for downstream artefacts that
    depend on `register.json` + the raw text per ref.

    Used by `dsar_embed` and `dsar_pii_discovery` (both consume raw text).
    Toolkit-shape: register is a flat list; extracted text lives at
    `working/<ref>.txt` (see :func:`text_path_for_ref`).
    """
    register = read_register(register_path)
    case_path = register_path.parent.parent  # working/ -> case/
    pairs: list[tuple[str, str]] = []
    for entry in register:
        ref = entry["ref"]
        text_path = text_path_for_ref(case_path, ref)
        if text_path.exists():
            pairs.append((ref, sha256_file(text_path)))
    return hash_pairs(pairs)


def read_recorded_hash(artefact_path: Path) -> str:
    """Extract the recorded `upstream_hash` from an artefact.

    Convention: every JSONL row carries `upstream_hash` as a field. We
    read the first row and trust it; rows in a single artefact share
    the same upstream hash (they all derive from the same upstream).

    For ``.jsonl`` files the convention is: the first JSON object on
    the first line carries the hash (every row should share the same
    value). For ``.json`` files (single-object, often pretty-printed
    across multiple lines) the whole file is parsed.
    """
    if not artefact_path.exists():
        raise FileNotFoundError(f"Artefact missing: {artefact_path}")

    if artefact_path.suffix == ".jsonl":
        with open(artefact_path) as f:
            first_line = f.readline().strip()
        if not first_line:
            raise UpstreamHashMismatch(
                f"Artefact {artefact_path} is empty; cannot read upstream_hash"
            )
        row = json.loads(first_line)
    else:
        # .json or unknown — parse the whole file as a single object.
        try:
            row = json.loads(artefact_path.read_text())
        except json.JSONDecodeError as e:
            raise UpstreamHashMismatch(f"Artefact {artefact_path} is not valid JSON: {e}") from e

    if "upstream_hash" not in row:
        raise UpstreamHashMismatch(
            f"Artefact {artefact_path} has no `upstream_hash` field. "
            f"Likely written by a non-orchestrator-aware tool. "
            f"Re-run the producing stage."
        )
    return row["upstream_hash"]


def verify_upstream(
    artefact_path: Path,
    current_upstream_hash: str,
    *,
    rerun_instruction: str | None = None,
) -> None:
    """Verify the artefact's recorded upstream_hash matches the current
    upstream state.

    Raises UpstreamHashMismatch with a clear instruction if not. The
    instruction defaults to a generic message; pass `rerun_instruction`
    to make it more specific to the artefact ("re-run dsar-embed
    --case X --if-exists overwrite", etc.).
    """
    recorded = read_recorded_hash(artefact_path)
    if recorded != current_upstream_hash:
        instr = rerun_instruction or (
            f"Re-run the stage that produces {artefact_path.name} "
            f"with --if-exists overwrite, or pass --from <upstream-stage> "
            f"to dsar-conductor."
        )
        raise UpstreamHashMismatch(
            f"Upstream changed since {artefact_path} was written.\n"
            f"  Recorded: {recorded[:16]}...\n"
            f"  Current:  {current_upstream_hash[:16]}...\n"
            f"  Fix: {instr}"
        )
