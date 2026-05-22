"""Synthetic-case generator — deterministic, seeded, library + CLI.

``synthesize_case(case_no, out_dir, doc_count=100, seed=...)`` produces
a complete case directory at ``out_dir/<case_no>/`` with:

- ``source/<case_no>-NNNN.txt`` — N text documents
- ``case_config.json`` — pre-populated config with the synthetic
  data subject
- ``synthetic_truth.json`` — the answer key (per-doc category +
  truth class) for integration-test assertions

The function is deterministic: same ``case_no`` + ``seed`` → same
bytes every run. This is what makes it usable as a CI fixture.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dsar_orchestrator.synthesis.templates import (
    BYSTANDERS,
    DECOY_PEOPLE,
    DEPARTMENTS_OUT_OF_SCOPE,
    DOC_MIX,
    FINANCE_PEERS,
    SUBJECT_ALIASES,
    SUBJECT_DEPT,
    SUBJECT_DOB,
    SUBJECT_EMAIL,
    SUBJECT_EMPLOYEE_ID,
    SUBJECT_NAME,
    SUBJECT_ROLE,
    TEMPLATES,
)

DEFAULT_DOC_COUNT = 100


@dataclass
class SyntheticCase:
    """Output of ``synthesize_case``. Carries the on-disk path + a
    summary of what was generated, useful for test assertions."""

    case_no: str
    case_path: Path
    doc_count: int
    by_truth_class: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)


def synthesize_case(
    case_no: str,
    out_dir: Path,
    *,
    doc_count: int = DEFAULT_DOC_COUNT,
    seed: int | None = None,
) -> SyntheticCase:
    """Generate a synthetic case under ``out_dir/<case_no>/``.

    Deterministic on (case_no, seed). If ``seed`` is None the case_no
    is parsed as an integer fallback (e.g., "800001" → 800001) so
    re-running with the same case number produces identical output.
    """
    if seed is None:
        try:
            seed = int("".join(c for c in case_no if c.isdigit()) or "0")
        except ValueError:
            seed = 0
    rng = random.Random(seed)

    case_path = out_dir / case_no
    source_dir = case_path / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    (case_path / "working").mkdir(exist_ok=True)
    (case_path / "redacted").mkdir(exist_ok=True)
    (case_path / "output").mkdir(exist_ok=True)

    # Resolve the doc-mix to a flat list of (category, truth_class)
    # for `doc_count` docs. If doc_count != 100, scale the buckets
    # proportionally with rounding.
    plan: list[tuple[str, str]] = []
    if doc_count == DEFAULT_DOC_COUNT:
        for category, count, truth in DOC_MIX:
            plan.extend([(category, truth)] * count)
    else:
        scale = doc_count / DEFAULT_DOC_COUNT
        for category, count, truth in DOC_MIX:
            n = max(1, round(count * scale))
            plan.extend([(category, truth)] * n)
        # Trim or pad to exactly doc_count.
        plan = plan[:doc_count]
        while len(plan) < doc_count:
            plan.append(("off-building", "off_topic"))

    rng.shuffle(plan)

    by_truth: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    truth_rows: list[dict[str, Any]] = []

    for i, (category, truth_class) in enumerate(plan, start=1):
        ref = f"{case_no}-{i:04d}"
        body = _render_doc(category, ref, rng)
        (source_dir / f"{ref}.txt").write_text(body, encoding="utf-8")
        truth_rows.append(
            {
                "ref": ref,
                "category": category,
                "truth_class": truth_class,
                "filename": f"source/{ref}.txt",
            }
        )
        by_truth[truth_class] = by_truth.get(truth_class, 0) + 1
        by_cat[category] = by_cat.get(category, 0) + 1

    # case_config.json
    config = {
        "case_no": case_no,
        "case_scope": (
            f"All personal data about {SUBJECT_NAME}, {SUBJECT_ROLE} in "
            f"the {SUBJECT_DEPT} department, employed 2022 to 2025. "
            f"Includes emails to or from him, performance reviews, "
            f"salary records, and meeting notes mentioning him "
            f"biographically."
        ),
        "subject_identifier": {
            "primary_name": SUBJECT_NAME,
            "dob": SUBJECT_DOB,
            "employee_id": SUBJECT_EMPLOYEE_ID,
            "aliases": SUBJECT_ALIASES,
            "disambiguation_notes": (
                f"Subject: {SUBJECT_ROLE}, {SUBJECT_DEPT}, 2022-2025. "
                f"Decoys present in corpus: " + ", ".join(p["name"] for p in DECOY_PEOPLE) + "."
            ),
        },
        "rerank_mode": "shadow",
        "rerank_threshold": 0.01,
        "pii_classify_mode": "shadow",
        "pii_budget_usd": 5.0,
        "synthetic": True,
        "synthetic_seed": seed,
    }
    (case_path / "case_config.json").write_text(json.dumps(config, indent=2))

    # synthetic_truth.json — the integration-test answer key.
    truth = {
        "case_no": case_no,
        "doc_count": doc_count,
        "seed": seed,
        "by_truth_class": by_truth,
        "by_category": by_cat,
        "rows": truth_rows,
    }
    (case_path / "synthetic_truth.json").write_text(json.dumps(truth, indent=2))

    return SyntheticCase(
        case_no=case_no,
        case_path=case_path,
        doc_count=doc_count,
        by_truth_class=by_truth,
        by_category=by_cat,
    )


# ─── doc rendering ────────────────────────────────────────────────


def _render_doc(category: str, ref: str, rng: random.Random) -> str:
    """Pick a template for the category, fill slots from the RNG."""
    template = rng.choice(TEMPLATES[category])
    slots = _gen_slots(rng, ref)
    return template.format(**slots) + f"\n\n[doc-ref: {ref}]\n"


def _gen_slots(rng: random.Random, ref: str) -> dict[str, Any]:
    """All slots templates may reference — over-provision so any
    template can format()."""
    peer1, peer2, peer3 = rng.sample(FINANCE_PEERS, 3)
    bystander1, bystander2 = rng.sample(BYSTANDERS, 2)
    decoy = rng.choice(DECOY_PEOPLE)
    year = rng.choice([2022, 2023, 2024, 2025])
    q = rng.choice([1, 2, 3, 4])
    month = rng.choice(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
    )
    date = f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    next_date = f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    floor = rng.randint(2, 14)
    nights = rng.randint(1, 4)
    salary = rng.choice([62_500, 71_200, 78_400, 84_900, 92_500, 105_000])
    bonus = rng.choice([8, 10, 12, 15])
    rating = rng.choice(["Strong", "Solid", "Exceeds", "Meets"])
    exp_a = rng.randint(120, 380)
    exp_b = rng.randint(180, 600)
    exp_c = rng.randint(50, 200)
    service = rng.choice(["SSO", "expense system", "intranet portal", "VPN"])
    discount = rng.choice([5, 8, 10, 15, 20])
    deadline = f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    join_year = rng.choice([2020, 2021, 2022])
    amendment_no = rng.randint(1, 4)
    decoy_tenure = rng.choice([5, 7, 9])

    return {
        # Subject
        "subject_name": SUBJECT_NAME,
        "subject_first": SUBJECT_NAME.split()[0],
        "subject_email": SUBJECT_EMAIL,
        "subject_dob": SUBJECT_DOB,
        "subject_id": SUBJECT_EMPLOYEE_ID,
        "subject_dept": SUBJECT_DEPT,
        "subject_role": SUBJECT_ROLE,
        # Decoy James
        "decoy_name": decoy["name"],
        "decoy_first": decoy["name"].split()[0],
        "decoy_email": decoy["email"],
        "decoy_dept": decoy["dept"],
        "decoy_tenure": decoy_tenure,
        # Peers (third parties — Finance dept)
        "peer1_name": peer1,
        "peer1_first": peer1.split()[0],
        "peer1_email": _email_for(peer1),
        "peer2_name": peer2,
        "peer2_first": peer2.split()[0],
        "peer2_email": _email_for(peer2),
        "peer3_name": peer3,
        "peer3_first": peer3.split()[0],
        "peer3_email": _email_for(peer3),
        # HR + bystanders
        "hr_name": bystander1,
        "hr_first": bystander1.split()[0],
        "bystander_name": bystander2,
        # Scalars
        "year": year,
        "q": q,
        "month": month,
        "date": date,
        "next_date": next_date,
        "date_a": date,
        "date_b": next_date,
        "date_c": f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        "floor": floor,
        "nights": nights,
        "salary": salary,
        "bonus_pct": bonus,
        "rating": rating,
        "exp_a": exp_a,
        "exp_b": exp_b,
        "exp_c": exp_c,
        "exp_total": exp_a + exp_b + exp_c,
        "service": service,
        "discount": discount,
        "deadline": deadline,
        "join_year": join_year,
        "amendment_no": amendment_no,
        # Out-of-scope-department flavour
        "off_dept": rng.choice(DEPARTMENTS_OUT_OF_SCOPE),
        "ref": ref,
    }


def _email_for(full_name: str) -> str:
    """Best-effort email from a name."""
    parts = full_name.lower().split()
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[-1]}@acme.test"
    return f"{parts[0]}@acme.test"
