"""DSAR Approver Agent — final release-readiness gate before human sign-off.

Routes a structured DSAR release package through the local mlx-broker
(model alias ``chat``) using the conservative-reviewer system prompt
from the agent specification.

Returns one of:
  - APPROVE_FOR_HUMAN_SIGNOFF
  - APPROVE_WITH_CONDITIONS
  - REJECT
  - ESCALATE_TO_DPO_OR_LEGAL

Output JSON is validated against the published schema before being
appended to ``<case-root>/audit/approver-decisions.jsonl``.

CLI usage:
  dsar-approver <case_id>                         < input.json
  dsar-approver --case-root <path> <case_id>      < input.json
  dsar-approver --selftest

When ``--case-root`` is omitted, the current working directory is used
(matching the legacy in-bundle behaviour). ``DSAR_CASE_ROOT`` env var
overrides the cwd default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

BROKER = "http://127.0.0.1:8090/v1/chat/completions"
MODEL = "chat"

SYSTEM_PROMPT = """You are the DSAR Approver Agent.

You are the final automated quality, compliance, and release-readiness gate in a Data Subject Access Request pipeline.

Your purpose is to decide whether a DSAR release package is ready for human approval, requires conditional correction, must be rejected, or must be escalated to the DPO or Legal.

You do not perform the original search, deduplication, redaction, exemption assessment, or response drafting. You verify whether those stages have been completed correctly, consistently, and defensibly.

You must behave as a conservative privacy quality reviewer.

You must protect against:
- accidental over-disclosure
- under-disclosure
- missed third-party data
- missed PHI
- missed special category data
- poor redaction
- reversible redaction
- inconsistent exemption handling
- poor auditability
- contradictions between the disclosure bundle and response letter
- release of hidden metadata, comments, tracked changes, or unredacted layers

You must assess the package using the evidence provided. Do not invent facts. Do not assume that a missing report exists. Do not assume that a search was performed unless the evidence shows it.

Your decision must be one of:

APPROVE_FOR_HUMAN_SIGNOFF
APPROVE_WITH_CONDITIONS
REJECT
ESCALATE_TO_DPO_OR_LEGAL

You must not give final legal approval. You only determine readiness for human sign-off.

Approval standard:

Only use APPROVE_FOR_HUMAN_SIGNOFF where:
- identity verification is complete or appropriately documented
- statutory deadline is recorded
- scope is defined
- all in-scope systems are searched or explicitly excluded with documented rationale
- search methodology is documented
- deduplication is complete
- relevance review is complete
- third-party review is complete
- exemption assessment is complete
- redactions are irreversible
- redaction codes are present and consistent
- response letter matches the disclosure bundle and withheld items
- final internal report is complete
- release package metadata has been checked
- there are no unresolved high-risk or critical issues

Use APPROVE_WITH_CONDITIONS where:
- the package is broadly safe
- no high-risk release blockers are present
- remaining issues are minor or moderate
- all required corrections can be clearly listed
- the corrected package should be rechecked before release

Use REJECT where:
- the package is unsafe
- redactions may be reversible
- third-party data is exposed without justification
- PHI or special category data appears without review
- search coverage is incomplete or undocumented
- identity verification is missing
- the response letter and disclosure bundle contradict each other
- audit records are insufficient
- release metadata has not been checked
- you cannot determine whether the package is safe

Use ESCALATE_TO_DPO_OR_LEGAL where:
- legal privilege may apply
- complex exemptions are involved
- high-risk PHI or special category data is present
- the case involves regulatory sensitivity
- the requester disputes scope, identity, or disclosure
- disclosure could affect another individual's rights and freedoms
- the case involves employment relations, litigation, whistleblowing, safeguarding, criminal allegations, or regulatory investigations
- there is uncertainty that requires accountable human judgement

You must check the following areas:

1. Case setup — case ID, requester, data subject, date received, statutory deadline, request wording, identity verification, authority to act, scope status.
2. Scope and search — systems searched, custodians, repositories, date ranges, identifiers, keywords, search limitations, exclusions, failed searches, evidence of completion.
3. Ingestion — manifest of ingested files, file types, extraction status, corrupted files, password-protected files, failed imports, attachments, spreadsheets, embedded files, unsupported formats.
4. Data subject relevance — included records relate to the data subject; contextual records are justified.
5. Deduplication — exact duplicates and near-duplicates handled consistently; duplicate removal did not remove materially different records.
6. Third-party data — names, emails, identifiers, opinions, contact details, employee data, patient data, client data, other external individual data identified and protected.
7. PHI and special category data — health data, adverse events, medical info, ethnicity, religion, biometric, genetic, sex life, trade union, criminal offence data present? review/redaction decisions documented?
8. Exemptions and withholding — withheld material has documented reason; response letter explains withholding appropriately.
9. Redaction — each redaction blacked out, irreversible, has a code, maps to a redaction log entry; redactions make sense in context; no partial identifiers visible.
10. Redaction QA — no selectable text remains beneath redactions; no hidden layers; no comments; no OCR text leaks; no metadata exposing redacted material.
11. Disclosure bundle — correct documents, versions, format; no draft or source documents.
12. Response letter — matches disclosure bundle, withheld documents log, exemption decisions, deadline position, identity status.
13. Final internal report — defensible audit trail; request history, scope, searches, decisions, redactions, exemptions, reviewers, issues, final recommendation.
14. Release package safety — files flattened where required, metadata stripped, comments and tracked changes removed, hidden tabs removed, embedded files reviewed, no unredacted source material.
15. Human review readiness — who should review next: Privacy Lead, DPO, Legal, HR, Information Security, Clinical Safety, Pharmacovigilance, other accountable owner.

Your output must be valid JSON only. Do not include markdown. Do not include commentary outside the JSON.

The JSON must follow this structure:

{
  "case_id": "",
  "decision": "",
  "risk_level": "",
  "summary": "",
  "reviewed_areas": [{"area":"","status":"","notes":""}],
  "blocking_issues": [{"issue_id":"","area":"","issue":"","evidence":"","required_action":"","owner":"","severity":""}],
  "conditions": [{"condition_id":"","area":"","issue":"","required_action":"","owner":"","severity":""}],
  "escalations": [{"escalation_id":"","area":"","reason":"","recommended_recipient":"","severity":""}],
  "release_safety_checks": {
    "irreversible_redaction_confirmed":"","redaction_codes_confirmed":"","metadata_removed":"",
    "comments_removed":"","tracked_changes_removed":"","hidden_layers_removed":"",
    "hidden_spreadsheet_tabs_checked":"","ocr_text_checked":"","attachments_checked":"",
    "embedded_objects_checked":""
  },
  "recommended_next_step": "",
  "recommended_reviewer": "",
  "approval_notes": []
}

Allowed values:
- decision: APPROVE_FOR_HUMAN_SIGNOFF | APPROVE_WITH_CONDITIONS | REJECT | ESCALATE_TO_DPO_OR_LEGAL
- risk_level: LOW | MEDIUM | HIGH | CRITICAL
- reviewed_areas.status: PASS | PASS_WITH_NOTE | FAIL | NOT_PROVIDED | ESCALATE
- severity: LOW | MEDIUM | HIGH | CRITICAL
- release_safety_checks values: YES | NO | UNKNOWN | NOT_APPLICABLE

For any missing required evidence, mark the relevant area as NOT_PROVIDED and consider whether the decision must be REJECT or ESCALATE_TO_DPO_OR_LEGAL.

Do not approve where evidence is absent.
Do not approve based on trust.
Do not approve based on agent confidence alone.
Base every conclusion on supplied artefacts."""

_SAFETY_CHECK_KEYS = [
    "irreversible_redaction_confirmed",
    "redaction_codes_confirmed",
    "metadata_removed",
    "comments_removed",
    "tracked_changes_removed",
    "hidden_layers_removed",
    "hidden_spreadsheet_tabs_checked",
    "ocr_text_checked",
    "attachments_checked",
    "embedded_objects_checked",
]

OUTPUT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "DSARApproverAgentOutput",
    "type": "object",
    "required": [
        "case_id",
        "decision",
        "risk_level",
        "summary",
        "reviewed_areas",
        "blocking_issues",
        "conditions",
        "escalations",
        "release_safety_checks",
        "recommended_next_step",
        "recommended_reviewer",
        "approval_notes",
    ],
    "properties": {
        "case_id": {"type": "string"},
        "decision": {
            "type": "string",
            "enum": [
                "APPROVE_FOR_HUMAN_SIGNOFF",
                "APPROVE_WITH_CONDITIONS",
                "REJECT",
                "ESCALATE_TO_DPO_OR_LEGAL",
            ],
        },
        "risk_level": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
        },
        "summary": {"type": "string"},
        "reviewed_areas": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["area", "status", "notes"],
                "properties": {
                    "area": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "PASS",
                            "PASS_WITH_NOTE",
                            "FAIL",
                            "NOT_PROVIDED",
                            "ESCALATE",
                        ],
                    },
                    "notes": {"type": "string"},
                },
            },
        },
        "blocking_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "issue_id",
                    "area",
                    "issue",
                    "evidence",
                    "required_action",
                    "owner",
                    "severity",
                ],
                "properties": {
                    "issue_id": {"type": "string"},
                    "area": {"type": "string"},
                    "issue": {"type": "string"},
                    "evidence": {"type": "string"},
                    "required_action": {"type": "string"},
                    "owner": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    },
                },
            },
        },
        "conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "condition_id",
                    "area",
                    "issue",
                    "required_action",
                    "owner",
                    "severity",
                ],
                "properties": {
                    "condition_id": {"type": "string"},
                    "area": {"type": "string"},
                    "issue": {"type": "string"},
                    "required_action": {"type": "string"},
                    "owner": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    },
                },
            },
        },
        "escalations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "escalation_id",
                    "area",
                    "reason",
                    "recommended_recipient",
                    "severity",
                ],
                "properties": {
                    "escalation_id": {"type": "string"},
                    "area": {"type": "string"},
                    "reason": {"type": "string"},
                    "recommended_recipient": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    },
                },
            },
        },
        "release_safety_checks": {
            "type": "object",
            "required": _SAFETY_CHECK_KEYS,
            "properties": {
                k: {
                    "type": "string",
                    "enum": ["YES", "NO", "UNKNOWN", "NOT_APPLICABLE"],
                }
                for k in _SAFETY_CHECK_KEYS
            },
        },
        "recommended_next_step": {"type": "string"},
        "recommended_reviewer": {"type": "string"},
        "approval_notes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

_VALIDATOR = Draft202012Validator(OUTPUT_SCHEMA)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_case_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("DSAR_CASE_ROOT")
    if env:
        return Path(env)
    return Path.cwd()


def _audit_log_path(case_root: Path) -> Path:
    return case_root / "audit" / "approver-decisions.jsonl"


def _call_broker(user_payload: str, *, max_tokens: int = 16000) -> dict:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(BROKER, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)


def review(case_id: str, package: dict, *, case_root: Path | None = None) -> dict:
    """Submit a DSAR release package to the Approver Agent and return its
    validated decision record. Side effect: appends a full prompt+response
    audit row to ``<case_root>/audit/approver-decisions.jsonl``.
    """
    root = _resolve_case_root(case_root)
    user_payload = (
        f"Case ID: {case_id}\n\n"
        f"DSAR release package (structured input — assess only what is "
        f"present; treat missing keys as NOT_PROVIDED):\n\n" + json.dumps(package, indent=2)
    )
    started = time.monotonic()
    raw = _call_broker(user_payload)
    elapsed = round(time.monotonic() - started, 2)

    msg = raw["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning") or "").strip()
    finish = raw["choices"][0]["finish_reason"]

    if not content:
        raise RuntimeError(
            f"approver returned no content (finish_reason={finish}; reasoning len={len(reasoning)})"
        )

    try:
        decision = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"approver content not valid JSON: {exc}\n{content!r}") from exc

    try:
        _VALIDATOR.validate(decision)
    except ValidationError as exc:
        raise RuntimeError(
            f"approver output failed schema validation: {exc.message} "
            f"(path: {list(exc.absolute_path)})"
        ) from exc

    audit = {
        "ts": _iso_now(),
        "case_id": case_id,
        "model": raw.get("model", MODEL),
        "elapsed_sec": elapsed,
        "finish_reason": finish,
        "prompt": {"system": SYSTEM_PROMPT, "user": user_payload},
        "response": {"reasoning": reasoning, "content": content},
        "decision": decision,
    }
    audit_path = _audit_log_path(root)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as fh:
        fh.write(json.dumps(audit) + "\n")
    return decision


def _selftest(case_root: Path) -> int:
    """Quick smoke test with a synthetic missing-evidence package — the
    approver should reject or escalate, never approve."""
    package = {
        "request_summary": {
            "request_received": "2026-05-01",
            "request_text": "All personal data held about me.",
        },
        "requester_identity_status": {},
        "data_subject_profile": {},
        "scope_definition": {},
        "deadline_record": {},
        "systems_searched": [],
        "search_methodology": {},
        "ingested_documents_manifest": [],
        "deduplication_report": {},
        "relevance_report": {},
        "third_party_detection_report": {},
        "exemption_assessment_report": {},
        "redaction_log": [],
        "redaction_qa_report": {},
        "disclosure_bundle_manifest": [],
        "withheld_documents_log": [],
        "response_letter_draft": "",
        "final_internal_report": "",
        "release_package_metadata_report": {},
        "previous_human_comments": [],
    }
    print("Running approver selftest with empty-evidence package...")
    decision = review("SELFTEST-0001", package, case_root=case_root)
    print(f"\ndecision:           {decision['decision']}")
    print(f"risk_level:         {decision['risk_level']}")
    print(f"summary:            {decision['summary']}")
    print(f"blocking_issues:    {len(decision['blocking_issues'])}")
    print(f"conditions:         {len(decision['conditions'])}")
    print(f"escalations:        {len(decision['escalations'])}")
    print(f"recommended_reviewer: {decision['recommended_reviewer']}")
    if decision["decision"] == "APPROVE_FOR_HUMAN_SIGNOFF":
        print("\nFAIL: approver approved an empty-evidence package", file=sys.stderr)
        return 1
    print("\nOK: approver correctly refused to approve empty evidence.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="dsar-approver",
        description="Final release-readiness gate for a DSAR package via mlx-broker.",
    )
    p.add_argument(
        "--case-root",
        type=Path,
        default=None,
        help="Case directory (audit log lands at <case-root>/audit/approver-decisions.jsonl). "
        "Defaults to $DSAR_CASE_ROOT or cwd.",
    )
    p.add_argument(
        "--selftest",
        action="store_true",
        help="Submit an empty-evidence package as a smoke test; exits 1 if approver approves it.",
    )
    p.add_argument(
        "case_id",
        nargs="?",
        help="Case identifier (required unless --selftest).",
    )
    args = p.parse_args()
    case_root = _resolve_case_root(args.case_root)
    if args.selftest:
        return _selftest(case_root)
    if not args.case_id:
        p.error("case_id is required (or pass --selftest)")
    raw = sys.stdin.read().strip()
    if not raw:
        print("error: no JSON package on stdin", file=sys.stderr)
        return 2
    try:
        package = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2
    decision = review(args.case_id, package, case_root=case_root)
    print(json.dumps(decision, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
