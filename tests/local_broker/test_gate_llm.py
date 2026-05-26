"""Tests for dsar_orchestrator.local_broker.gate_llm.

The broker call is mocked end-to-end; these tests prove the contract
(arg parsing, audit-log shape, decision validation, error cases) without
touching a real LLM.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest import mock

import pytest

from dsar_orchestrator.local_broker import gate_llm


def _fake_broker_response(decision: dict) -> dict:
    """Build an OpenAI-shaped chat-completion response wrapping decision."""
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(decision),
                    "reasoning": "synthetic reasoning",
                },
            }
        ],
    }


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    (tmp_path / "audit").mkdir()
    return tmp_path


def _patched_urlopen(payload: dict):
    """Return a fake urlopen context manager that yields ``payload`` as JSON."""

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            pass

        def read(self) -> bytes:
            return self._body

    body = json.dumps(payload).encode()

    def fake(req, timeout):  # noqa: ARG001 - matches urlopen signature
        return _Resp(body)

    return fake


def test_decide_appends_audit_record(case_dir: Path) -> None:
    cfg = gate_llm.GateLLMConfig(case_dir=case_dir)
    decision_payload = {"decision": "ACCEPT", "rationale": "OK", "confidence": 0.9}
    with (
        mock.patch.object(
            gate_llm.urllib.request,
            "urlopen",
            side_effect=lambda req, timeout: _patched_urlopen(
                _fake_broker_response(decision_payload)
            )(req, timeout),
        ),
        mock.patch.object(
            gate_llm.json, "load", lambda fh: _fake_broker_response(decision_payload)
        ),
    ):
        result = gate_llm.decide(cfg, "g1", ["ACCEPT", "HALT"], "ctx body")
    assert result == decision_payload
    assert cfg.audit_log.exists()
    row = json.loads(cfg.audit_log.read_text().strip())
    assert row["gate"] == "g1"
    assert row["options"] == ["ACCEPT", "HALT"]
    assert row["decision"] == decision_payload
    assert row["prompt"]["user"].startswith("Gate: g1\n")


def test_decide_rejects_unknown_decision(case_dir: Path) -> None:
    cfg = gate_llm.GateLLMConfig(case_dir=case_dir)
    bad = {"decision": "SOMETHING_ELSE", "rationale": "x", "confidence": 1.0}
    with (
        mock.patch.object(
            gate_llm.urllib.request,
            "urlopen",
            side_effect=lambda req, timeout: _patched_urlopen(_fake_broker_response(bad))(
                req, timeout
            ),
        ),
        mock.patch.object(gate_llm.json, "load", lambda fh: _fake_broker_response(bad)),
    ):
        with pytest.raises(RuntimeError, match="picked 'SOMETHING_ELSE'"):
            gate_llm.decide(cfg, "g1", ["ACCEPT", "HALT"], "ctx")
    assert not cfg.audit_log.exists()


def test_decide_rejects_non_json_content(case_dir: Path) -> None:
    cfg = gate_llm.GateLLMConfig(case_dir=case_dir)
    bad_response = {
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "not json"},
            }
        ],
    }
    with (
        mock.patch.object(
            gate_llm.urllib.request,
            "urlopen",
            side_effect=lambda req, timeout: _patched_urlopen(bad_response)(req, timeout),
        ),
        mock.patch.object(gate_llm.json, "load", lambda fh: bad_response),
    ):
        with pytest.raises(RuntimeError, match="not valid JSON"):
            gate_llm.decide(cfg, "g1", ["ACCEPT"], "ctx")


def test_decide_rejects_empty_content(case_dir: Path) -> None:
    cfg = gate_llm.GateLLMConfig(case_dir=case_dir)
    empty = {
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "length",
                "message": {"role": "assistant", "content": ""},
            }
        ],
    }
    with (
        mock.patch.object(
            gate_llm.urllib.request,
            "urlopen",
            side_effect=lambda req, timeout: _patched_urlopen(empty)(req, timeout),
        ),
        mock.patch.object(gate_llm.json, "load", lambda fh: empty),
    ):
        with pytest.raises(RuntimeError, match="no content"):
            gate_llm.decide(cfg, "g1", ["ACCEPT"], "ctx")


def test_main_returns_2_when_case_dir_missing(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("context"),
    )
    rc = gate_llm.main(["--case-dir", str(missing), "g1", "ACCEPT,HALT"])
    assert rc == 2


def test_main_returns_2_when_no_stdin(case_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = gate_llm.main(["--case-dir", str(case_dir), "g1", "ACCEPT,HALT"])
    assert rc == 2


def test_main_happy_path(case_dir: Path, monkeypatch, capsys) -> None:
    decision_payload = {"decision": "ACCEPT", "rationale": "OK", "confidence": 0.95}

    def fake_decide(cfg, gate, options, context):
        return decision_payload

    monkeypatch.setattr(gate_llm, "decide", fake_decide)
    monkeypatch.setattr("sys.stdin", io.StringIO("findings: 12"))
    rc = gate_llm.main(["--case-dir", str(case_dir), "stage2", "ACCEPT,REINGEST,HALT"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "ACCEPT"
    assert "rationale: OK" in out.err
    assert "confidence: 0.95" in out.err
