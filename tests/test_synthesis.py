"""Tests for the synthetic-case generator."""

from __future__ import annotations

import json
from pathlib import Path

from dsar_orchestrator.synthesis import synthesize_case


def test_synthesize_default_produces_exactly_100_docs(tmp_path: Path) -> None:
    result = synthesize_case("800100", tmp_path)
    source_dir = result.case_path / "source"
    docs = list(source_dir.glob("*.txt"))
    assert len(docs) == 100
    assert result.doc_count == 100


def test_synthesize_writes_case_config(tmp_path: Path) -> None:
    result = synthesize_case("800100", tmp_path)
    cfg_path = result.case_path / "case_config.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text())
    assert cfg["case_no"] == "800100"
    assert cfg["subject_identifier"]["primary_name"] == "James Carter"
    assert cfg["subject_identifier"]["employee_id"] == "FIN-0241"
    assert cfg["rerank_mode"] == "shadow"
    assert cfg["pii_classify_mode"] == "shadow"
    assert cfg["synthetic"] is True


def test_synthesize_writes_truth_file(tmp_path: Path) -> None:
    result = synthesize_case("800100", tmp_path)
    truth_path = result.case_path / "synthetic_truth.json"
    assert truth_path.exists()
    truth = json.loads(truth_path.read_text())
    assert truth["case_no"] == "800100"
    assert truth["doc_count"] == 100
    assert len(truth["rows"]) == 100
    # Every row has the four required fields
    for row in truth["rows"]:
        assert "ref" in row
        assert "category" in row
        assert "truth_class" in row
        assert "filename" in row


def test_synthesize_truth_class_distribution(tmp_path: Path) -> None:
    """The 100-doc mix should land at ~30 gold, ~12 mid, ~10 decoy,
    ~13 off_finance, ~35 off_topic. These are the exact counts from
    the DOC_MIX table — assert them precisely."""
    result = synthesize_case("800100", tmp_path)
    assert result.by_truth_class == {
        "gold": 30,
        "mid": 12,
        "decoy": 10,
        "off_finance": 13,
        "off_topic": 35,
    }


def test_synthesize_is_deterministic(tmp_path: Path) -> None:
    """Same case_no + seed → same source bytes."""
    a = synthesize_case("800200", tmp_path / "a")
    b = synthesize_case("800200", tmp_path / "b")
    assert a.by_truth_class == b.by_truth_class
    assert a.by_category == b.by_category
    # Spot-check that doc bytes match
    for ref in ("800200-0001", "800200-0042", "800200-0099"):
        a_bytes = (a.case_path / "source" / f"{ref}.txt").read_bytes()
        b_bytes = (b.case_path / "source" / f"{ref}.txt").read_bytes()
        assert a_bytes == b_bytes


def test_synthesize_different_case_no_yields_different_corpus(tmp_path: Path) -> None:
    """Different case numbers should produce different orderings (the
    document mix is the same but the shuffle differs)."""
    a = synthesize_case("800001", tmp_path / "a")
    b = synthesize_case("800002", tmp_path / "b")
    assert a.by_truth_class == b.by_truth_class  # mix is fixed
    # But the per-ref content should differ (different shuffle)
    a_first = (a.case_path / "source" / "800001-0001.txt").read_text()
    b_first = (b.case_path / "source" / "800002-0001.txt").read_text()
    assert a_first != b_first


def test_synthesize_explicit_seed_overrides(tmp_path: Path) -> None:
    """When seed is passed explicitly, two different case_nos with
    the same seed produce the same shuffle (the category for the
    Nth ref is identical)."""
    a = synthesize_case("CASE_A", tmp_path / "a", seed=42)
    b = synthesize_case("CASE_B", tmp_path / "b", seed=42)
    a_truth = json.loads((a.case_path / "synthetic_truth.json").read_text())
    b_truth = json.loads((b.case_path / "synthetic_truth.json").read_text())
    # Categories per ref position should match
    a_cats = [row["category"] for row in a_truth["rows"]]
    b_cats = [row["category"] for row in b_truth["rows"]]
    assert a_cats == b_cats


def test_synthesize_scales_to_smaller_doc_count(tmp_path: Path) -> None:
    result = synthesize_case("800300", tmp_path, doc_count=20)
    assert result.doc_count == 20
    docs = list((result.case_path / "source").glob("*.txt"))
    assert len(docs) == 20


def test_synthesize_creates_required_directories(tmp_path: Path) -> None:
    result = synthesize_case("800100", tmp_path)
    assert (result.case_path / "source").is_dir()
    assert (result.case_path / "working").is_dir()
    assert (result.case_path / "redacted").is_dir()
    assert (result.case_path / "output").is_dir()


def test_synthesize_doc_carries_subject_in_gold_class(tmp_path: Path) -> None:
    """Every gold-class doc should mention James Carter by name."""
    result = synthesize_case("800100", tmp_path)
    truth = json.loads((result.case_path / "synthetic_truth.json").read_text())
    gold_refs = [r["ref"] for r in truth["rows"] if r["truth_class"] == "gold"]
    assert len(gold_refs) == 30
    for ref in gold_refs:
        body = (result.case_path / "source" / f"{ref}.txt").read_text()
        # Either the full name OR the first name + employee ID pattern
        has_subject = (
            "James Carter" in body
            or "james.carter@acme.test" in body
            or ("James" in body and "FIN-0241" in body)
        )
        assert has_subject, f"gold doc {ref} doesn't mention the subject:\n{body}"


def test_synthesize_decoy_docs_mention_other_james_not_subject(tmp_path: Path) -> None:
    """Decoy docs should mention James Marshall/Lee/Smith but NOT
    James Carter as the subject."""
    result = synthesize_case("800100", tmp_path)
    truth = json.loads((result.case_path / "synthetic_truth.json").read_text())
    decoy_refs = [r["ref"] for r in truth["rows"] if r["truth_class"] == "decoy"]
    for ref in decoy_refs:
        body = (result.case_path / "source" / f"{ref}.txt").read_text()
        # Must mention a decoy James — by full name or by email handle
        mentions_decoy = (
            "James Marshall" in body
            or "James Lee" in body
            or "James Smith" in body
            or "j.marshall@" in body
            or "james.lee@" in body
            or "j.smith@" in body
        )
        # Must NOT mention the subject explicitly
        mentions_subject = (
            "James Carter" in body or "james.carter@acme.test" in body or "FIN-0241" in body
        )
        assert mentions_decoy, f"decoy doc {ref} doesn't mention a decoy James:\n{body}"
        assert not mentions_subject, (
            f"decoy doc {ref} mentions the subject — that defeats the purpose"
        )


def test_synthesize_off_topic_docs_dont_mention_subject(tmp_path: Path) -> None:
    """Off-topic docs (building, IT, cafeteria, vendor, holiday) should
    contain zero subject references — they're the noise tier."""
    result = synthesize_case("800100", tmp_path)
    truth = json.loads((result.case_path / "synthetic_truth.json").read_text())
    off_refs = [r["ref"] for r in truth["rows"] if r["truth_class"] == "off_topic"]
    assert len(off_refs) == 35
    for ref in off_refs:
        body = (result.case_path / "source" / f"{ref}.txt").read_text()
        assert "James Carter" not in body, f"off_topic doc {ref} mentions subject"
        assert "FIN-0241" not in body, f"off_topic doc {ref} mentions subject ID"


def test_subject_identifier_aliases_present(tmp_path: Path) -> None:
    """case_config.subject_identifier should include aliases for the
    decoy-disambiguation contract."""
    result = synthesize_case("800100", tmp_path)
    cfg = json.loads((result.case_path / "case_config.json").read_text())
    si = cfg["subject_identifier"]
    assert "J. Carter" in si["aliases"]
    assert "Jim Carter" in si["aliases"]
    # disambiguation_notes should mention the decoys for the LLM PII classifier
    assert "James Marshall" in si["disambiguation_notes"]
