"""Tests for the /people-register operator-console data layer."""

from __future__ import annotations

import json
from pathlib import Path

from dsar_orchestrator.local_broker.people_register_console import (
    load_people_register,
    rank_clusters,
    ranking_score,
    select_subject_referent_candidates,
    select_top_n,
)


def _seed_case(tmp_path: Path, clusters: list[dict]) -> Path:
    working = tmp_path / "working"
    working.mkdir(parents=True, exist_ok=True)
    (working / "people_register.json").write_text(json.dumps(clusters))
    return tmp_path


def _cluster(
    *,
    name: str,
    mention_count: int = 1,
    distinct_doc_count: int = 1,
    is_subject_confidence: float = 0.0,
    subject_centricity_score: float = 0.0,
    is_data_subject: bool = False,
    **extra,
) -> dict:
    """Cluster factory with the schema fields the console needs."""
    return {
        "canonical_name": name,
        "emails": [],
        "phones": [],
        "titles": [],
        "source_refs": [f"r-{name}"],
        "correlation_ids": [],
        "mention_count": mention_count,
        "distinct_doc_count": distinct_doc_count,
        "confidence_score": 1.0,
        "discovered_by": "exchange_flat",
        "is_data_subject": is_data_subject,
        "is_subject_confidence": is_subject_confidence,
        "subject_centricity_score": subject_centricity_score,
        "text_quality_summary": "unknown",
        **extra,
    }


# ---- load ----


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    """Missing working/people_register.json -> empty list, no exception."""
    assert load_people_register(tmp_path) == []


def test_load_existing_returns_list(tmp_path: Path) -> None:
    expected = [_cluster(name="Alice")]
    _seed_case(tmp_path, expected)
    loaded = load_people_register(tmp_path)
    assert loaded == expected


def test_load_garbage_returns_empty(tmp_path: Path) -> None:
    """Corrupted JSON file -> empty list (operator gets empty UI, can re-run
    sig_block_discovery or build_people_register)."""
    working = tmp_path / "working"
    working.mkdir(parents=True)
    (working / "people_register.json").write_text("not valid json")
    assert load_people_register(tmp_path) == []


# ---- ranking_score ----


def test_ranking_score_formula() -> None:
    """ranking_score = mention_count * distinct_doc_count * (1 - is_subject_confidence)."""
    c = _cluster(name="Alice", mention_count=10, distinct_doc_count=3, is_subject_confidence=0.2)
    assert ranking_score(c) == 10 * 3 * (1 - 0.2)


def test_ranking_score_subject_drops_to_zero() -> None:
    """A confident subject cluster has is_subject_confidence=1.0 → score 0."""
    c = _cluster(
        name="Subject A",
        mention_count=99,
        distinct_doc_count=99,
        is_subject_confidence=1.0,
    )
    assert ranking_score(c) == 0.0


def test_ranking_score_handles_missing_fields() -> None:
    """Defensive: missing fields default sensibly (0)."""
    score = ranking_score({"canonical_name": "X"})
    assert score == 0.0


# ---- rank_clusters ----


def test_rank_clusters_descending() -> None:
    clusters = [
        _cluster(name="Low", mention_count=1),
        _cluster(name="High", mention_count=10),
        _cluster(name="Mid", mention_count=5),
    ]
    ranked = rank_clusters(clusters)
    assert [c["canonical_name"] for c in ranked] == ["High", "Mid", "Low"]


def test_rank_clusters_stable_on_ties() -> None:
    """Tie-break: alphabetical canonical_name."""
    clusters = [
        _cluster(name="Zebra", mention_count=5),
        _cluster(name="Alpha", mention_count=5),
        _cluster(name="Mango", mention_count=5),
    ]
    ranked = rank_clusters(clusters)
    assert [c["canonical_name"] for c in ranked] == ["Alpha", "Mango", "Zebra"]


# ---- select_top_n ----


def test_select_top_n_default_50() -> None:
    clusters = [_cluster(name=f"C{i:03d}", mention_count=100 - i) for i in range(75)]
    top = select_top_n(clusters)
    assert len(top) == 50
    assert top[0]["canonical_name"] == "C000"  # highest mention_count


def test_select_top_n_custom_limit() -> None:
    clusters = [_cluster(name=f"C{i}", mention_count=100 - i) for i in range(20)]
    top = select_top_n(clusters, n=5)
    assert len(top) == 5


def test_select_top_n_filters_subject_clusters() -> None:
    """The data subject cluster(s) are excluded from the top-N third-party list."""
    clusters = [
        _cluster(name="Subject A", is_data_subject=True, mention_count=99),
        _cluster(name="Alice", mention_count=10),
    ]
    top = select_top_n(clusters)
    names = {c["canonical_name"] for c in top}
    assert "Subject A" not in names
    assert "Alice" in names


# ---- select_subject_referent_candidates ----


def test_subject_referent_candidates_threshold() -> None:
    """Only clusters with subject_centricity_score > 0.7 surface."""
    clusters = [
        _cluster(name="C-low", subject_centricity_score=0.5),
        _cluster(name="C-just-over", subject_centricity_score=0.71),
        _cluster(name="C-high", subject_centricity_score=0.9),
        _cluster(name="C-edge", subject_centricity_score=0.7),  # NOT included (strict >)
    ]
    out = select_subject_referent_candidates(clusters)
    names = {c["canonical_name"] for c in out}
    assert names == {"C-just-over", "C-high"}


def test_subject_referent_candidates_excludes_subjects() -> None:
    """A data-subject cluster never surfaces as a referent candidate (it IS the subject)."""
    clusters = [
        _cluster(name="Subject A", is_data_subject=True, subject_centricity_score=1.0),
        _cluster(name="Alice", subject_centricity_score=0.9),
    ]
    names = {c["canonical_name"] for c in select_subject_referent_candidates(clusters)}
    assert "Subject A" not in names
    assert "Alice" in names


def test_subject_referent_candidates_custom_threshold() -> None:
    clusters = [_cluster(name="X", subject_centricity_score=0.5)]
    out = select_subject_referent_candidates(clusters, threshold=0.3)
    assert len(out) == 1


# ---- HTTP smoke test ----


def test_render_people_register_route_returns_200(tmp_path: Path) -> None:
    """The operator-console GET /people-register route returns 200 and
    includes the top-50 cluster names. This is a smoke test — we don't
    fully boot the HTTPServer, just call render_people_register directly."""
    from dsar_orchestrator.operator_console import (
        CaseContext,
        render_people_register,
    )

    _seed_case(
        tmp_path,
        [
            _cluster(name="Alice Other", mention_count=10),
            _cluster(name="Bob Person", mention_count=5),
            _cluster(name="Charlie Site", mention_count=3),
        ],
    )
    # Also need a minimal case-metadata file so load_case_metadata doesn't crash
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com"})
    )

    ctx = CaseContext(case_dir=tmp_path)
    html_out = render_people_register(ctx, action_result=None)
    assert "<title>" in html_out.lower()
    assert "Alice Other" in html_out
    assert "Bob Person" in html_out
    assert "Charlie Site" in html_out
    assert "people-register" in html_out.lower() or "People register" in html_out


def test_render_people_register_shows_referent_candidates_section(
    tmp_path: Path,
) -> None:
    """A cluster with subject_centricity_score > 0.7 renders under the
    REVIEW PRIORITY heading."""
    from dsar_orchestrator.operator_console import (
        CaseContext,
        render_people_register,
    )

    _seed_case(
        tmp_path,
        [
            _cluster(name="Alice (Subject's GP)", subject_centricity_score=0.85, mention_count=3),
            _cluster(name="Bob (unrelated)", subject_centricity_score=0.1, mention_count=10),
        ],
    )
    (tmp_path / "working" / "data_subject.json").write_text(
        json.dumps({"full_name": "Subject A", "email": "s@x.com"})
    )
    ctx = CaseContext(case_dir=tmp_path)
    html_out = render_people_register(ctx, action_result=None)
    # The referent candidate appears in the priority section
    assert "Alice (Subject" in html_out
    # The heading explicitly calls out the priority section
    assert (
        "subject_referent" in html_out.lower()
        or "subject referent" in html_out.lower()
        or "review priority" in html_out.lower()
    )
