"""Conductor-owned scope-classify adapter — Stage 4 sync barrier.

Bridges to the toolkit's ``dsar-scope-check`` CLI (entry:
``dsar_pipeline.scope_check_stage:main``). The toolkit calls this
"scope_check" but the conductor's Stage 4 maps to it 1:1: per-ref
scope determination (Durant biographical-focus + temporal window),
output ``working/scope_verdicts.jsonl``.

**Retirement contract.** Toolkit's ``ScopeCheckStage`` class is heavy
(claude-opus-4-7 + GateRunner + GateDurant deps) so we shell out to
the CLI instead of importing. When the toolkit ships a thin Python
entry shaped ``dsar_pipeline.scope_check_stage.run_for_case(case_path)``
(per the prioritised adapter list on toolkit issue #1), this adapter
retires.

The adapter writes ``working/scope_classify_complete.jsonl`` — the
cascade anchor the conductor's `STAGE_ARTEFACTS` registry expects —
on successful completion. The toolkit's own ``scope_verdicts.jsonl``
stays as the per-ref source of truth.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from dsar_orchestrator.config import CaseConfig
from dsar_orchestrator.exceptions import DSARPipelineError
from dsar_orchestrator.hash_chain import sha256_file

PRODUCER_VERSION = "dsar_orchestrator.adapters.scope_classify 0.4.9"
SCHEMA_VERSION = "1.0"
DEFAULT_CLI = "dsar-scope-check"

# Subprocess runner type — injectable for tests.
RunnerFn = Callable[[list[str], dict[str, str]], subprocess.CompletedProcess]


def _default_runner() -> RunnerFn:
    """Real subprocess invocation with env, capture, timeout."""

    def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,  # 1h — scope_check can be slow on large cases
            check=False,
        )

    return run


def run_for_case(
    cfg: CaseConfig,
    *,
    runner: RunnerFn | None = None,
    cli: str = DEFAULT_CLI,
) -> None:
    """Drive the toolkit's scope-check stage; write the cascade anchor.

    ``runner`` is injectable; defaults to a real subprocess call.
    """
    if runner is None:
        runner = _default_runner()

    # Toolkit's CLI resolves --case via $DSAR_CASE_ROOT/<id>. Set
    # the env so it lands on cfg.case_path.
    from dsar_orchestrator.subprocess_env import build_subprocess_env

    env = build_subprocess_env()
    env["DSAR_CASE_ROOT"] = str(cfg.case_path.parent)

    # Upstream artefact for the cascade depends on rerank mode.
    upstream_artefact = _upstream_artefact_for(cfg)
    if not upstream_artefact.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: scope-classify upstream missing at "
            f"{upstream_artefact}. Run the prefilter (and rerank if in "
            f"enforce mode) first."
        )

    argv = [cli, "--case", cfg.case_no]
    completed = runner(argv, env)

    if completed.returncode != 0:
        # Truncate noisy outputs but keep enough for debugging.
        stderr = (completed.stderr or "")[-2000:]
        raise DSARPipelineError(
            f"case={cfg.case_no}: scope-check CLI exited "
            f"{completed.returncode}. stderr tail:\n{stderr}"
        )

    verdicts_path = cfg.case_path / "working" / "scope_verdicts.jsonl"
    if not verdicts_path.exists():
        raise DSARPipelineError(
            f"case={cfg.case_no}: scope-check CLI succeeded but "
            f"scope_verdicts.jsonl was not produced at {verdicts_path}."
        )

    # Compute the cascade anchor.
    upstream_hash = sha256_file(upstream_artefact)
    summary = _summarise_verdicts(verdicts_path)
    _write_anchor(cfg.case_path, upstream_hash, summary)


def _upstream_artefact_for(cfg: CaseConfig) -> Path:
    """In enforce mode the upstream is scope_rerank.jsonl; otherwise
    it's cosine_prefilter.jsonl. Mirrors stages._hash_scope_inputs.
    """
    if cfg.rerank_mode == "enforce":
        return cfg.case_path / "working" / "scope_rerank.jsonl"
    return cfg.case_path / "working" / "cosine_prefilter.jsonl"


def _summarise_verdicts(path: Path) -> dict[str, int]:
    """Count verdicts by type for the anchor row. Raises on any
    malformed row — the toolkit emitting corrupt JSON in
    scope_verdicts.jsonl is a real bug we surface rather than
    swallow."""
    counts: dict[str, int] = {}
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DSARPipelineError(f"malformed verdict row {line_no} in {path}: {exc}") from exc
        verdict = row.get("scope_verdict") or row.get("verdict") or "unknown"
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def _write_anchor(case_path: Path, upstream_hash: str, summary: dict[str, int]) -> None:
    """Write working/scope_classify_complete.jsonl atomically with the
    cascade fields the orchestrator's STAGE_ARTEFACTS registry
    expects."""
    out_path = case_path / "working" / "scope_classify_complete.jsonl"
    tmp_path = out_path.with_suffix(".jsonl.tmp")
    row = {
        "completed": True,
        "upstream_hash": upstream_hash,
        "summary": summary,
        "schema_version": SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
