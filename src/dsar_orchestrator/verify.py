"""Audit-row verification — spec §4.1 (G) + §4.4.

Two verifiers, both invoked by `dsar-conductor verify --check ...`:

  * `verify_prompt_versions(case_dir, *, strict=False)` — for each row
    in durant_verdicts.jsonl + recheck JSONL, look up the canonical
    seal in the installed toolkit's _registry.json, load the archived
    asset, replay applied_strips, recompute the effective sha, compare
    to the audit row's recorded effective_sha256. Catches both
    accidental drift (different toolkit version used at run time vs
    audit time) and tampering.

  * `verify_fitness_report(case_dir)` — confirms a matching fresh +
    passing fitness report exists under
    ~/.dsar/fitness_reports/<deployment_id>/ (or override via
    DSAR_FITNESS_REPORT_ROOT env).
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


@dataclass
class VerifyResult:
    ok: bool
    exit_code: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _iter_jsonl_rows(path: Path) -> Iterator[tuple[int, dict]]:
    """Yield (line_no, decoded JSON object) tuples from a JSONL file.

    Skips blank lines. Yields ``{"_decode_error": "..."}`` for any
    malformed row so the caller can surface a single fatal error per
    bad line rather than aborting the whole verifier on the first
    decode failure.
    """
    if not path.is_file():
        return
    with open(path, encoding="utf-8") as f:
        for ln_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield ln_no, json.loads(raw)
            except json.JSONDecodeError as e:
                yield ln_no, {"_decode_error": str(e)}


def _read_archived_asset(archive_path: Path) -> tuple[dict, str]:
    """Load a gzipped archived asset and return (meta, body)."""
    import yaml

    raw_gz = archive_path.read_bytes()
    text = gzip.decompress(raw_gz).decode("utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{archive_path}: no leading ---")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{archive_path}: frontmatter not terminated")
    fm_text = text[4:end]
    body = text[end + len("\n---\n") :]
    meta = yaml.safe_load(fm_text)
    return meta, body


def _replay_effective(body: str, applied_strips: list[str], droppable: set[str]) -> str:
    """Replay the strip+normalise pipeline from prompt_loader against
    an archived body. Imports from toolkit so the rules stay in sync."""
    import hashlib

    from dsar_pipeline.gates.prompt_loader import (
        _normalise_whitespace,
        _strip_block,
    )

    processed = body
    for sid in applied_strips:
        if sid not in droppable:
            raise ValueError(f"applied_strips contains non-droppable id {sid!r}")
        processed = _strip_block(processed, sid)
    processed = _normalise_whitespace(processed)
    return hashlib.sha256(processed.encode("utf-8")).hexdigest()


def verify_prompt_versions(case_dir: Path, *, strict: bool = False) -> VerifyResult:
    """Walk durant_verdicts.jsonl + recheck JSONL and cross-check every
    row's prompt hashes against the installed toolkit's registry.

    Exit-code policy (spec §4.1 G):
      - 0: clean (or warnings only without --strict)
      - 2: hash drift, unknown seal, prompt_id mismatch, or
           older-version-than-current with --strict
    """
    result = VerifyResult(ok=True, exit_code=0)

    # Late toolkit import — orchestrator can install standalone.
    try:
        from dsar_pipeline.gates import prompt_loader as pl
    except ImportError:
        result.ok = False
        result.exit_code = 2
        result.errors.append(
            "dsar-toolkit not installed — `pip install -e ~/projects/dsar-toolkit`"
        )
        return result

    registry_path = pl._PROMPTS_DIR / "_registry.json"
    if not registry_path.is_file():
        result.ok = False
        result.exit_code = 2
        result.errors.append(f"prompt registry missing: {registry_path}")
        return result
    registry: dict[str, list[dict]] = json.loads(registry_path.read_text(encoding="utf-8"))

    # Build seal → (prompt_id, version, archive_path) index.
    seal_index: dict[str, tuple[str, str, Path]] = {}
    current_version_by_id: dict[str, str] = {}
    for prompt_id, entries in registry.items():
        for entry in entries:
            seal = entry["seal_sha256"]
            archive_path = pl._PROMPTS_DIR / "_archive" / prompt_id / f"{entry['version']}.md.gz"
            seal_index[seal] = (prompt_id, entry["version"], archive_path)
        if entries:
            current_version_by_id[prompt_id] = entries[-1]["version"]

    rows_to_check: list[tuple[str, int, dict]] = []
    primary_jsonl = case_dir / "working" / "durant_verdicts.jsonl"
    for ln_no, row in _iter_jsonl_rows(primary_jsonl):
        rows_to_check.append(("durant_verdicts.jsonl", ln_no, row))
    recheck_jsonl = case_dir / "working" / "durant_underdisclosure_recheck.jsonl"
    for ln_no, row in _iter_jsonl_rows(recheck_jsonl):
        rows_to_check.append(("durant_underdisclosure_recheck.jsonl", ln_no, row))

    if not rows_to_check:
        result.warnings.append("no audit rows found to verify")

    for source, ln_no, row in rows_to_check:
        if "_decode_error" in row:
            result.errors.append(f"{source}:{ln_no}: malformed JSON: {row['_decode_error']}")
            continue
        seal = row.get("prompt_canonical_seal_sha256")
        if not seal:
            result.errors.append(f"{source}:{ln_no}: missing prompt_canonical_seal_sha256")
            continue
        if seal not in seal_index:
            result.errors.append(f"{source}:{ln_no}: canonical seal {seal} not in registry")
            continue
        prompt_id, version, archive_path = seal_index[seal]
        if row.get("prompt_id") != prompt_id:
            result.errors.append(
                f"{source}:{ln_no}: prompt_id mismatch — "
                f"row={row.get('prompt_id')!r} registry={prompt_id!r}"
            )
            continue
        try:
            meta, body = _read_archived_asset(archive_path)
        except (OSError, ValueError) as e:
            result.errors.append(f"{source}:{ln_no}: cannot read archive {archive_path}: {e}")
            continue
        droppable = set(meta.get("droppable_blocks", []) or [])
        applied = list(row.get("prompt_applied_strips", []) or [])
        try:
            replayed = _replay_effective(body, applied, droppable)
        except ValueError as e:
            result.errors.append(f"{source}:{ln_no}: replay error: {e}")
            continue
        expected_eff = row.get("prompt_effective_sha256")
        if replayed != expected_eff:
            result.errors.append(
                f"{source}:{ln_no}: effective_sha256 drift — row={expected_eff} replayed={replayed}"
            )
            continue
        # Older-version check
        current = current_version_by_id.get(prompt_id)
        if current is not None and version != current:
            msg = (
                f"{source}:{ln_no}: row uses {prompt_id} v{version}; "
                f"current registered is v{current}"
            )
            if strict:
                result.errors.append(msg)
            else:
                result.warnings.append(msg)

    if result.errors:
        result.ok = False
        result.exit_code = 2
    elif result.warnings and strict:
        result.ok = False
        result.exit_code = 2
    else:
        result.ok = True
        result.exit_code = 0
    return result


def _read_case_config(case_dir: Path) -> dict[str, Any]:
    path = case_dir / "case_config.json"
    if not path.is_file():
        raise FileNotFoundError(f"case_config.json missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_fitness_report(case_dir: Path) -> VerifyResult:
    """Confirms a fresh + passing fitness report exists for this case's
    deployment_id under DSAR_FITNESS_REPORT_ROOT or
    ~/.dsar/fitness_reports/<deployment_id>/.
    """
    result = VerifyResult(ok=True, exit_code=0)
    try:
        cfg_raw = _read_case_config(case_dir)
    except FileNotFoundError as e:
        result.ok = False
        result.exit_code = 1
        result.errors.append(str(e))
        return result

    deployment_id = cfg_raw.get("fitness_check_deployment_id") or cfg_raw.get("deployment_id") or ""
    if not deployment_id:
        result.ok = False
        result.exit_code = 1
        result.errors.append("case_config.json missing fitness_check_deployment_id")
        return result

    max_age = int(cfg_raw.get("fitness_check_max_report_age_days", 30))
    report_root = Path(
        os.environ.get(
            "DSAR_FITNESS_REPORT_ROOT",
            str(Path.home() / ".dsar" / "fitness_reports"),
        )
    )
    deploy_dir = report_root / deployment_id
    if not deploy_dir.is_dir():
        result.ok = False
        result.exit_code = 1
        result.errors.append(
            f"no fitness reports directory at {deploy_dir} for deployment_id={deployment_id}"
        )
        return result

    now = datetime.now(timezone.utc)
    fresh_passing: list[tuple[str, str]] = []  # (path, generated_at)
    for rp in sorted(deploy_dir.glob("*.json")):
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            result.warnings.append(f"unreadable report {rp}: {e}")
            continue
        if not r.get("passed", False):
            continue
        gen_at = r.get("generated_at", "")
        try:
            gen_dt = datetime.fromisoformat(gen_at)
        except ValueError:
            continue
        age_days = (now - gen_dt).total_seconds() / 86400.0
        if age_days > max_age:
            continue
        fresh_passing.append((str(rp), gen_at))

    if not fresh_passing:
        result.ok = False
        result.exit_code = 1
        result.errors.append(
            f"no fresh+passing fitness report (<= {max_age}d) under "
            f"{deploy_dir}; run dsar-fitness-canary "
            f"--deployment-id {deployment_id}"
        )
        return result

    return result
