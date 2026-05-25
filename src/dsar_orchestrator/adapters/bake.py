"""Conductor-owned bake adapter — Stage 7 in v5.0.

Bridges to the toolkit's ``dsar-bake`` CLI. Reads
``working/redaction_input.jsonl`` (produced by the redact stage) and
applies redactions to the source files, writing to ``<case>/redacted/``.

This adapter was extracted from ``adapters/export.py`` in v5.0 (rollout
B phase 1) — previously bake ran inside the export adapter, which meant
the verifier couldn't see ``redacted/`` until after verify already ran.
v5.0 promotes bake to its own coarse stage between ``redact`` and
``verify_pdf``.

**Retirement contract.** When the toolkit ships
``dsar_pipeline.bake.run_for_case(case_path)`` (no toolkit issue yet),
this adapter retires.
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

PRODUCER_VERSION = "dsar_orchestrator.adapters.bake 0.5.0"
SCHEMA_VERSION = "1.0"
DEFAULT_BAKE_CLI = "dsar-bake"

# runner(argv, env, cwd) -> CompletedProcess
RunnerFn = Callable[[list[str], dict[str, str], Path], subprocess.CompletedProcess]


def _default_runner() -> RunnerFn:
    def run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )

    return run


def run_for_case(
    cfg: CaseConfig,
    *,
    runner: RunnerFn | None = None,
    bake_cli: str = DEFAULT_BAKE_CLI,
) -> None:
    """Drive `dsar-bake --case <id>`; write the cascade anchor manifest."""
    if runner is None:
        runner = _default_runner()

    # Issue #18: synthetic cases have no operator to resolve detect-stage
    # flag entries. Auto-mark them as redact:false before bake invokes
    # the legacy redact_all path (which refuses to ship while flags
    # remain unresolved).
    # Issue #26: real cases gain --resolve-flags-as opt-in for non-
    # interactive runs. Without the opt-in and without synth marker,
    # pending flags trigger an actionable halt below.
    if cfg.synthetic:
        _auto_resolve_synthetic_flags(cfg.case_path)
    elif cfg.resolve_flags_as is not None:
        target = cfg.resolve_flags_as == "true"
        _resolve_all_flags_to(cfg.case_path, target)
    else:
        _halt_on_pending_flags(cfg)

    from dsar_orchestrator.subprocess_env import build_subprocess_env

    env = build_subprocess_env()
    env["DSAR_CASE_ROOT"] = str(cfg.case_path.parent)
    # Skip the toolkit's MRA post-stage hooks (internal QA tooling that
    # imports a `module_agents` package not part of the conductor's
    # runtime contract; without this the hook raises ImportError mid-bake).
    # The hook is best-effort dashboard health checks; the conductor's
    # own check_<stage> agents cover validation we actually need.
    env.setdefault("DSAR_PIPELINE_SKIP_MRA", "1")
    # Synthetic cases have no operator to sign off — toolkit v0.3.2 added
    # DSAR_AUTO_SIGNOFF=1 which auto-writes a synthetic signoff after
    # redact (with proper timestamp ordering). Real operator cases must
    # sign off via dsar-pipeline --signoff '<reviewer>' as before.
    if cfg.synthetic:
        env.setdefault("DSAR_AUTO_SIGNOFF", "1")

    argv = [bake_cli, "--case", cfg.case_no]
    result = runner(argv, env, cfg.case_path)
    if result.returncode != 0:
        stderr = (result.stderr or "")[-2000:]
        raise DSARPipelineError(
            f"case={cfg.case_no}: bake CLI exited {result.returncode}. stderr tail:\n{stderr}"
        )

    redacted_dir = cfg.case_path / "redacted"
    if not redacted_dir.exists() or not any(redacted_dir.iterdir()):
        raise DSARPipelineError(
            f"case={cfg.case_no}: bake CLI succeeded but redacted/ missing or empty at {redacted_dir}."
        )

    upstream_hash = _hash_redaction_input(cfg.case_path / "working" / "redaction_input.jsonl")
    _write_manifest(cfg.case_path, upstream_hash)


def _auto_resolve_synthetic_flags(case_path: Path) -> int:
    """Rewrite any ``redact: "flag"`` entry in ``working/*_tags.json`` to
    ``redact: false`` AND clear ``"flagged for review"`` notes in
    ``working/register.json``, so the toolkit's bake (which delegates
    to legacy redact_all) doesn't refuse to ship.

    Two signals to clear:
      - Per-entity ``redact: "flag"`` in *_tags.json files (detect output).
      - Per-doc ``notes: "<N> items flagged for review"`` in register.json
        (also detect output; this is what legacy redact_all checks first).

    Synthetic cases have no operator in the loop. Real operator cases
    bypass this helper entirely (cfg.synthetic=False; see issue #18).

    Contract A note: this helper mutates register.json (toolkit-owned)
    only on synthetic cases — register.json's notes field is operator-
    workflow state, not the conductor-owned cascade metadata that
    Contract A reserves for the sibling register_meta.json.

    Returns: count of entries rewritten across all tags files (for tests).
    """
    working = case_path / "working"
    if not working.exists():
        return 0
    resolved = 0
    for tags_file in sorted(working.glob("*_tags.json")):
        try:
            data = json.loads(tags_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        changed = False
        for entity in data.get("entities", []):
            if entity.get("redact") == "flag":
                entity["redact"] = False
                resolved += 1
                changed = True
        if changed:
            tmp = tags_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, tags_file)

    _clear_synthetic_register_notes(working / "register.json")
    return resolved


def _clear_synthetic_register_notes(register_path: Path) -> None:
    """Clear ``notes`` fields containing "flagged for review" from
    register.json. Synthetic-only; called by _auto_resolve_synthetic_flags."""
    if not register_path.exists():
        return
    try:
        register = json.loads(register_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(register, list):  # Contract A: register is a flat list
        return
    changed = False
    for doc in register:
        if isinstance(doc, dict) and "flagged for review" in (doc.get("notes") or ""):
            doc["notes"] = ""
            changed = True
    if changed:
        tmp = register_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(register, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, register_path)


def _count_pending_flags(case_path: Path) -> tuple[int, int]:
    """Count unresolved detect-stage flags. Issue #26.

    Returns ``(entity_flag_count, register_notes_count)``:
      - entity_flag_count: number of ``redact: "flag"`` entries across
        all ``working/*_tags.json`` files.
      - register_notes_count: number of register.json rows whose
        ``notes`` contains "flagged for review".
    """
    working = case_path / "working"
    if not working.exists():
        return 0, 0
    entity_count = 0
    for tags_file in sorted(working.glob("*_tags.json")):
        try:
            data = json.loads(tags_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for entity in data.get("entities", []):
            if entity.get("redact") == "flag":
                entity_count += 1
    notes_count = 0
    register_path = working / "register.json"
    if register_path.exists():
        try:
            register = json.loads(register_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            register = []
        if isinstance(register, list):
            for doc in register:
                if isinstance(doc, dict) and "flagged for review" in (doc.get("notes") or ""):
                    notes_count += 1
    return entity_count, notes_count


def _resolve_all_flags_to(case_path: Path, target: bool) -> int:
    """Rewrite every ``redact: "flag"`` entry in ``working/*_tags.json``
    to ``redact: target`` AND clear register.json ``notes`` containing
    "flagged for review". Issue #26 — operator opt-in via
    ``--resolve-flags-as``.

    Returns: count of entity entries rewritten."""
    working = case_path / "working"
    if not working.exists():
        return 0
    resolved = 0
    for tags_file in sorted(working.glob("*_tags.json")):
        try:
            data = json.loads(tags_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        changed = False
        for entity in data.get("entities", []):
            if entity.get("redact") == "flag":
                entity["redact"] = target
                resolved += 1
                changed = True
        if changed:
            tmp = tags_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, tags_file)
    _clear_synthetic_register_notes(working / "register.json")
    return resolved


def _halt_on_pending_flags(cfg: "CaseConfig") -> None:
    """Pre-bake gate (issue #26). Halt with actionable message when real
    cases have unresolved flag entries from the detect stage."""
    from dsar_orchestrator.exceptions import PipelineHalt

    entity_count, notes_count = _count_pending_flags(cfg.case_path)
    if entity_count == 0 and notes_count == 0:
        return
    msg = (
        f"case={cfg.case_no} has pending detect-stage flags: "
        f"{entity_count} entity flag(s), {notes_count} doc(s) with "
        f"'flagged for review' in register.json::notes. "
        f"Review each working/*_tags.json (set redact: true|false), "
        f"OR re-run with --resolve-flags-as=true|false for non-interactive "
        f"resolution. See dsar-orchestrator#26."
    )
    raise PipelineHalt(msg)


def _hash_redaction_input(plan_path: Path) -> str:
    """Upstream for bake: the redaction plan written by redact stage."""
    return sha256_file(plan_path) if plan_path.exists() else ""


def _write_manifest(case_path: Path, upstream_hash: str) -> None:
    """Atomically write ``working/redact_v4/bake_manifest.json``."""
    out_dir = case_path / "working" / "redact_v4"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "bake_manifest.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    obj = {
        "completed": True,
        "upstream_hash": upstream_hash,
        "schema_version": SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
