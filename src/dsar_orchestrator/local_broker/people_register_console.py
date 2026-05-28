"""Data-layer helpers for the /people-register operator console route.

Mirrors the flag_review.py pattern: pure-Python, no HTTP, returns
sorted/filtered cluster lists for the operator_console renderer.

Per spec §1.5:
  - top-50 view ranked by mention_count * distinct_doc_count * (1 - is_subject_confidence)
  - separate REVIEW PRIORITY subject_referent_candidate section for
    clusters with subject_centricity_score > 0.7 (advisory threshold)
  - data-subject clusters are excluded from both views (they are the subject,
    not the redaction target)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_TOP_N = 50
DEFAULT_REFERENT_THRESHOLD = 0.7


def cluster_id(cluster: dict) -> str:
    """Stable 16-char hex ID derived from canonical_name (sha1[:16]).
    Used to round-trip cluster identity through HTML forms without
    modifying people_register.json on the GET path (Option A)."""
    name = str(cluster.get("canonical_name") or "")
    return hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def load_people_register(case_dir: Path) -> list[dict[str, Any]]:
    """Return the cluster list from working/people_register.json, or []
    if missing / unreadable / non-JSON."""
    p = case_dir / "working" / "people_register.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return data


def ranking_score(cluster: dict[str, Any]) -> float:
    """Spec §1.5: mention_count * distinct_doc_count * (1 - is_subject_confidence)."""
    m = cluster.get("mention_count", 0)
    d = cluster.get("distinct_doc_count", 0)
    s = cluster.get("is_subject_confidence", 0.0)
    try:
        return float(m) * float(d) * (1.0 - float(s))
    except (TypeError, ValueError):
        return 0.0


def rank_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return clusters sorted by (ranking_score DESC, canonical_name ASC)."""
    return sorted(
        clusters,
        key=lambda c: (-ranking_score(c), str(c.get("canonical_name") or "")),
    )


def select_top_n(clusters: list[dict[str, Any]], n: int = DEFAULT_TOP_N) -> list[dict[str, Any]]:
    """Top-N third-party clusters by ranking_score. Data-subject clusters
    are filtered out (they belong on the subject side, not the denylist)."""
    third_parties = [c for c in clusters if not c.get("is_data_subject")]
    return rank_clusters(third_parties)[:n]


def select_subject_referent_candidates(
    clusters: list[dict[str, Any]], threshold: float = DEFAULT_REFERENT_THRESHOLD
) -> list[dict[str, Any]]:
    """Clusters with subject_centricity_score > threshold (advisory).
    Excludes data-subject clusters themselves. Sorted by score DESC."""
    out = [
        c
        for c in clusters
        if not c.get("is_data_subject")
        and float(c.get("subject_centricity_score") or 0.0) > threshold
    ]
    return sorted(
        out,
        key=lambda c: (
            -float(c.get("subject_centricity_score") or 0.0),
            str(c.get("canonical_name") or ""),
        ),
    )
