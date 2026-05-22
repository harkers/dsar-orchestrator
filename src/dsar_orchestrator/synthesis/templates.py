"""Document templates + name banks for the synthetic-case generator.

Each template is parameterised; the generator fills slots with random
(but deterministic, seeded) fake values. Categories drive the truth
labels in ``synthetic_truth.json``.

Document classes:
- ``gold`` — clearly in-scope, biographically about the data subject
- ``mid`` — in-scope but tangential (subject CC'd, in a thread, etc.)
- ``decoy`` — another person with the same first name; lexically
  similar but out of scope
- ``off_topic_finance`` — Finance department docs that don't mention
  the subject
- ``off_topic`` — completely unrelated (building, IT, etc.)
"""

from __future__ import annotations

# The data subject (the case is about this person).
SUBJECT_NAME = "James Carter"
SUBJECT_EMAIL = "james.carter@acme.test"
SUBJECT_DOB = "1985-03-12"
SUBJECT_EMPLOYEE_ID = "FIN-0241"
SUBJECT_DEPT = "Finance"
SUBJECT_ROLE = "Senior Analyst"
SUBJECT_ALIASES = ["J. Carter", "Jim Carter", "JC"]

# Decoys — other Jameses to confuse purely-lexical matching.
DECOY_PEOPLE = [
    {"name": "James Marshall", "email": "j.marshall@acme.test", "dept": "Operations"},
    {"name": "James Lee", "email": "james.lee@acme.test", "dept": "Sales"},
    {"name": "James Smith", "email": "j.smith@acme.test", "dept": "Engineering"},
]

# Other Finance department people (third parties).
FINANCE_PEERS = [
    "Sarah Lin",
    "Tom Reeves",
    "Priya Khan",
    "Daniel Okafor",
    "Beatrice Romano",
    "Hiroshi Tanaka",
]

# Bystanders for general "thread members" + signatures.
BYSTANDERS = [
    "Alex Yim",
    "Marina Cole",
    "Diego Vasquez",
    "Imani Roberts",
    "Sven Karlsson",
    "Anya Petrov",
]

DEPARTMENTS_OUT_OF_SCOPE = [
    "Marketing",
    "Engineering",
    "Operations",
    "Sales",
    "HR",
    "Legal",
]

# Body templates — each (category, list of templates).
TEMPLATES: dict[str, list[str]] = {
    # ─── GOLD (about the data subject directly) ────────────────────
    "gold-perf-review": [
        (
            "Performance Review — {year} Q{q}\n"
            "Employee: {subject_name} ({subject_role}, {subject_dept})\n"
            "Employee ID: {subject_id}\n"
            "Reviewer: {hr_name}\n\n"
            "Summary: {subject_first} delivered the quarterly close on "
            "schedule. Stakeholder management remains an area for "
            "development. Recommended for promotion review next cycle.\n"
            "Rating: {rating}\n"
            "Salary band proposal: L5 (£{salary:,})\n"
        ),
    ],
    "gold-salary-letter": [
        (
            "From: payroll@acme.test\n"
            "To: {subject_email}\n"
            "Subject: Salary adjustment — effective {month} {year}\n\n"
            "Dear {subject_first},\n\n"
            "Following the recent compensation review, your annual "
            "base salary will be adjusted to £{salary:,}, with a bonus "
            "eligibility of {bonus_pct}%, effective {month} {year}.\n\n"
            "Employee ID: {subject_id}. DOB on file: {subject_dob}.\n\n"
            "HR\n"
        ),
    ],
    "gold-authored-email": [
        (
            "From: {subject_email}\n"
            "To: finance-leadership@acme.test\n"
            "Subject: Q{q} budget review — my notes\n\n"
            "All,\n\n"
            "Notes from yesterday's Q{q} budget review: pipeline "
            "shortfall against forecast (see attached spreadsheet). "
            "I'll walk through on Monday's call.\n\n"
            "Best,\n{subject_name}\n{subject_role}, {subject_dept}\n"
        ),
        (
            "From: {subject_email}\n"
            "To: {peer1_email}\n"
            "Subject: Re: audit follow-up\n\n"
            "{peer1_first} — I can take the lead on the {month} audit "
            "follow-up. Drafting a note for {hr_name} now.\n\n"
            "{subject_first}\n"
        ),
    ],
    "gold-expense-report": [
        (
            "Expense Report — {month} {year}\n"
            "Filed by: {subject_name} ({subject_id})\n"
            "Department: {subject_dept}\n\n"
            "Travel to client site (London): £{exp_a:,}\n"
            "Hotel ({nights} nights): £{exp_b:,}\n"
            "Subsistence: £{exp_c:,}\n"
            "Total: £{exp_total:,}\n\n"
            "Approver: {hr_name}\n"
        ),
    ],
    "gold-promotion-notice": [
        (
            "Internal Announcement — {month} {year}\n\n"
            "We are pleased to announce that {subject_name} has been "
            "promoted to {subject_role}, effective immediately. "
            "{subject_first} has been with {subject_dept} since "
            "{join_year} and has consistently demonstrated strong "
            "delivery on the quarterly close cycle.\n\n"
            "Please join us in congratulating {subject_first}.\n"
        ),
    ],
    "gold-meeting-notes": [
        (
            "Meeting notes — {subject_dept} weekly, {date}\n\n"
            "Attendees: {hr_name} (chair), {subject_name}, "
            "{peer1_name}, {peer2_name}\n\n"
            "{subject_first} reported on the {month} close: ahead of "
            "schedule on receivables, behind on the supplier "
            "reconciliation. Action: {subject_first} to circulate the "
            "supplier list by {next_date}.\n"
        ),
    ],
    "gold-contract": [
        (
            "Employment Contract — Amendment {amendment_no}\n"
            "Employee: {subject_name}\n"
            "Employee ID: {subject_id}\n"
            "DOB: {subject_dob}\n"
            "Department: {subject_dept}\n"
            "Role: {subject_role}\n"
            "Effective date: {date}\n\n"
            "This amendment updates the compensation terms to: base "
            "£{salary:,}, bonus {bonus_pct}% of base, six month notice.\n"
        ),
    ],
    # ─── MID (tangential — subject CC'd or in thread) ──────────────
    "mid-cc-policy": [
        (
            "{subject_dept} policy — expense approvals (revised {month} {year})\n\n"
            "All expense claims above £500 now require director sign-off. "
            "Distribution: finance-all@acme.test\n"
            "cc: {subject_name}, {peer1_name}, {peer2_name}, {peer3_name}\n"
        ),
    ],
    "mid-thread-bystander": [
        (
            "From: {peer1_email}\n"
            "To: {hr_name}@acme.test\n"
            "cc: {subject_email}, {peer2_email}\n"
            "Subject: Re: Q{q} forecast meeting\n\n"
            "{hr_first} — happy with the proposed slot. {subject_first}, "
            "are you OK to present the receivables piece?\n\n"
            "{peer1_first}\n"
        ),
    ],
    "mid-finance-allhands": [
        (
            "Finance dept all-hands — {date}\n\n"
            "{hr_name} opened with the {month} numbers. {peer1_name} "
            "covered supplier risk. {subject_name} sat in but did not "
            "present this round. {peer2_name} flagged a year-end "
            "deadline shift.\n"
        ),
    ],
    # ─── DECOY (other people named James) ──────────────────────────
    "decoy-marshall-promotion": [
        (
            "HR Notice — {month} {year}\n\n"
            "{decoy_name} has been promoted to Director of "
            "{decoy_dept}, effective {date}. James joins the "
            "leadership team after {decoy_tenure} years in regional "
            "management.\n\n"
            "Congratulations to {decoy_first}.\n"
        ),
    ],
    "decoy-james-email": [
        (
            "From: {decoy_email}\n"
            "To: {peer1_email}\n"
            "Subject: Re: {decoy_dept} pipeline update\n\n"
            "Thanks {peer1_first} — looks good from this end. "
            "{decoy_dept} side is on track for the quarter.\n\n"
            "{decoy_first}\n"
        ),
    ],
    "decoy-perf-review": [
        (
            "Performance Review — {year} Q{q}\n"
            "Employee: {decoy_name} ({decoy_dept})\n"
            "Reviewer: {hr_name}\n\n"
            "{decoy_first} delivered well on the regional accounts. "
            "Rating: {rating}.\n"
        ),
    ],
    # ─── OFF-TOPIC FINANCE (lexically similar but no subject) ──────
    "off-finance-perf": [
        (
            "Performance Review — {year} Q{q}\n"
            "Employee: {peer1_name} (Finance)\n"
            "Reviewer: {hr_name}\n\n"
            "{peer1_first} has been a consistent over-performer this "
            "year. Rating: {rating}. Recommended for L5.\n"
        ),
    ],
    "off-finance-policy": [
        (
            "Finance dept procurement policy (revised {month} {year})\n\n"
            "All vendor onboarding now requires legal sign-off. "
            "Contact: {peer1_name} for procurement, {peer2_name} for "
            "legal liaison.\n"
        ),
    ],
    "off-finance-other-salary": [
        (
            "From: payroll@acme.test\n"
            "To: {peer1_email}\n"
            "Subject: Annual compensation review\n\n"
            "Dear {peer1_first}, following the annual review, your "
            "base salary has been adjusted to £{salary:,}, effective "
            "{date}.\n\nHR\n"
        ),
    ],
    # ─── OFF-TOPIC (completely unrelated) ──────────────────────────
    "off-building": [
        (
            "Building Maintenance Notice\n\n"
            "Carpet replacement on Floor {floor} is scheduled for the "
            "weekend of {date}. Access will be restricted from 18:00 "
            "Friday until 09:00 Monday. Contractor: Carpet Right "
            "Commercial.\n"
        ),
    ],
    "off-it-outage": [
        (
            "IT Notice — Planned Outage\n\n"
            "The {service} system will be offline on {date} from 02:00 "
            "to 04:00 GMT for security patching. No action required "
            "from end users. Affected services: SSO, email, intranet.\n"
        ),
    ],
    "off-holiday": [
        (
            "Holiday Schedule — {year}\n\n"
            "Office closed: {date_a}, {date_b}, {date_c}. Please "
            "submit your annual leave requests via the HR portal by "
            "{deadline}.\n"
        ),
    ],
    "off-vendor": [
        (
            "From: sales@vendor-acme.test\n"
            "To: procurement@acme.test\n"
            "Subject: Q{q} promotional pricing\n\n"
            "Quarterly volume discount available on our enterprise "
            "tier — reach out by {date} to lock in {discount}% off "
            "standard rates.\n"
        ),
    ],
    "off-cafeteria": [
        (
            "Cafeteria Menu — Week of {date}\n\n"
            "Monday: vegetable curry, rice, naan.\n"
            "Tuesday: chicken caesar salad.\n"
            "Wednesday: pasta arrabbiata.\n"
            "Thursday: lentil dahl, basmati.\n"
            "Friday: fish & chips.\n"
        ),
    ],
}

# Category → (count in a 100-doc case, truth label)
DOC_MIX: list[tuple[str, int, str]] = [
    # gold (~30 docs)
    ("gold-perf-review", 4, "gold"),
    ("gold-salary-letter", 5, "gold"),
    ("gold-authored-email", 8, "gold"),
    ("gold-expense-report", 4, "gold"),
    ("gold-promotion-notice", 2, "gold"),
    ("gold-meeting-notes", 5, "gold"),
    ("gold-contract", 2, "gold"),
    # mid (~12 docs)
    ("mid-cc-policy", 4, "mid"),
    ("mid-thread-bystander", 5, "mid"),
    ("mid-finance-allhands", 3, "mid"),
    # decoy (~10 docs)
    ("decoy-marshall-promotion", 2, "decoy"),
    ("decoy-james-email", 5, "decoy"),
    ("decoy-perf-review", 3, "decoy"),
    # off-finance (~13 docs)
    ("off-finance-perf", 4, "off_finance"),
    ("off-finance-policy", 4, "off_finance"),
    ("off-finance-other-salary", 5, "off_finance"),
    # off-topic (~35 docs)
    ("off-building", 8, "off_topic"),
    ("off-it-outage", 6, "off_topic"),
    ("off-holiday", 6, "off_topic"),
    ("off-vendor", 8, "off_topic"),
    ("off-cafeteria", 7, "off_topic"),
]
# Total = 4+5+8+4+2+5+2 + 4+5+3 + 2+5+3 + 4+4+5 + 8+6+6+8+7 = 100 docs.
