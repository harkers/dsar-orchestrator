"""``dsar-conductor`` — operator CLI for the orchestrator.

Argparse subparsers (spec §10.2):

  - ``run`` (default): orchestrate a case run. ``dsar-conductor --case X``
    (no subcommand) is preserved as the historic invocation and routes
    to ``run`` transparently.
  - ``verify``: post-hoc audit-row checks
    (``--check prompt-versions | fitness-report``;
    optional ``--strict``).

Flags on ``run``:
  - ``--auto-fitness``: on a missing/stale/failing fitness report, run
    ``dsar-fitness-canary`` inline before proceeding (spec §4.4 F).
  - ``--force-skip-fitness "<non-blank reason>"``: bypass + record
    ``case_audit/skip_fitness.json``; empty reason is rejected.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dsar_orchestrator import __version__
from dsar_orchestrator.exceptions import DSARPipelineError, PipelineHalt
from dsar_orchestrator.pipeline import ALL_STAGE_NAMES, STAGE_ORDER, run
from dsar_orchestrator.verify import verify_fitness_report, verify_prompt_versions


_KNOWN_SUBCOMMANDS = ("run", "verify")


def _add_run_args(p: argparse.ArgumentParser) -> None:
    """Shared args for the ``run`` subcommand."""
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
        help="Override case directory root.",
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
        "--force",
        action="store_true",
        help=(
            "Disable the resume cascade — run every in-scope stage even "
            "if its artefacts look fresh."
        ),
    )
    p.add_argument(
        "--acknowledge-issues",
        action="store_true",
        help=("Clear any analyser block flag from a prior `dsar-analyse-logs` run and proceed."),
    )
    p.add_argument(
        "--resolve-flags-as",
        choices=("true", "false"),
        default=None,
        metavar="<true|false>",
        help=(
            "Operator opt-in: auto-resolve all detect-stage 'flag' entries "
            "to redact:true|false before bake. See #26."
        ),
    )
    # Phase 5 additions (spec §4.4 F).
    p.add_argument(
        "--auto-fitness",
        action="store_true",
        help=(
            "On missing/stale/failing fitness report, run "
            "`dsar-fitness-canary` inline then proceed."
        ),
    )
    p.add_argument(
        "--force-skip-fitness",
        metavar="<reason>",
        default=None,
        help=("Bypass fitness pre-flight + record audit. Reason must be non-blank."),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dsar-conductor",
        description="Orchestrate a DSAR case run through the dsar-toolkit modular pipeline.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"dsar-conductor (dsar_orchestrator) {__version__}",
    )

    sub = p.add_subparsers(dest="cmd", required=False)

    # ``run`` — the default subcommand.
    p_run = sub.add_parser("run", help="Orchestrate a case run (default).")
    _add_run_args(p_run)

    # ``verify`` — post-hoc audit checks.
    p_verify = sub.add_parser("verify", help="Verify audit rows / fitness report.")
    p_verify.add_argument(
        "--case",
        required=True,
        metavar="<case-no>",
        help="Case number (resolves to ~/dsars/cases/<case-no>/).",
    )
    p_verify.add_argument(
        "--case-root",
        type=Path,
        default=None,
        metavar="<path>",
        help="Override case directory root.",
    )
    p_verify.add_argument(
        "--check",
        required=True,
        choices=("prompt-versions", "fitness-report", "people-register"),
        help="What to verify.",
    )
    p_verify.add_argument(
        "--strict",
        action="store_true",
        help="Upgrade warnings (older-but-registered versions) to errors.",
    )

    return p


def _resolve_case_path(case_no: str, case_root: Path | None) -> Path:
    return case_root or (Path.home() / "dsars" / "cases" / case_no)


def _inline_fitness_canary(case_no: str, case_root: Path) -> int:
    """Best-effort ``dsar-fitness-canary`` invocation for ``--auto-fitness``.

    Reads ``fitness_check_deployment_id`` from case_config.json; falls
    back to ``DSAR_DEPLOYMENT_ID`` env. Returns the subprocess exit code.
    """
    import json as _json

    cfg_path = case_root / "case_config.json"
    deployment_id = ""
    if cfg_path.is_file():
        try:
            cfg_raw = _json.loads(cfg_path.read_text(encoding="utf-8"))
            deployment_id = cfg_raw.get("fitness_check_deployment_id", "") or ""
        except (OSError, _json.JSONDecodeError):
            pass
    if not deployment_id:
        deployment_id = os.environ.get("DSAR_DEPLOYMENT_ID", "")
    if not deployment_id:
        print(
            "--auto-fitness: no fitness_check_deployment_id in case_config.json",
            file=sys.stderr,
        )
        return 2
    proc = subprocess.run(
        ["dsar-fitness-canary", "--deployment-id", deployment_id],
        check=False,
    )
    return proc.returncode


def _dispatch_run(args: argparse.Namespace) -> int:
    if args.only_stage and (args.from_stage or args.through_stage):
        print(
            "--only is mutually exclusive with --from / --through",
            file=sys.stderr,
        )
        return 2

    # Validate --force-skip-fitness: empty / whitespace-only → reject.
    if args.force_skip_fitness is not None:
        if not args.force_skip_fitness.strip():
            print(
                "--force-skip-fitness requires a non-blank reason",
                file=sys.stderr,
            )
            return 2
        os.environ["DSAR_FORCE_SKIP_FITNESS_REASON"] = args.force_skip_fitness

    if args.resolve_flags_as is not None:
        os.environ["DSAR_RESOLVE_FLAGS_AS"] = args.resolve_flags_as

    # --auto-fitness: try canary inline. Best-effort — the pre-flight
    # will re-evaluate after canary writes its report.
    if args.auto_fitness:
        rc = _inline_fitness_canary(
            args.case,
            _resolve_case_path(args.case, args.case_root),
        )
        if rc != 0:
            print(
                f"auto-fitness canary returned {rc}; pre-flight may still halt.",
                file=sys.stderr,
            )

    try:
        run(
            case_no=args.case,
            case_root=args.case_root,
            from_stage=args.from_stage,
            through_stage=args.through_stage,
            only_stage=args.only_stage,
            dry_run=args.dry_run,
            check=args.check,
            force=args.force,
            acknowledge_issues=args.acknowledge_issues,
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


def _dispatch_verify(args: argparse.Namespace) -> int:
    case_path = _resolve_case_path(args.case, args.case_root)
    if args.check == "prompt-versions":
        result = verify_prompt_versions(case_path, strict=args.strict)
    elif args.check == "fitness-report":
        result = verify_fitness_report(case_path)
    elif args.check == "people-register":
        from dsar_orchestrator.verify import verify_people_register

        outcome = verify_people_register(case_path)
        mark = "✓" if outcome["ok"] else "✗"
        print(f"{mark} {outcome['message']}")
        return 0 if outcome["ok"] else 1
    else:
        print(f"unknown --check {args.check!r}", file=sys.stderr)
        return 2
    for w in result.warnings:
        print(f"WARN: {w}", file=sys.stderr)
    for e in result.errors:
        print(f"ERROR: {e}", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(argv) if argv is not None else sys.argv[1:]

    # Back-compat: missing subcommand → default to ``run``.
    # We detect "no subcommand" by checking whether argv[0] is one of
    # our known subcommand names. Anything else (empty argv, a leading
    # flag) → prepend ``run``. The lone exception is ``--version`` /
    # ``-h`` / ``--help`` which need to be handled by the top-level
    # parser; we let them through.
    top_level_flags = {"--version", "-h", "--help"}
    if not argv or (argv[0].startswith("-") and argv[0] not in top_level_flags):
        argv = ["run", *argv]
    elif argv[0] not in _KNOWN_SUBCOMMANDS and not argv[0].startswith("-"):
        # Positional that isn't a known subcommand → assume it's a
        # historic call. (Shouldn't happen with --case-style args but
        # safe default.)
        argv = ["run", *argv]

    args = parser.parse_args(argv)

    if args.cmd == "run" or args.cmd is None:
        return _dispatch_run(args)
    if args.cmd == "verify":
        return _dispatch_verify(args)
    parser.error(f"unknown subcommand: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
