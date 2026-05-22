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
from collections.abc import Iterable
from pathlib import Path

from dsar_orchestrator.exceptions import UpstreamHashMismatch


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
    """
    with open(register_path) as f:
        register = json.load(f)
    case_root = register_path.parent.parent  # working/ -> case/
    pairs: list[tuple[str, str]] = []
    for entry in register.get("refs", []):
        ref = entry["ref"]
        text_path = case_root / entry["text_path"]
        pairs.append((ref, sha256_file(text_path)))
    return hash_pairs(pairs)


def read_recorded_hash(artefact_path: Path) -> str:
    """Extract the recorded `upstream_hash` from an artefact.

    Convention: every JSONL row carries `upstream_hash` as a field. We
    read the first row and trust it; rows in a single artefact share
    the same upstream hash (they all derive from the same upstream).

    For single-row artefacts or header-only artefacts, the convention
    is identical — the first JSON object on the first line.
    """
    if not artefact_path.exists():
        raise FileNotFoundError(f"Artefact missing: {artefact_path}")
    with open(artefact_path) as f:
        first_line = f.readline().strip()
    if not first_line:
        raise UpstreamHashMismatch(f"Artefact {artefact_path} is empty; cannot read upstream_hash")
    row = json.loads(first_line)
    if "upstream_hash" not in row:
        raise UpstreamHashMismatch(
            f"Artefact {artefact_path} has no `upstream_hash` field on its "
            f"first row. Likely written by a non-orchestrator-aware tool. "
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
            f"to dsar-pipeline."
        )
        raise UpstreamHashMismatch(
            f"Upstream changed since {artefact_path} was written.\n"
            f"  Recorded: {recorded[:16]}...\n"
            f"  Current:  {current_upstream_hash[:16]}...\n"
            f"  Fix: {instr}"
        )
