"""``dsar-analyse-logs`` — operator CLI for the log analyser."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dsar_orchestrator import __version__
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.log_analyser.client import (
    DEFAULT_BROKER_URL,
    DEFAULT_MODEL_ALIAS,
)
from dsar_orchestrator.log_analyser.core import (
    analyse_case,
    clear_block,
    is_blocked,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dsar-analyse-logs",
        description=(
            "Run the local-LLM (mlx-broker) analyser over a case's "
            "audit logs. Stays on the box; no external API calls."
        ),
    )
    p.add_argument(
        "--case",
        required=True,
        metavar="<case-no>",
        help="Case number whose audit logs to analyse.",
    )
    p.add_argument(
        "--model",
        default=None,
        metavar="<alias>",
        help=(
            f"mlx-broker model alias (default: {DEFAULT_MODEL_ALIAS!r}; "
            f"override via DSAR_ANALYSER_MODEL env var)."
        ),
    )
    p.add_argument(
        "--broker-url",
        default=None,
        metavar="<url>",
        help=(
            f"mlx-broker URL (default: {DEFAULT_BROKER_URL}; override via MLX_BROKER_URL env var)."
        ),
    )
    p.add_argument(
        "--audit-root",
        type=Path,
        default=None,
        metavar="<path>",
        help="Override the audit root directory. Default: ~/.dsar-audit/",
    )
    p.add_argument(
        "--check-block",
        action="store_true",
        help=(
            "Don't run the analyser; just check whether the case is "
            "currently under an analyser block. Exit 0 if not blocked, "
            "1 if blocked."
        ),
    )
    p.add_argument(
        "--clear-block",
        action="store_true",
        help=("Clear an existing analyser block for the case (operator acknowledgement)."),
    )
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Don't persist analysis.jsonl/md/flag; print only.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"dsar-analyse-logs (dsar_orchestrator) {__version__}",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check_block:
        if is_blocked(args.case, audit_root=args.audit_root):
            print(f"BLOCKED: case={args.case} has a pending analyser block.", file=sys.stderr)
            return 1
        print(f"OK: case={args.case} is not blocked.")
        return 0

    if args.clear_block:
        clear_block(args.case, audit_root=args.audit_root)
        print(f"Cleared analyser block for case={args.case}.")
        return 0

    try:
        report = analyse_case(
            args.case,
            audit_root=args.audit_root,
            model_alias=args.model,
            broker_url=args.broker_url,
            write_outputs=not args.no_write,
        )
    except DSARPipelineError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    print(report.render_markdown())

    if report.has_blocking_issues:
        print(
            f"\nBLOCK: {len(report.critical)} critical finding(s); "
            f"next `dsar-conductor --case {args.case}` will refuse to "
            f"start without --acknowledge-issues.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
