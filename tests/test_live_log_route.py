# tests/test_live_log_route.py
"""Integration-ish tests for the /live-log/stream SSE route.
Spawns ConsoleHandler in-process, connects with stdlib http.client,
asserts SSE frames.
"""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture()
def case_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "CASE-TEST"
    (cd / "working").mkdir(parents=True)
    return cd


@pytest.fixture()
def server(case_dir: Path):
    # case_dir reaches the handler via module-level _CFG.
    import dsar_orchestrator.operator_console as oc

    saved = getattr(oc, "_CFG", None)
    oc._CFG = oc.ServerConfig(
        case_dir=case_dir,
        orchestrator_cli="dsar-conductor",
        approver_bin=None,
        approver_input=Path("/tmp/approver_input.json"),
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), oc.ConsoleHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()
    oc._CFG = saved


def test_live_log_stream_serves_text_event_stream(case_dir, server):
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    conn.close()


def test_live_log_stream_emits_frame_for_appended_event(case_dir, server):
    port = server
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()

    def appender() -> None:
        time.sleep(0.1)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": "2026-05-29T10:42:11Z",
                        "event_type": "REDACT_COMPLETED",
                        "refs_processed": 1,
                        "redactions_applied": 1,
                    }
                )
                + "\n"
            )

    threading.Thread(target=appender, daemon=True).start()

    body = resp.read1(4096)
    conn.close()
    text = body.decode("utf-8", errors="replace")
    assert "event: live-log" in text
    assert "REDACT_COMPLETED" in text


def test_live_log_stream_malformed_last_event_id_falls_back_to_replay(case_dir, server):
    """Spec §6.13: a malformed Last-Event-ID MUST NOT 500. Server
    falls back to Phase A replay (returns 200)."""
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "GET",
        "/live-log/stream",
        headers={"Last-Event-ID": "garbage:not:numeric:here"},
    )
    resp = conn.getresponse()
    assert resp.status == 200
    conn.close()


def test_live_log_page_serves_html(case_dir, server):
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    assert resp.status == 200
    assert "<title>Live log" in body
    assert "EventSource" in body
    assert "/live-log/stream" in body


def test_case_header_nav_links_to_live_log(case_dir, server):
    port = server
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    # Either the nav strip on the landing page, or the page itself, links to /live-log.
    assert "/live-log" in body


def _read_until(resp, needle: str, *, max_reads: int = 40) -> str:
    """Read SSE chunks until `needle` appears or the stream stalls."""
    text = ""
    for _ in range(max_reads):
        chunk = resp.read1(4096)
        if not chunk:
            break
        text += chunk.decode("utf-8", errors="replace")
        if needle in text:
            break
    return text


def test_event_frame_carries_composite_cursor_id(case_dir, server):
    """The SSE `id:` for a real event is a composite cursor (§3.5)."""
    port = server
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()

    def appender() -> None:
        time.sleep(0.2)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": "2026-05-29T10:42:11Z",
                        "event_type": "REDACT_COMPLETED",
                        "refs_processed": 1,
                        "redactions_applied": 1,
                    }
                )
                + "\n"
            )

    threading.Thread(target=appender, daemon=True).start()

    text = _read_until(resp, "REDACT_COMPLETED")
    conn.close()
    # id line present and composite (audit + cond joined by |).
    id_lines = [ln for ln in text.splitlines() if ln.startswith("id: ")]
    assert id_lines, f"no id line in: {text!r}"
    assert "|" in id_lines[0], f"id not composite: {id_lines[0]}"
    assert "audit:" in id_lines[0]


def test_heartbeat_is_real_frame_with_cursor(case_dir, monkeypatch):
    """Spec §4.5: a heartbeat is a real SSE frame (event: live-log,
    data {kind: heartbeat}) carrying an id — NOT a bare comment. Also
    proves the heartbeat code path doesn't NameError (datetime/UTC)."""
    import dsar_orchestrator.operator_console as oc

    # Force a fast heartbeat so the test actually observes one.
    monkeypatch.setattr(oc, "_LIVE_LOG_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(oc, "_LIVE_LOG_POLL_INTERVAL", 0.02)

    saved = getattr(oc, "_CFG", None)
    oc._CFG = oc.ServerConfig(
        case_dir=case_dir,
        orchestrator_cli="dsar-conductor",
        approver_bin=None,
        approver_input=Path("/tmp/approver_input.json"),
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), oc.ConsoleHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        (case_dir / "working" / "audit_events.jsonl").touch()
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/live-log/stream")
        resp = conn.getresponse()
        text = _read_until(resp, '"kind":"heartbeat"')
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        oc._CFG = saved

    assert ":hb" not in text, "heartbeat must not be a bare SSE comment"
    assert '"kind":"heartbeat"' in text, f"no heartbeat frame: {text!r}"
    assert "event: live-log" in text
    hb_ids = [ln for ln in text.splitlines() if ln.startswith("id: ")]
    assert hb_ids, "heartbeat frame carries no id (composite cursor)"


def test_l1_pii_fields_never_reach_browser(case_dir, server):
    """An L1 event carrying PII in non-allowlisted fields must not leak
    to the SSE stream — the projection drops unknown fields (§3.2)."""
    port = server
    audit = case_dir / "working" / "audit_events.jsonl"
    audit.touch()
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/live-log/stream")
    resp = conn.getresponse()

    canary = "Jane-Smith-PII-canary-12345"

    def appender() -> None:
        time.sleep(0.2)
        with audit.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": "2026-05-29T10:42:11Z",
                        "event_type": "REDACT_COMPLETED",
                        "refs_processed": 1,
                        "redactions_applied": 1,
                        "subject_protected_phrases": [canary],
                        "rationale": canary,
                    }
                )
                + "\n"
            )

    threading.Thread(target=appender, daemon=True).start()

    text = _read_until(resp, "REDACT_COMPLETED")
    conn.close()
    assert "REDACT_COMPLETED" in text
    assert canary not in text, f"PII canary leaked to SSE: {text!r}"


def test_l3_note_message_never_reaches_browser(case_dir, monkeypatch, tmp_path):
    """End-to-end (§3.2, §7.4): a genuine L3 pipeline.jsonl `note` row
    whose free-text `message` carries PII must NOT reach the browser —
    the projection drops `message`. Exercises the real L3 source by
    redirecting its path into tmp."""
    import dsar_orchestrator.operator_console as oc
    import dsar_orchestrator.local_broker.live_log_stream as lls

    l3_path = tmp_path / "pipeline.jsonl"
    monkeypatch.setattr(lls, "_l3_pipeline_jsonl_path", lambda _case_dir: l3_path)

    saved = getattr(oc, "_CFG", None)
    oc._CFG = oc.ServerConfig(
        case_dir=case_dir,
        orchestrator_cli="dsar-conductor",
        approver_bin=None,
        approver_input=Path("/tmp/approver_input.json"),
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), oc.ConsoleHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    canary = "Jane-Smith-DoB-1985-03-14-canary"
    try:
        (case_dir / "working" / "audit_events.jsonl").touch()
        l3_path.touch()
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/live-log/stream")
        resp = conn.getresponse()

        def appender() -> None:
            time.sleep(0.2)
            with l3_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "event": "note",
                            "kind": "info",
                            "ts": "2026-05-29T10:42:11Z",
                            "message": f"data subject is {canary}",
                        }
                    )
                    + "\n"
                )

        threading.Thread(target=appender, daemon=True).start()

        text = _read_until(resp, '"kind":"event"')
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        oc._CFG = saved

    assert canary not in text, f"L3 note().message leaked PII to SSE: {text!r}"
