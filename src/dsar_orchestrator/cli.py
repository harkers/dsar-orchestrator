"""``dsar-pipeline`` — operator CLI for the orchestrator.

Thin argparse wrapper around ``pipeline.run()``. Per the orchestration
spec, the CLI and the in-process orchestrator share a single
implementation; this module only handles argument parsing + presenting
errors to the operator.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dsar_orchestrator import __version__
from dsar_orchestrator.exceptions import DSARPipelineError, PipelineHalt
from dsar_orchestrator.pipeline import ALL_STAGE_NAMES, STAGE_ORDER, run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dsar-pipeline",
        description=("Orchestrate a DSAR case run through the dsar-toolkit modular pipeline."),
    )
    p.add_argument(
        "--case",
        required=True,
        metavar="<case-no>",
        help="Case number (resolves to ~/dsars/cases/<case-no>/).",
    )
    p.add_argument(
        "--case-root",
        type=Path,
        default=None,
        metavar="<path>",
        help="Override case directory root. Default: ~/dsars/cases/<case-no>/.",
    )
    p.add_argument(
        "--from",
        dest="from_stage",
        choices=STAGE_ORDER,
        default=None,
        help="Start from this stage; skip earlier ones.",
    )
    p.add_argument(
        "--through",
        dest="through_stage",
        choices=STAGE_ORDER,
        default=None,
        help="Stop after this stage; skip later ones.",
    )
    p.add_argument(
        "--only",
        dest="only_stage",
        choices=ALL_STAGE_NAMES,
        default=None,
        metavar="<stage>",
        help=(
            "Run only this stage; accepts coarse stages (e.g., "
            "stage_2_parallel) or sub-stages (e.g., embed, rerank). "
            "Mutually exclusive with --from / --through."
        ),
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Print the resume plan without running anything.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Same as --check; print the plan and exit.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"dsar-pipeline (dsar_orchestrator) {__version__}",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.only_stage and (args.from_stage or args.through_stage):
        parser.error("--only is mutually exclusive with --from / --through")

    try:
        run(
            case_no=args.case,
            case_root=args.case_root,
            from_stage=args.from_stage,
            through_stage=args.through_stage,
            only_stage=args.only_stage,
            dry_run=args.dry_run,
            check=args.check,
        )
    except PipelineHalt as e:
        print(f"\nPIPELINE HALTED: {e}", file=sys.stderr)
        return 2
    except DSARPipelineError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"\nCONFIG ERROR: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
