#!/usr/bin/env python3
"""Detect drift between dsar-orchestrator stage definitions and the operator docs.

Compares STAGE_ORDER (src/dsar_orchestrator/pipeline.py) against:
  - docs/runbooks/dsar-operator-loop.md (stage ladder rows: every
    non-internal stage must appear as `--through <stage>`)
  - docs/operator-guide.md + the runbook ("N-stage pipeline" / "N-stage map"
    phrasing must match len(STAGE_ORDER))

Exits 0 if no drift, 1 if drift detected. Intended for CI; runnable locally.

Scope: orchestrator-side only. Toolkit-side stage definitions live in a
separate repo (dsar-toolkit) and need their own drift check; the file paths
here will not see those changes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dsar_orchestrator.pipeline import STAGE_ORDER  # noqa: E402

# Stages deliberately not surfaced as operator checkpoints in the runbook.
# Parallel-group containers and intermediate classifiers — the operator
# advances past them automatically via the surrounding --through targets.
RUNBOOK_INTERNAL_STAGES: frozenset[str] = frozenset(
    {
        "stage_2_parallel",
        "stage_3_parallel",
        "pii_classify",
    }
)

EXPECTED_RUNBOOK_STAGES: tuple[str, ...] = tuple(
    s for s in STAGE_ORDER if s not in RUNBOOK_INTERNAL_STAGES
)

RUNBOOK_PATH = REPO_ROOT / "docs" / "runbooks" / "dsar-operator-loop.md"
OPERATOR_GUIDE_PATH = REPO_ROOT / "docs" / "operator-guide.md"
STAGE_COUNT_TARGETS: tuple[Path, ...] = (OPERATOR_GUIDE_PATH, RUNBOOK_PATH)


def _check_runbook_ladder() -> list[str]:
    """Every non-internal stage must appear as `--through <stage>` in the runbook."""
    if not RUNBOOK_PATH.is_file():
        return [f"{RUNBOOK_PATH.relative_to(REPO_ROOT)}: file missing"]

    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    found = set(re.findall(r"--through\s+(\w+)", text))
    drifts: list[str] = []

    missing = [s for s in EXPECTED_RUNBOOK_STAGES if s not in found]
    extra = [s for s in found if s not in STAGE_ORDER and s != "<stage>"]

    if missing:
        drifts.append(
            f"{RUNBOOK_PATH.relative_to(REPO_ROOT)}: stage ladder is missing "
            f"`--through` rows for: {', '.join(missing)}"
        )
    if extra:
        drifts.append(
            f"{RUNBOOK_PATH.relative_to(REPO_ROOT)}: stage ladder references "
            f"stages not in STAGE_ORDER: {', '.join(sorted(extra))}"
        )
    return drifts


def _check_stage_count_claims() -> list[str]:
    """`N-stage pipeline` / `N-stage map` phrasing must match len(STAGE_ORDER)."""
    pattern = re.compile(r"(\d+)-stage\b")
    expected = len(STAGE_ORDER)
    drifts: list[str] = []
    for path in STAGE_COUNT_TARGETS:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        seen: set[int] = set()
        for m in pattern.finditer(text):
            n = int(m.group(1))
            if n == expected or n in seen:
                continue
            seen.add(n)
            drifts.append(
                f"{path.relative_to(REPO_ROOT)}: claims {n}-stage pipeline, "
                f"but STAGE_ORDER has {expected}"
            )
    return drifts


def main() -> int:
    drifts: list[str] = []
    drifts.extend(_check_runbook_ladder())
    drifts.extend(_check_stage_count_claims())

    if not drifts:
        print(
            f"runbook drift check: ok "
            f"({len(STAGE_ORDER)} stages, "
            f"{len(EXPECTED_RUNBOOK_STAGES)} operator checkpoints)"
        )
        return 0

    print("runbook drift detected:", file=sys.stderr)
    for d in drifts:
        print(f"  - {d}", file=sys.stderr)
    print(
        "\nFix the listed docs. If you intentionally added a new internal "
        "stage that should not appear as an operator checkpoint, add it to "
        "RUNBOOK_INTERNAL_STAGES in this script.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
