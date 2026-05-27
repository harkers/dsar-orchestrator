"""#110 — Hard-Blocker Waiver workflow.

A console-side waiver flow that is separate from the DSAR Approver's four
verdicts (which are issued by the auditor agent). The Approver verdict says
*what the auditor thinks*; the waiver records *that the operator accepts
residual risk and a DPO has co-signed off on it*. Both land in the audit
chain so a downstream regulator-review can reconstruct the decision.

Two events per waiver:
1. **Propose** (operator-initiated) — appends a ``state=pending`` row to
   ``<case>/audit/waivers.jsonl`` and emits a ``REVIEWER_DECISION_MADE``
   chain event with stage ``waiver_propose``.
2. **Co-sign** (DPO-initiated) — appends a ``state=co_signed`` row and
   emits a second chain event with stage ``waiver_cosign`` containing the
   full payload (operator id, DPO id, justification, DPO note, original
   propose-event hash). The cosign event is the tamper-evident anchor.

JSONL is latest-wins on `waiver_id`. The chain is the integrity layer; the
JSONL is the user-visible decision log. Both writes follow the chain-first
pattern (PR #104) with the compensating ``FAILURE_RECORDED`` fallback
(PR #114) on JSONL append failure.

Auth: ``DSAR_DPO_TOKEN`` env var. When set, the DPO cosign route requires
``Authorization: Bearer <token>``. When unset, single-operator mode and
the gate is open. Proportionate for a local single-operator console.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dsar_orchestrator.operator_console import CaseContext

log = logging.getLogger("waiver")

_WAIVER_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _waiver_id() -> str:
    return "WV-" + secrets.token_hex(6)


def _waivers_path(ctx: CaseContext) -> Path:
    return ctx.case_dir / "audit" / "waivers.jsonl"


def _append_waiver(ctx: CaseContext, row: dict) -> None:
    path = _waivers_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WAIVER_LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_waivers_raw(ctx: CaseContext) -> list[dict]:
    path = _waivers_path(ctx)
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def list_all_waivers(ctx: CaseContext) -> list[dict]:
    """Return one row per waiver_id (latest-wins). Stable order by
    proposed_ts ascending."""
    latest: dict[str, dict] = {}
    for r in _read_waivers_raw(ctx):
        wid = r.get("waiver_id")
        if not wid:
            continue
        latest[wid] = r
    return sorted(latest.values(), key=lambda r: r.get("proposed_ts", ""))


def list_pending_waivers(ctx: CaseContext) -> list[dict]:
    return [r for r in list_all_waivers(ctx) if r.get("state") == "pending"]


def load_waiver(ctx: CaseContext, waiver_id: str) -> dict | None:
    for r in list_all_waivers(ctx):
        if r["waiver_id"] == waiver_id:
            return r
    return None


def propose_waiver(
    ctx: CaseContext,
    *,
    blocker_ids: list[str],
    justification: str,
    operator_id: str,
) -> dict:
    """Operator proposes a waiver covering one or more CRITICAL/HIGH
    blockers. Records a ``state=pending`` row + chain event; DPO must
    co-sign to finalise."""
    if not blocker_ids:
        raise ValueError("blocker_ids must not be empty")
    if not (justification or "").strip():
        raise ValueError("justification must not be empty")
    if not (operator_id or "").strip():
        raise ValueError("operator_id must not be empty")

    waiver_id = _waiver_id()
    proposed_ts = _iso_now()
    payload = {
        "ts": proposed_ts,
        "waiver": True,
        "action": "propose",
        "waiver_id": waiver_id,
        "blocker_ids": list(blocker_ids),
        "operator_id": operator_id,
        "justification": justification,
    }

    from dsar_orchestrator.local_broker.audit_chain import (
        emit_failure_for_case_dir,
        emit_for_case_dir,
    )

    proposed_event_hash = emit_for_case_dir(
        ctx.case_dir,
        decision_kind="waiver_propose",
        payload=payload,
        item_id=waiver_id,
    )

    row = {
        "waiver_id": waiver_id,
        "state": "pending",
        "blocker_ids": list(blocker_ids),
        "operator_id": operator_id,
        "justification": justification,
        "proposed_ts": proposed_ts,
        "proposed_event_hash": proposed_event_hash,
        "dpo_id": None,
        "dpo_note": None,
        "co_signed_ts": None,
        "cosign_event_hash": None,
    }
    try:
        _append_waiver(ctx, row)
    except OSError as exc:
        emit_failure_for_case_dir(
            ctx.case_dir,
            decision_kind="waiver_propose",
            payload={
                "phase": "post-chain-jsonl-write",
                "original_event_hash": proposed_event_hash,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "target_path": str(_waivers_path(ctx)),
                "waiver_id": waiver_id,
            },
            item_id=waiver_id,
        )
        raise
    return row


def co_sign_waiver(
    ctx: CaseContext,
    *,
    waiver_id: str,
    dpo_id: str,
    dpo_note: str,
) -> dict:
    """DPO co-signs a pending waiver, finalising it. Raises ``LookupError``
    if the waiver_id is unknown; ``ValueError`` if already co-signed."""
    if not (dpo_id or "").strip():
        raise ValueError("dpo_id must not be empty")
    if not (dpo_note or "").strip():
        raise ValueError("dpo_note must not be empty")

    existing = load_waiver(ctx, waiver_id)
    if existing is None:
        raise LookupError(f"waiver {waiver_id!r} not found")
    if existing["state"] != "pending":
        raise ValueError(
            f"waiver {waiver_id!r} already co-signed by {existing.get('dpo_id')!r} "
            f"at {existing.get('co_signed_ts')!r}"
        )

    cosign_ts = _iso_now()
    payload = {
        "ts": cosign_ts,
        "waiver": True,
        "action": "cosign",
        "waiver_id": waiver_id,
        "blocker_ids": existing["blocker_ids"],
        "operator_id": existing["operator_id"],
        "justification": existing["justification"],
        "dpo_id": dpo_id,
        "dpo_note": dpo_note,
        "original_event_hash": existing["proposed_event_hash"],
    }

    from dsar_orchestrator.local_broker.audit_chain import (
        emit_failure_for_case_dir,
        emit_for_case_dir,
    )

    cosign_event_hash = emit_for_case_dir(
        ctx.case_dir,
        decision_kind="waiver_cosign",
        payload=payload,
        item_id=waiver_id,
    )

    finalised = dict(existing)
    finalised.update(
        {
            "state": "co_signed",
            "dpo_id": dpo_id,
            "dpo_note": dpo_note,
            "co_signed_ts": cosign_ts,
            "cosign_event_hash": cosign_event_hash,
        }
    )
    try:
        _append_waiver(ctx, finalised)
    except OSError as exc:
        emit_failure_for_case_dir(
            ctx.case_dir,
            decision_kind="waiver_cosign",
            payload={
                "phase": "post-chain-jsonl-write",
                "original_event_hash": cosign_event_hash,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "target_path": str(_waivers_path(ctx)),
                "waiver_id": waiver_id,
            },
            item_id=waiver_id,
        )
        raise
    return finalised


def check_dpo_auth(authorization_header: str | None) -> tuple[bool, str | None]:
    """Auth shim for DPO-only routes. Returns ``(True, None)`` if the
    request is allowed, ``(False, reason)`` if rejected. When
    ``DSAR_DPO_TOKEN`` is unset, single-operator mode — always allow.
    """
    token = os.environ.get("DSAR_DPO_TOKEN")
    if not token:
        return True, None
    if not authorization_header:
        return False, "missing Authorization header"
    if not authorization_header.startswith("Bearer "):
        return False, "expected 'Bearer <token>' Authorization header"
    presented = authorization_header[len("Bearer ") :].strip()
    if not secrets.compare_digest(presented, token):
        return False, "invalid DPO token"
    return True, None
