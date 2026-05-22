"""``dsar-synthesize-case`` — operator CLI for the synthetic-case generator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dsar_orchestrator import __version__
from dsar_orchestrator.synthesis.case import DEFAULT_DOC_COUNT, synthesize_case


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dsar-synthesize-case",
        description=(
            "Generate a deterministic synthetic DSAR case under "
            "<out-dir>/<case-no>/. Pure fake data; useful for "
            "pipeline smoke testing + CI integration tests."
        ),
    )
    p.add_argument(
        "--case-no",
        required=True,
        metavar="<no>",
        help="Case number (also seeds the RNG if --seed is not passed).",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        metavar="<path>",
        help="Output directory; case will go under <out-dir>/<case-no>/. Default: ~/dsars/cases/",
    )
    p.add_argument(
        "--doc-count",
        type=int,
        default=DEFAULT_DOC_COUNT,
        metavar="<n>",
        help=f"Number of documents to generate (default: {DEFAULT_DOC_COUNT}).",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="<int>",
        help=(
            "Explicit RNG seed; overrides the default behaviour of parsing the case-no for digits."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"dsar-synthesize-case (dsar_orchestrator) {__version__}",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = args.out_dir or (Path.home() / "dsars" / "cases")
    out_dir.mkdir(parents=True, exist_ok=True)

    result = synthesize_case(
        case_no=args.case_no,
        out_dir=out_dir,
        doc_count=args.doc_count,
        seed=args.seed,
    )

    print(f"Case {result.case_no} generated at {result.case_path}")
    print(f"  doc_count = {result.doc_count}")
    print("  by truth class:")
    for klass, n in sorted(result.by_truth_class.items(), key=lambda x: -x[1]):
        print(f"    {klass:<15} {n:>4}")
    print(f"  config: {result.case_path / 'case_config.json'}")
    print(f"  truth:  {result.case_path / 'synthetic_truth.json'}")
    print()
    print("To run the pipeline against this case:")
    print(f"  dsar-conductor --case {result.case_no} --case-root {result.case_path} --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
