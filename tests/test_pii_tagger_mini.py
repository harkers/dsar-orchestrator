"""Tests for the dsar-pii-tagger-mini CLI promotion (#111 sub-5).

Heavy on pure-function coverage since the safety semantics
(protected-phrase precedence, subject-id preservation, regex-layer
defaults) are load-bearing on real client data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.needs_toolkit


# --- Subject ID + protected-phrase derivation ---


def test_subject_identifier_set_includes_name_tokens_and_emails() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _subject_identifier_set

    ids = _subject_identifier_set(
        {
            "full_name": "Jane Mary Doe",
            "aliases": ["J. Doe", "Jane M."],
            "email": "Jane@Example.com",
            "additional_emails": ["jane.doe@elsewhere.com"],
        }
    )
    # Lowercased name + tokens > 2 chars
    assert "jane mary doe" in ids
    assert "jane" in ids
    assert "mary" in ids
    assert "doe" in ids
    # Aliases lowercased + tokens
    assert "j. doe" in ids
    # Emails lowercased
    assert "jane@example.com" in ids
    assert "jane.doe@elsewhere.com" in ids


def test_subject_identifier_skips_short_tokens() -> None:
    """Tokens <= 2 chars shouldn't be in the never-redact set (would
    flag every 'is', 'on', 'at' as the subject)."""
    from dsar_orchestrator.local_broker.pii_tagger_mini import _subject_identifier_set

    ids = _subject_identifier_set({"full_name": "Al Bo Co"})
    assert "al" not in ids
    assert "bo" not in ids
    assert "co" not in ids
    assert "al bo co" in ids  # full name does get in


def test_protected_phrases_lowercased() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _protected_phrases_set

    p = _protected_phrases_set({"subject_protected_phrases": ["My Org Name", "ProjectAlpha"]})
    assert p == {"my org name", "projectalpha"}


# --- _find_all_spans ---


def test_find_all_spans_case_insensitive() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _find_all_spans

    text = "Jane wrote to Jane and jane again"
    spans = _find_all_spans(text, "Jane")
    assert spans == [(0, 4), (14, 18), (23, 27)]


def test_find_all_spans_non_overlapping() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _find_all_spans

    # "aaaa" with needle "aa" → (0,2) and (2,4), not (0,2),(1,3),(2,4)
    spans = _find_all_spans("aaaa", "aa")
    assert spans == [(0, 2), (2, 4)]


def test_find_all_spans_empty_needle_returns_empty() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _find_all_spans

    assert _find_all_spans("text", "") == []


# --- _regex_layer ---


def test_regex_layer_detects_email_phone_nino() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _regex_layer

    text = "Contact: jane@example.com, phone 07700 900123, NI: AB123456C."
    ents = _regex_layer(text)
    by_type = {e["type"]: e for e in ents}
    assert "email" in by_type
    assert by_type["email"]["text"] == "jane@example.com"
    assert "phone_uk" in by_type
    assert "nino" in by_type
    assert by_type["nino"]["text"] == "AB123456C"
    # All carry start/end + source='regex'
    for e in ents:
        assert e["source"] == "regex"
        assert 0 <= e["start"] < e["end"] <= len(text)


# --- _classify_entity (precedence) ---


def test_classify_protected_phrase_wins_over_llm() -> None:
    """Even if the LLM says third_party, a protected phrase preserves."""
    from dsar_orchestrator.local_broker.pii_tagger_mini import _classify_entity

    classification, redact = _classify_entity(
        "Project Alpha",
        llm_classification="third_party",
        subject_ids=set(),
        protected_phrases={"project alpha"},
    )
    assert classification == "data_subject"
    assert redact is False


def test_classify_subject_id_wins_over_llm() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _classify_entity

    classification, redact = _classify_entity(
        "Jane Doe",
        llm_classification="third_party",
        subject_ids={"jane doe"},
        protected_phrases=set(),
    )
    assert classification == "data_subject"
    assert redact is False


def test_classify_subject_id_substring_match() -> None:
    """Token-level: 'Jane' should match a subject_ids of {'jane', 'doe'}."""
    from dsar_orchestrator.local_broker.pii_tagger_mini import _classify_entity

    classification, _ = _classify_entity(
        "Jane",
        llm_classification=None,
        subject_ids={"jane", "doe"},
        protected_phrases=set(),
    )
    assert classification == "data_subject"


def test_classify_llm_third_party_redacts() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _classify_entity

    classification, redact = _classify_entity(
        "Bob Smith",
        llm_classification="third_party",
        subject_ids=set(),
        protected_phrases=set(),
    )
    assert classification == "third_party"
    assert redact is True


def test_classify_llm_organisation_flags() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _classify_entity

    classification, redact = _classify_entity(
        "Acme Corp",
        llm_classification="organisation",
        subject_ids=set(),
        protected_phrases=set(),
    )
    assert classification == "organisation"
    assert redact == "flag"


def test_classify_unknown_flags() -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import _classify_entity

    classification, redact = _classify_entity(
        "something",
        llm_classification="uncertain",
        subject_ids=set(),
        protected_phrases=set(),
    )
    assert classification == "unknown"
    assert redact == "flag"


# --- build_tags_for_doc (integration with broker monkeypatched) ---


def test_build_tags_combines_llm_and_regex_layers(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import pii_tagger_mini

    text = "Bob Smith wrote to jane@example.com. Contact NI: AB123456C."

    def fake_post(*_a, **_k):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "entities": [
                                    {
                                        "text": "Bob Smith",
                                        "type": "person",
                                        "classification": "third_party",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(pii_tagger_mini, "_post", fake_post)
    subject = {
        "full_name": "Jane Doe",
        "email": "jane@example.com",
    }
    tags = pii_tagger_mini.build_tags_for_doc(
        ref="doc-001", filename="doc.eml", text=text, subject=subject
    )
    by_text = {e["text"]: e for e in tags["entities"]}
    # Bob Smith → LLM third_party → redact True
    assert "Bob Smith" in by_text
    assert by_text["Bob Smith"]["redact"] is True
    # jane@example.com → regex layer, matches subject email → preserve
    assert "jane@example.com" in by_text
    assert by_text["jane@example.com"]["redact"] is False
    assert by_text["jane@example.com"]["classification"] == "data_subject"
    # NI number → regex layer, no subject match → redact True
    assert "AB123456C" in by_text
    assert by_text["AB123456C"]["redact"] is True
    # Summary counts consistent with entities
    assert tags["entity_count"] == len(tags["entities"])
    assert tags["redact_count"] == sum(1 for e in tags["entities"] if e["redact"] is True)


def test_build_tags_drops_short_llm_entities(monkeypatch) -> None:
    from dsar_orchestrator.local_broker import pii_tagger_mini

    def fake_post(*_a, **_k):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "entities": [
                                    {
                                        "text": "a",
                                        "type": "person",
                                        "classification": "third_party",
                                    },
                                    {
                                        "text": "Bob",
                                        "type": "person",
                                        "classification": "third_party",
                                    },
                                ]
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(pii_tagger_mini, "_post", fake_post)
    tags = pii_tagger_mini.build_tags_for_doc(
        ref="doc-001", filename="doc.eml", text="Bob and a are here", subject={"full_name": "Jane"}
    )
    refs = {e["text"] for e in tags["entities"]}
    assert "a" not in refs
    assert "Bob" in refs


def test_build_tags_dedupes_overlapping_spans(monkeypatch) -> None:
    """Same text occurring twice should produce two distinct spans, not duped."""
    from dsar_orchestrator.local_broker import pii_tagger_mini

    def fake_post(*_a, **_k):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "entities": [
                                    {
                                        "text": "Bob",
                                        "type": "person",
                                        "classification": "third_party",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(pii_tagger_mini, "_post", fake_post)
    tags = pii_tagger_mini.build_tags_for_doc(
        ref="doc-001",
        filename="doc.eml",
        text="Bob met Bob at Bob's place",
        subject={"full_name": "Jane"},
    )
    bobs = [e for e in tags["entities"] if e["text"] == "Bob"]
    # Three distinct spans
    assert len(bobs) == 3
    spans = {(e["start"], e["end"]) for e in bobs}
    assert len(spans) == 3


# --- run() end-to-end ---


def _seed_minimal_case(case_root: Path, *, refs: list[str]) -> None:
    working = case_root / "working"
    working.mkdir(exist_ok=True)
    (working / "data_subject.json").write_text(
        json.dumps({"full_name": "Jane Doe", "email": "jane@example.com"})
    )
    register = []
    for ref in refs:
        text_file = working / f"{ref}.txt"
        text_file.write_text("Bob said hi to jane@example.com", encoding="utf-8")
        register.append(
            {
                "ref": ref,
                "filename": f"{ref}.eml",
                "path": f"/src/{ref}.eml",
                "text_file": str(text_file),
            }
        )
    (working / "register.json").write_text(json.dumps(register))
    # responsiveness: every ref included
    with (working / "responsiveness_decisions.jsonl").open("w") as f:
        for ref in refs:
            f.write(json.dumps({"doc_ref": ref, "disposition": "included"}) + "\n")
    # scope_verdicts: every ref present
    with (working / "scope_verdicts.jsonl").open("w") as f:
        for ref in refs:
            f.write(json.dumps({"doc_ref": ref, "scope_verdict": "present"}) + "\n")


def test_run_processes_included_writes_tag_files(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import pii_tagger_mini

    case_root = tmp_path / "case"
    case_root.mkdir()
    (case_root / "audit").mkdir()
    _seed_minimal_case(case_root, refs=["doc-001", "doc-002"])

    def fake_post(*_a, **_k):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "entities": [
                                    {
                                        "text": "Bob",
                                        "type": "person",
                                        "classification": "third_party",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(pii_tagger_mini, "_post", fake_post)
    rc = pii_tagger_mini.run(case_root)
    assert rc == 0
    for ref in ("doc-001", "doc-002"):
        tag_path = case_root / "working" / f"{ref}_tags.json"
        assert tag_path.exists()
        tags = json.loads(tag_path.read_text())
        assert tags["ref"] == ref


def test_run_skips_already_done(tmp_path: Path, monkeypatch) -> None:
    from dsar_orchestrator.local_broker import pii_tagger_mini

    case_root = tmp_path / "case"
    case_root.mkdir()
    (case_root / "audit").mkdir()
    _seed_minimal_case(case_root, refs=["doc-001"])
    # Pre-populate the tag file
    existing = {"ref": "doc-001", "prior": True}
    (case_root / "working" / "doc-001_tags.json").write_text(json.dumps(existing))

    call_count = {"n": 0}

    def fake_post(*_a, **_k):
        call_count["n"] += 1
        return {"choices": [{"message": {"content": '{"entities": []}'}}]}

    monkeypatch.setattr(pii_tagger_mini, "_post", fake_post)
    pii_tagger_mini.run(case_root)
    assert call_count["n"] == 0, "broker called for already-done doc"
    assert json.loads((case_root / "working" / "doc-001_tags.json").read_text()) == existing


def test_run_returns_1_on_missing_inputs(tmp_path: Path) -> None:
    from dsar_orchestrator.local_broker.pii_tagger_mini import run

    case_root = tmp_path / "empty"
    case_root.mkdir()
    (case_root / "working").mkdir()
    assert run(case_root) == 1
