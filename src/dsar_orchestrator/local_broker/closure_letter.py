"""Auto-draft the DSAR closure letter from the case state.

Reads the orchestrator's outputs (dedupe findings, durant verdicts,
responsiveness decisions, redaction decisions, approver verdict) and
produces a markdown draft of the closure letter the operator can review,
edit, and sign before sending.

Trigger semantics (the operator console uses these to decide when to
surface a "Review closure-letter draft" CTA):

- ``readiness_state(ctx)`` returns one of:
  - "not_ready_no_approver"   — Approver hasn't run yet
  - "not_ready_blocked"        — Approver said REJECT
  - "ready_with_conditions"    — Approver said APPROVE_WITH_CONDITIONS
  - "ready_approved"           — Approver said APPROVE_FOR_HUMAN_SIGNOFF
  - "escalate"                 — Approver said ESCALATE_TO_DPO_OR_LEGAL
  - "case_closed"              — orchestrator state is `closed`

- ``draft_letter(ctx)`` always returns a markdown string; it adds a
  prominent NOT-READY banner when readiness_state isn't approved.

The template wording follows the operator's documented preferences:
- "mentioned incidentally but not substantively relating to the requester
  as an individual" — NOT "work context" (per ICO guidance)
- Generic redaction principle without revealing what was redacted
- Per UK GDPR Art 15 + DPA 2018; ICO complaint right cited
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _CaseShim:
    """Subset of operator_console.CaseContext used by this module.

    Defined locally to avoid the cyclic import (operator_console imports
    from local_broker; local_broker shouldn't import back).
    """

    case_dir: Path

    @property
    def case_id(self) -> str:
        return self.case_dir.name

    @property
    def working(self) -> Path:
        return self.case_dir / "working"

    @property
    def audit(self) -> Path:
        return self.case_dir / "audit"


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    try:
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _read_json(p: Path) -> Any:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def compute_funnel(ctx: _CaseShim) -> dict[str, int]:
    """Compute the document-flow funnel from case state. All counts are
    derived from the working/* artefacts; no LLM, no broker."""
    working = ctx.working

    # Source / extraction
    ingested = _read_jsonl(working / "ingested_items.jsonl")
    ingested_count = len(ingested)
    # Extraction failures: we know operator built agent01_input.jsonl from
    # find -L on source/. Diff against ingested_items gives the count.
    agent01_input = _read_jsonl(working / "agent01_input.jsonl")
    source_attempted = len(agent01_input) if agent01_input else max(ingested_count, 0)
    extraction_failed = max(source_attempted - ingested_count, 0)

    # Dedupe
    dedupe = _read_jsonl(working / "dedupe_findings.jsonl")
    register = _read_json(working / "register.json") or []
    hash_to_ref: dict[str, str] = {}
    for e in register:
        ref = e.get("ref", "")
        h = e.get("hash", "")
        if ref and h:
            hash_to_ref[h] = ref
    canonical_hashes = set()
    duplicate_count = 0
    for r in dedupe:
        h = r.get("doc_ref", "")
        if r.get("dedupe_verdict") == "canonical":
            canonical_hashes.add(h)
        elif r.get("dedupe_verdict") == "duplicate":
            duplicate_count += 1
    canonical_refs = {hash_to_ref[h] for h in canonical_hashes if h in hash_to_ref}
    unique_after_dedupe = len(canonical_refs) if canonical_refs else ingested_count

    # Durant — register-style ref
    durant = _read_jsonl(working / "durant_verdicts.jsonl")
    bio_refs = {r["doc_ref"] for r in durant if r.get("durant_verdict") == "biographical"}
    wco_refs = {r["doc_ref"] for r in durant if r.get("durant_verdict") == "work_context_only"}
    canonical_bio = bio_refs & canonical_refs if canonical_refs else bio_refs
    canonical_wco = wco_refs & canonical_refs if canonical_refs else wco_refs

    # Redaction outcomes
    redaction = _read_jsonl(working / "redaction_decisions.jsonl")
    leak_failures = sum(
        1
        for r in redaction
        if r.get("status") == "failed" and (not canonical_refs or r.get("doc_ref") in canonical_refs)
    )
    redacted_ok_canonical = sum(
        1
        for r in redaction
        if r.get("status") == "redacted" and (not canonical_refs or r.get("doc_ref") in canonical_refs)
    )

    # PII entity totals
    entity_total = 0
    entity_redact = 0
    entity_flag = 0
    entity_subject = 0
    import glob

    for fp in glob.glob(str(working / "*_tags.json")):
        t = _read_json(Path(fp)) or {}
        entity_total += t.get("entity_count", 0)
        entity_redact += t.get("redact_count", 0)
        entity_flag += t.get("flag_count", 0)
        for e in t.get("entities", []):
            if e.get("classification") == "data_subject":
                entity_subject += 1

    return {
        "source_attempted": source_attempted,
        "extraction_failed": extraction_failed,
        "ingested": ingested_count,
        "duplicates_removed": duplicate_count,
        "unique_after_dedupe": unique_after_dedupe,
        "biographical_canonical": len(canonical_bio),
        "incidental_canonical": len(canonical_wco),
        "leak_failures_canonical": leak_failures,
        "redacted_ok_canonical": redacted_ok_canonical,
        "final_disclosure_items": max(redacted_ok_canonical, len(canonical_bio) - leak_failures),
        "pii_entities_total": entity_total,
        "pii_redact_third_party": entity_redact,
        "pii_flag_for_review": entity_flag,
        "pii_subject_preserved": entity_subject,
    }


def latest_approver_verdict(ctx: _CaseShim) -> dict | None:
    """Latest approver decision row, or None."""
    path = ctx.audit / "approver-decisions.jsonl"
    if not path.exists():
        return None
    last = None
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
    return last


def _read_orchestrator_state(ctx: _CaseShim) -> dict:
    p = ctx.working / "orchestrator_state.json"
    if not p.exists():
        return {"current_stage": "intake_created"}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"current_stage": "intake_created"}


def readiness_state(ctx: _CaseShim) -> str:
    """Trigger semantic for surfacing the letter to the operator."""
    state = _read_orchestrator_state(ctx)
    if state.get("current_stage") == "closed":
        return "case_closed"
    verdict = latest_approver_verdict(ctx)
    if not verdict:
        return "not_ready_no_approver"
    decision = (verdict.get("decision") or {}).get("decision", "")
    return {
        "APPROVE_FOR_HUMAN_SIGNOFF": "ready_approved",
        "APPROVE_WITH_CONDITIONS": "ready_with_conditions",
        "REJECT": "not_ready_blocked",
        "ESCALATE_TO_DPO_OR_LEGAL": "escalate",
    }.get(decision, "not_ready_no_approver")


def _readiness_banner(state: str, verdict: dict | None) -> str:
    """Markdown banner explaining draft state."""
    if state == "ready_approved":
        return (
            "> ✓ **Release readiness review: APPROVED.** This draft is ready "
            "for human sign-off and dispatch."
        )
    if state == "ready_with_conditions":
        conditions = (verdict or {}).get("decision", {}).get("conditions", [])
        return (
            f"> ⚠ **Release readiness: APPROVE WITH CONDITIONS** "
            f"({len(conditions)} condition(s) listed in the Approver verdict). "
            "Resolve conditions before sign-off; the draft below is otherwise ready."
        )
    if state == "not_ready_blocked":
        blockers = (verdict or {}).get("decision", {}).get("blocking_issues", [])
        return (
            f"> ✗ **NOT YET READY — Approver verdict: REJECT** "
            f"({len(blockers)} open blocker(s)). This draft shows what would be sent "
            "once the blockers are resolved. See /blockers."
        )
    if state == "escalate":
        return (
            "> ↑ **ESCALATE TO DPO / LEGAL** — Approver verdict requires senior "
            "review before any release decision. This draft is provisional."
        )
    if state == "case_closed":
        return (
            "> ✓ **Case closed.** This letter has been dispatched (see audit log)."
        )
    return (
        "> ⓘ **DRAFT — Approver hasn't run yet.** Visit /release-check to "
        "generate the readiness verdict, then return here."
    )


def _load_data_subject(ctx: _CaseShim) -> dict:
    return _read_json(ctx.working / "data_subject.json") or {}


def _load_case_context(ctx: _CaseShim) -> dict:
    return _read_json(ctx.working / "case_context.json") or {}


def draft_letter(ctx: _CaseShim) -> str:
    """Generate the markdown draft from case state."""
    funnel = compute_funnel(ctx)
    verdict = latest_approver_verdict(ctx)
    state = readiness_state(ctx)
    banner = _readiness_banner(state, verdict)
    ds = _load_data_subject(ctx)
    cc = _load_case_context(ctx)
    subject_name = ds.get("full_name", "[Name]")
    aliases = ds.get("aliases", [])
    email = ds.get("email", "[email]")
    additional_emails = ds.get("additional_emails", [])
    controller = cc.get("controller", "[Controller]")
    request_date = cc.get("request_date") or "**[date — fill in]**"
    deadline = cc.get("response_deadline") or "**[deadline — not recorded; fill in]**"
    case_id = ctx.case_id

    alias_str = ", ".join(f'"{a}"' for a in aliases) if aliases else "—"
    addl_email_str = ", ".join(additional_emails) if additional_emails else "—"

    f = funnel  # shorthand

    leak_paragraph = (
        "Three further items could not be confirmed as safely redacted within "
        "the response timeline. These have been set aside for review and, where "
        "appropriate, will be supplied in a supplementary response or held back "
        "under an applicable exemption with a separate explanation."
    ) if f["leak_failures_canonical"] > 0 else ""
    closing_caveat = (
        ", subject to any supplementary material to be provided in respect of "
        "the items referred to above"
    ) if f["leak_failures_canonical"] > 0 else ""
    return f"""# DSAR Closure Letter — DRAFT

**Case:** {case_id}
**Data subject:** {subject_name}
**Controller:** {controller}
**Request date:** {request_date}
**Statutory deadline:** {deadline}
**Generated:** {datetime.now(UTC).isoformat(timespec="seconds")}

{banner}

---

## Document funnel (live from case state)

| Stage | Count | Explanation |
|---|---:|---|
| Initial data points identified | **{f['source_attempted']:,}** | Raw files across the in-scope systems |
| Items unextractable / outside scope | **{f['extraction_failed']:,}** | Encrypted, unsupported, or corrupt files set aside |
| Documents / artefacts selected for review | **{f['ingested']:,}** | Substantive review population after extraction |
| Duplicates removed (Message-ID match) | **{f['duplicates_removed']:,}** | Cross-mailbox copies of the same logical email |
| Unique artefacts reviewed | **{f['unique_after_dedupe']:,}** | De-duplicated review population |
| Artefacts containing requester personal data | **{f['biographical_canonical']:,}** | Substantive content about the requester — included in disclosure bundle |
| Artefacts mentioning requester only incidentally | **{f['incidental_canonical']:,}** | Subject named only as sender / recipient / cc / distribution member; substantive content concerns other matters |
| Artefacts withheld pending operator review | **{f['leak_failures_canonical']:,}** | Leak verification could not confirm safe redaction |
| **Final disclosure items** | **{f['final_disclosure_items']:,}** | Net items released in the disclosure bundle |

Supporting PII metrics (across the included documents):

- Total personal-data entities detected: **{f['pii_entities_total']:,}**
- Third-party PII redactions applied: **{f['pii_redact_third_party']:,}**
- Items flagged for operator review: **{f['pii_flag_for_review']:,}**
- Requester's own identifiers preserved: **{f['pii_subject_preserved']:,}**

---

## Draft letter (fill in bracketed fields before send)

> **Subject:** Response to your data subject access request — reference {case_id}
>
> Dear {subject_name},
>
> We are writing in response to your data subject access request received on
> {request_date}.
>
> Your request was handled under Article 15 of the UK GDPR and the applicable
> provisions of the Data Protection Act 2018. We have now completed our
> searches and review of the information held within the scope of your
> request. This response provides the personal data identified as relating
> to you, together with relevant explanatory information about how your
> request was processed.
>
> **Scope of the search**
>
> The scope of our search covered the Microsoft 365 environment associated
> with {controller}, including Exchange mailboxes, SharePoint workspaces,
> and related repositories where personal data relating to you may have
> been held.
>
> Identifiers used for the search: your full name ({subject_name}),
> the aliases [{alias_str}], your primary email address ({email}),
> and any additional contact details on file ({addl_email_str}).
>
> Material outside that scope — operational systems unrelated to your
> engagement and personal data of other individuals not linked to you —
> was not searched.
>
> **Search and review methodology**
>
> As part of our search and review process, we identified **{f['source_attempted']:,}**
> initial data points across the in-scope systems. These data points
> included emails, attachments, spreadsheets, documents, and other
> potentially relevant artefacts.
>
> Following initial filtering, **{f['extraction_failed']:,}** items were set
> aside as they could not be reliably extracted for review (encrypted or
> password-protected files, unsupported file formats, and similar technical
> exclusions). The remaining **{f['ingested']:,}** documents and artefacts
> were taken forward for substantive review.
>
> We then identified and removed **{f['duplicates_removed']:,}** duplicate or
> substantially duplicate items. Many of these were copies of the same
> email present in multiple mailboxes because they had been sent to,
> copied to, or forwarded among several recipients — once de-duplicated
> these collapse to a single representative item. This left
> **{f['unique_after_dedupe']:,}** unique artefacts for substantive assessment.
>
> Of those **{f['unique_after_dedupe']:,}** unique artefacts,
> **{f['biographical_canonical']:,}** were assessed as containing personal
> data relating to you and have been included in the disclosure bundle,
> subject to any necessary redactions.
>
> A further **{f['incidental_canonical']:,}** artefacts mentioned your name,
> email address, role, or work involvement but were assessed as not
> requiring disclosure because the content did not relate to you as an
> individual for the purposes of the right of access. These included, for
> example, items where your name appeared only as a sender, recipient,
> attendee, distribution-list member, document owner, workflow
> participant, or general business contact, and where the substantive
> content concerned a business process or matter rather than information
> about you.
>
> **Disclosed material**
>
> The documents and extracts provided with this response contain the
> personal data identified as relating to you within the scope of your
> request.
>
> Where a document contained both your personal data and information
> relating to other individuals, we have provided the information
> relating to you and redacted information relating to others where
> disclosure would adversely affect their rights and freedoms or where
> it would not be reasonable to disclose without consent. This is
> consistent with guidance from the Information Commissioner's Office
> that information may relate to more than one person and that
> third-party data does not have to be disclosed unless the other
> person consents or it is reasonable to disclose without consent.
>
> **Duplicates**
>
> Duplicate and substantially duplicate records have not been reproduced
> multiple times in the disclosure bundle. Where the same item appeared
> in more than one source or mailbox, we have generally provided a
> single representative copy unless there was a material difference
> between versions.
>
> **Excluded material**
>
> Some material was excluded from the disclosure bundle because it did
> not contain personal data relating to you, was duplicative, fell
> outside the scope of the request, or was subject to redaction or
> withholding under an applicable exemption. Where your name or contact
> details appeared incidentally in business records, but the content did
> not relate to you as an individual, those records were not included in
> full. This approach is consistent with the principle that the right of
> access is to personal data relating to the requester, not necessarily
> to every whole document in which the requester's name appears.
>
> {leak_paragraph}
>
> **Redactions and exemptions**
>
> Redactions have been applied where necessary to protect third-party
> personal data, confidential information, legally privileged material,
> or other information exempt from disclosure under applicable data
> protection law. Where your own business identifiers — such as your
> trading name, consulting role, professional title, and business
> address — appeared in the material, those have been preserved as
> personal data relating to you, not redacted as third-party content.
>
> **Categories of personal data, purposes, recipients, retention**
>
> The personal data included in the disclosure bundle may include the
> following categories, where applicable: identification and contact
> information; employment or contractor-engagement information;
> communications involving or referring to you; system or workflow
> records (such as Salesforce, Workday, or timesheet entries); records
> of decisions, actions, or interactions involving you.
>
> The purposes for which this data is processed include contractor
> engagement and management, operational and project administration,
> legal and compliance obligations, business communications, audit and
> governance, information security, and record keeping.
>
> Your personal data may have been shared with internal departments,
> authorised employees, service providers acting on the controller's
> instructions, professional advisers, regulators where required by law,
> and clients in the course of engagements requiring your name to appear
> in deliverables.
>
> We retain personal data in accordance with our applicable retention
> policies and legal, regulatory, contractual, and operational
> requirements. Specific retention periods vary depending on the type of
> record and the purpose for which it is held. **[insert specific
> retention periods if controller policy requires]**
>
> **Your rights**
>
> You have the right to request rectification of inaccurate personal
> data, erasure where applicable, restriction of processing, and to
> object to processing in certain circumstances. You also have the right
> to complain to the Information Commissioner's Office (https://ico.org.uk)
> if you are dissatisfied with how your request has been handled.
>
> We now consider your data subject access request closed{closing_caveat}.
>
> Yours sincerely,
>
> **[Name]**
> **[Role]**
> **{controller}**

---

## Pre-send checklist

This auto-draft updates whenever case state changes. Operator must still:

1. [ ] Identity verification document/record attached to the case file
2. [ ] Statutory deadline recorded in `working/case_context.json`
3. [ ] {f['pii_flag_for_review']:,} flag-for-review PII entities triaged
4. [ ] {f['leak_failures_canonical']:,} leak-failure documents resolved (remediated, supplementary, or withheld with documented exemption)
5. [ ] This response letter finalised (name, role, retention specifics)
6. [ ] Internal report drafted
7. [ ] Metadata-strip audit on the redacted/ tree
8. [ ] Disclosure pack filtered to the **{f['final_disclosure_items']:,} canonical biographical refs** (drop duplicate copies + leak-failures)
9. [ ] DSAR Approver re-run after blockers resolved; verdict APPROVE_FOR_HUMAN_SIGNOFF
10. [ ] DPO / Privacy Lead sign-off recorded
"""
