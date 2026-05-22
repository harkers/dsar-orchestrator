"""Tests for the upstream_hash primitives.

Validates stability + sort-order + mismatch surface area.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsar_orchestrator.exceptions import UpstreamHashMismatch
from dsar_orchestrator.hash_chain import (
    hash_pairs,
    read_recorded_hash,
    sha256_bytes,
    sha256_file,
    sha256_text,
    verify_upstream,
)


def test_sha256_bytes_is_stable() -> None:
    assert sha256_bytes(b"hello") == sha256_bytes(b"hello")
    assert sha256_bytes(b"hello") != sha256_bytes(b"world")


def test_sha256_text_matches_utf8_bytes() -> None:
    assert sha256_text("café") == sha256_bytes("café".encode())


def test_sha256_file_streams_correctly(tmp_path: Path) -> None:
    p = tmp_path / "doc.txt"
    p.write_text("the quick brown fox")
    assert sha256_file(p) == sha256_text("the quick brown fox")


def test_sha256_file_handles_large_input(tmp_path: Path) -> None:
    # 200 KiB — larger than the 64 KiB chunk size
    p = tmp_path / "large.bin"
    p.write_bytes(b"a" * (200 * 1024))
    h1 = sha256_file(p)
    h2 = sha256_bytes(b"a" * (200 * 1024))
    assert h1 == h2


def test_hash_pairs_is_order_independent() -> None:
    a = [("ref-1", "deadbeef"), ("ref-2", "cafebabe"), ("ref-3", "12345678")]
    b = [("ref-3", "12345678"), ("ref-1", "deadbeef"), ("ref-2", "cafebabe")]
    assert hash_pairs(a) == hash_pairs(b)


def test_hash_pairs_distinguishes_content() -> None:
    a = [("ref-1", "deadbeef"), ("ref-2", "cafebabe")]
    b = [("ref-1", "deadbeef"), ("ref-2", "different")]
    assert hash_pairs(a) != hash_pairs(b)


def test_hash_pairs_distinguishes_labels() -> None:
    a = [("ref-1", "deadbeef")]
    b = [("ref-1-extra", "deadbeef")]
    assert hash_pairs(a) != hash_pairs(b)


def test_read_recorded_hash_extracts_first_row(tmp_path: Path) -> None:
    p = tmp_path / "artefact.jsonl"
    rows = [
        {"upstream_hash": "abc123", "ref": "0001", "data": "..."},
        {"upstream_hash": "abc123", "ref": "0002", "data": "..."},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert read_recorded_hash(p) == "abc123"


def test_read_recorded_hash_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_recorded_hash(tmp_path / "nope.jsonl")


def test_read_recorded_hash_raises_on_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    with pytest.raises(UpstreamHashMismatch, match="empty"):
        read_recorded_hash(p)


def test_read_recorded_hash_raises_on_missing_field(tmp_path: Path) -> None:
    p = tmp_path / "no-hash.jsonl"
    p.write_text(json.dumps({"ref": "0001", "data": "..."}) + "\n")
    with pytest.raises(UpstreamHashMismatch, match="no `upstream_hash`"):
        read_recorded_hash(p)


def test_verify_upstream_passes_when_match(tmp_path: Path) -> None:
    p = tmp_path / "ok.jsonl"
    p.write_text(json.dumps({"upstream_hash": "abc123"}) + "\n")
    verify_upstream(p, "abc123")  # no raise


def test_verify_upstream_raises_with_instruction_when_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "stale.jsonl"
    p.write_text(json.dumps({"upstream_hash": "abc123"}) + "\n")
    with pytest.raises(UpstreamHashMismatch, match="Upstream changed"):
        verify_upstream(p, "different_hash")


def test_verify_upstream_includes_custom_instruction(tmp_path: Path) -> None:
    p = tmp_path / "stale.jsonl"
    p.write_text(json.dumps({"upstream_hash": "abc123"}) + "\n")
    with pytest.raises(UpstreamHashMismatch, match="dsar-embed --case X"):
        verify_upstream(p, "different", rerun_instruction="Re-run dsar-embed --case X")
