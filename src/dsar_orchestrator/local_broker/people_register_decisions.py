"""Decision writer for the /people-register operator console.

Handles the four spec §1.5 actions and writes spec §3.2-conformant
working/third_party_denylist.json + applies subject-alias / merge edits
back to working/people_register.json and working/data_subject.json.

All writes are atomic (tmp + fsync + os.replace, mode 0o600).

Bulk-accept for @<controller-domain> emails is deferred — not wired in
row-level UI. Can be added as a separate form or JSON API endpoint.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dsar_orchestrator.local_broker.people_register_console import (
    cluster_id as _cid,
    load_people_register,
)

VALID_ACTIONS = frozenset(
    {
        "accept_as_third_party",
        "preserve",
        "merge_with",
        "mark_subject_alias",
    }
)

_DECISION_LOCK = threading.Lock()


class DecisionError(ValueError):
    """Raised on invalid action / unknown cluster_id / missing required arg."""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _find_cluster(clusters: list[dict], cluster_id_value: str) -> dict | None:
    for c in clusters:
        if _cid(c) == cluster_id_value:
            return c
    return None


def _load_denylist(case_dir: Path) -> dict[str, Any]:
    p = case_dir / "working" / "third_party_denylist.json"
    if not p.exists():
        return {
            "schema_version": 1,
            "controller": "",
            "populated_at": "",
            "operator_id": "",
            "entries": [],
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": 1,
            "controller": "",
            "populated_at": "",
            "operator_id": "",
            "entries": [],
        }


def write_denylist(
    *,
    case_dir: Path,
    controller: str,
    operator_id: str,
    entries: list[dict[str, Any]],
) -> None:
    """Spec §3.2 schema. Atomic write."""
    payload = {
        "schema_version": 1,
        "controller": controller,
        "populated_at": _iso_now(),
        "operator_id": operator_id,
        "entries": entries,
    }
    _atomic_write_json(case_dir / "working" / "third_party_denylist.json", payload)


def _upsert_entry(
    denylist: dict[str, Any],
    *,
    canonical_name: str,
    redact: bool,
    operator_note: str,
    cluster_id_value: str,
) -> None:
    entries = denylist.setdefault("entries", [])
    for e in entries:
        if e.get("people_register_cluster_id") == cluster_id_value:
            e["canonical_name"] = canonical_name
            e["redact"] = redact
            e["operator_note"] = operator_note
            return
    entries.append(
        {
            "canonical_name": canonical_name,
            "redact": redact,
            "operator_note": operator_note,
            "people_register_cluster_id": cluster_id_value,
        }
    )


def _remove_entry(denylist: dict[str, Any], cluster_id_value: str) -> None:
    entries = denylist.get("entries") or []
    denylist["entries"] = [
        e for e in entries if e.get("people_register_cluster_id") != cluster_id_value
    ]


def record_decision(
    *,
    case_dir: Path,
    cluster_id: str,
    action: str,
    operator_id: str,
    controller: str,
    note: str = "",
    merge_target_id: str | None = None,
) -> None:
    """Apply an operator decision per spec §1.5. Atomic writes to affected
    files. Raises DecisionError on invalid input."""
    if action not in VALID_ACTIONS:
        raise DecisionError(f"unknown action {action!r}; valid: {sorted(VALID_ACTIONS)}")

    with _DECISION_LOCK:
        clusters = load_people_register(case_dir)
        source = _find_cluster(clusters, cluster_id)
        if source is None:
            raise DecisionError(f"unknown cluster_id {cluster_id!r}")

        denylist = _load_denylist(case_dir)
        denylist["controller"] = controller
        denylist["operator_id"] = operator_id

        if action == "accept_as_third_party":
            _upsert_entry(
                denylist,
                canonical_name=source["canonical_name"],
                redact=True,
                operator_note=note,
                cluster_id_value=cluster_id,
            )

        elif action == "preserve":
            _upsert_entry(
                denylist,
                canonical_name=source["canonical_name"],
                redact=False,
                operator_note=note,
                cluster_id_value=cluster_id,
            )

        elif action == "merge_with":
            if not merge_target_id:
                raise DecisionError("merge_with requires merge_target_id")
            target = _find_cluster(clusters, merge_target_id)
            if target is None:
                raise DecisionError(f"merge target cluster_id {merge_target_id!r} not found")
            for field in ("emails", "phones", "titles", "source_refs", "correlation_ids"):
                for v in source.get(field) or []:
                    if v not in target.setdefault(field, []):
                        target[field].append(v)
            target["mention_count"] = int(target.get("mention_count") or 0) + int(
                source.get("mention_count") or 0
            )
            target["distinct_doc_count"] = len(target.get("source_refs") or [])
            clusters = [c for c in clusters if _cid(c) != cluster_id]
            _atomic_write_json(case_dir / "working" / "people_register.json", clusters)
            _remove_entry(denylist, cluster_id)
            _atomic_write_json(case_dir / "working" / "third_party_denylist.json", denylist)
            return

        elif action == "mark_subject_alias":
            ds_path = case_dir / "working" / "data_subject.json"
            if not ds_path.exists():
                raise DecisionError(
                    f"data_subject.json not found at {ds_path}; "
                    f"Phase 1 setup must populate the subject record before "
                    f"mark_subject_alias can fold a cluster into it"
                )
            ds = json.loads(ds_path.read_text(encoding="utf-8"))
            aliases = list(ds.get("aliases") or [])
            additional_emails = list(ds.get("additional_emails") or [])
            # Phase 3 extension: subject_phones carries phones from clusters
            # the operator confirms as the subject. Downstream subject_protection
            # (Phase 2) should embed these alongside the name/email identifiers
            # in v2; for v1 they're preserved so they aren't lost.
            subject_phones = list(ds.get("subject_phones") or [])
            name = source.get("canonical_name") or ""
            if name and name not in aliases and name != ds.get("full_name"):
                aliases.append(name)
            for e in source.get("emails") or []:
                if e and e not in additional_emails and e != ds.get("email"):
                    additional_emails.append(e)
            for ph in source.get("phones") or []:
                if ph and ph not in subject_phones:
                    subject_phones.append(ph)
            ds["aliases"] = aliases
            ds["additional_emails"] = additional_emails
            ds["subject_phones"] = subject_phones
            _atomic_write_json(ds_path, ds)
            source["is_data_subject"] = True
            source["is_subject_confidence"] = 1.0
            _atomic_write_json(case_dir / "working" / "people_register.json", clusters)
            _remove_entry(denylist, cluster_id)
            _atomic_write_json(case_dir / "working" / "third_party_denylist.json", denylist)
            return

        _atomic_write_json(case_dir / "working" / "third_party_denylist.json", denylist)
