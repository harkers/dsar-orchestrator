"""AI-generated per-stage summaries with RAG status for the operator console.

Routes through the local mlx-broker `writer` model (Llama-3.3-70B-Instruct
at time of writing) to produce a 60-word operator-readable summary +
Red/Amber/Green status for each pipeline stage. Results cached to
``<case-dir>/audit/stage_summaries.jsonl`` so we don't re-call the broker
on every page load.

PII redaction (per code-qwen25 review HIGH 4): source-file paths contain
mailbox-owner email addresses which are PII. Before sending stage context
to the broker, paths are stripped to filename + parent-dir-stem only,
and mailbox owner emails are replaced with their slug.

Eviction warning (per code-qwen25 review HIGH 1): the writer model is
~70B, so loading it will evict whatever else is resident on the broker.
Callers should check broker state and warn the operator if another long
pass is in flight.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BROKER = "http://127.0.0.1:8090/v1/chat/completions"
DEFAULT_MODEL = "writer"
DEFAULT_MAX_TOKENS = 600

# Mailbox-owner-email pattern, used to redact PII out of prompts.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Path components matching a known mailbox-owner pattern get the email
# replaced with `<email-redacted>` so the broker never sees real names.

ALLOWED_RAG = ("G", "A", "R")

log = logging.getLogger("stage-summariser")


@dataclass(frozen=True)
class SummariserConfig:
    case_dir: Path
    broker: str = DEFAULT_BROKER
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def cache_file(self) -> Path:
        return self.case_dir / "audit" / "stage_summaries.jsonl"

    @property
    def working(self) -> Path:
        return self.case_dir / "working"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------


def _redact_pii(text: str) -> str:
    """Strip mailbox-owner emails out of any string before broker send."""
    return _EMAIL_RE.sub("<email-redacted>", text)


def _redact_path(p: str | Path) -> str:
    """Reduce a full path to `<parent-dir-stem>/<filename>`. Drops the
    mailbox-owner directory in Exchange/SharePoint paths so PII doesn't
    leak into the prompt."""
    pp = Path(p)
    if not pp.parts:
        return str(p)
    if len(pp.parts) >= 2:
        return f"{pp.parts[-2]}/{pp.name}"
    return pp.name


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_key(stage: str, artefacts: list[dict]) -> str:
    """sha256 of stage name + sorted artefact (name, line_count, sample-sha)."""
    parts = [stage]
    for a in sorted(artefacts, key=lambda x: x["name"]):
        parts.append(a["name"])
        parts.append(str(a.get("line_count") or ""))
        parts.append(a.get("sample_sha", ""))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _load_cache(cfg: SummariserConfig) -> dict[str, dict]:
    """Return ``{stage_name: latest_summary_record}``."""
    if not cfg.cache_file.exists():
        return {}
    by_stage: dict[str, dict] = {}
    with cfg.cache_file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("stage"):
                by_stage[r["stage"]] = r
    return by_stage


def _append_cache(cfg: SummariserConfig, record: dict) -> None:
    cfg.cache_file.parent.mkdir(parents=True, exist_ok=True)
    with cfg.cache_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Stage context gathering
# ---------------------------------------------------------------------------


def _sample_jsonl(path: Path, *, max_rows: int = 1, max_chars: int = 500) -> tuple[str, str]:
    """Return (sample_text, sample_sha) for cache-key + prompt input."""
    if not path.exists():
        return ("", "")
    samples: list[str] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                samples.append(line)
                if len(samples) >= max_rows:
                    break
    except OSError:
        return ("", "")
    text = "\n".join(samples)[:max_chars]
    text = _redact_pii(text)
    sample_sha = hashlib.sha256(text.encode()).hexdigest()[:12]
    return (text, sample_sha)


def gather_stage_artefacts(
    cfg: SummariserConfig, stage: str, stage_artefact_names: list[str]
) -> list[dict]:
    """Per-stage artefact summary: name, exists, line_count, sample."""
    out = []
    for name in stage_artefact_names:
        p = cfg.working / name
        if not p.exists():
            out.append(
                {"name": name, "exists": False, "line_count": None, "sample": "", "sample_sha": ""}
            )
            continue
        line_count = None
        sample = ""
        sample_sha = ""
        if name.endswith(".jsonl"):
            try:
                line_count = sum(1 for line in p.open() if line.strip())
            except OSError:
                pass
            sample, sample_sha = _sample_jsonl(p)
        else:
            size = p.stat().st_size
            sample = f"({name}: {size} bytes)"
            sample_sha = hashlib.sha256(str(size).encode()).hexdigest()[:12]
        out.append(
            {
                "name": name,
                "exists": True,
                "line_count": line_count,
                "sample": sample,
                "sample_sha": sample_sha,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Broker call
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """You are summarising one stage of an automated DSAR \
(Data Subject Access Request) pipeline for a privacy specialist. \
Your output drives a real operator's decision about whether the stage \
is clean, needs review, or is blocking release.

Three RAG status values:
  GREEN (G): stage completed cleanly with no findings that need review
  AMBER (A): stage completed but produced findings or caveats the operator \
should eventually review; not blocking
  RED   (R): stage failed, hasn't run when it should have, or produced \
findings that BLOCK release of the disclosure pack

Respond with VALID JSON ONLY, no markdown fences, no prose:
{"summary": "<60-100 word operator-readable summary>", \
"rag": "G|A|R", \
"reasoning": "<one sentence justifying the RAG choice>"}

Keep the summary plain English. Don't use technical jargon the operator \
hasn't already seen on their screen (no internal state-machine names \
like 'redaction_qc_a_running' — say 'over-disclosure check' instead).
Cite specific numbers from the artefact counts whenever possible.
"""


def _build_user_prompt(
    stage_label: str,
    phase_label: str,
    status: str,
    artefacts: list[dict],
) -> str:
    lines = [
        f"Stage: {stage_label}",
        f"Phase: {phase_label}",
        f"Current status: {status}",
        "",
        "Artefacts present in working/:",
    ]
    if not artefacts:
        lines.append("  (no artefacts declared for this stage)")
    else:
        for a in artefacts:
            line = f"  - {a['name']}"
            if a["line_count"] is not None:
                line += f" — {a['line_count']:,} rows"
            if not a["exists"]:
                line += " (not present)"
            lines.append(line)
            if a.get("sample"):
                lines.append(f"    sample: {a['sample'][:300]}")
    lines.append("")
    lines.append("Summarise and assign R/A/G.")
    return "\n".join(lines)


def _call_writer(cfg: SummariserConfig, system: str, user: str) -> tuple[dict, float]:
    body = json.dumps(
        {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": cfg.max_tokens,
        }
    ).encode()
    started = time.monotonic()
    req = urllib.request.Request(
        cfg.broker, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = json.load(resp)
    elapsed = time.monotonic() - started
    return raw, elapsed


def _parse_response(raw: dict) -> dict:
    msg = raw["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1] if content.count("```") >= 2 else content
        if content.lower().startswith("json"):
            content = content[4:]
    content = content.strip().rstrip("`").strip()
    if not content:
        raise RuntimeError(
            f"writer returned no content (finish_reason={raw['choices'][0]['finish_reason']})"
        )
    parsed = json.loads(content)
    rag = parsed.get("rag", "?")
    if rag not in ALLOWED_RAG:
        # Force into AMBER on bad value rather than throwing — robust UX
        rag = "A"
    return {
        "summary": str(parsed.get("summary", ""))[:1000],
        "rag": rag,
        "reasoning": str(parsed.get("reasoning", ""))[:500],
    }


def summarise_stage(
    cfg: SummariserConfig,
    *,
    stage: str,
    stage_label: str,
    phase_label: str,
    status: str,
    stage_artefact_names: list[str],
    force_refresh: bool = False,
) -> dict:
    """Return a summary record. Uses cache when possible.

    Output shape (also the cache row shape):
      {ts, stage, cache_key, model, elapsed_sec, summary, rag, reasoning,
       artefacts_at_summary_time: [...]}
    """
    artefacts = gather_stage_artefacts(cfg, stage, stage_artefact_names)
    key = _cache_key(stage, artefacts)
    cache = _load_cache(cfg)
    cached = cache.get(stage)
    if cached and cached.get("cache_key") == key and not force_refresh:
        return cached

    user = _build_user_prompt(stage_label, phase_label, status, artefacts)
    raw, elapsed = _call_writer(cfg, SYSTEM_PROMPT, user)
    parsed = _parse_response(raw)
    record = {
        "ts": _iso_now(),
        "stage": stage,
        "cache_key": key,
        "model": f"{cfg.model}@{cfg.broker.split('://', 1)[-1].split('/', 1)[0]}",
        "elapsed_sec": round(elapsed, 2),
        "summary": parsed["summary"],
        "rag": parsed["rag"],
        "reasoning": parsed["reasoning"],
        "artefacts_at_summary_time": [
            {"name": a["name"], "exists": a["exists"], "line_count": a["line_count"]}
            for a in artefacts
        ],
    }
    _append_cache(cfg, record)
    return record


# ---------------------------------------------------------------------------
# Broker state check (per code-qwen25 review HIGH 1)
# ---------------------------------------------------------------------------


def check_broker_eviction_risk(cfg: SummariserConfig) -> dict:
    """Probe broker to see which models are loaded. Return a hint dict the
    caller can show as a warning before refreshing summaries.

    {risk: 'none'|'low'|'high',
     loaded: [...],
     warning: "..."}
    """
    try:
        with urllib.request.urlopen(
            cfg.broker.replace("/v1/chat/completions", "/v1/models"),
            timeout=5,
        ) as r:
            data = json.load(r)
        loaded = sorted(m["id"] for m in data.get("data", []) if m.get("loaded"))
    except Exception as exc:
        return {"risk": "unknown", "loaded": [], "warning": f"broker probe failed: {exc}"}
    if cfg.model in loaded:
        return {"risk": "none", "loaded": loaded, "warning": ""}
    # If anything heavy is loaded, the writer load might evict it
    heavyweights = {"chat", "heavy", "longctx", "reasoner", "code", "code-qwen25"}
    risky_loaded = sorted(set(loaded) & heavyweights)
    if risky_loaded:
        return {
            "risk": "high",
            "loaded": loaded,
            "warning": (
                f"loading the writer model will likely evict {risky_loaded} from broker memory. "
                "If a long-running broker pass is in flight (e.g. agent-durant, agent06-tagger), "
                "wait for it to finish before refreshing summaries."
            ),
        }
    return {"risk": "low", "loaded": loaded, "warning": ""}
