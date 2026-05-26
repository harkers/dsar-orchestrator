"""Route a DSAR operator-gate decision through the local mlx-broker.

Usage:
    dsar-gate-llm --case-dir <path> <gate_name> <opt1,opt2,...> < context

The script reads gate context (counts, sample findings, etc.) from stdin,
posts a structured JSON-only prompt to the broker's ``chat`` model, parses
``{decision, rationale, confidence}`` from the response, and appends the
full ``{prompt, response, decision, ts}`` record to
``<case-dir>/audit/gate-decisions.jsonl``. Prints the decision string to
stdout so a caller can branch on it; rationale + confidence go to stderr.

Audit-trail note: this tool exists because some operators choose full
LLM autonomy for routine gate decisions. Each call records the model id
+ reasoning so the chain is reproducible. Cloud-LLM-routed equivalents
live elsewhere; this one is local-broker only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BROKER = "http://127.0.0.1:8090/v1/chat/completions"
DEFAULT_MODEL = "chat"
DEFAULT_MAX_TOKENS = 2500


@dataclass(frozen=True)
class GateLLMConfig:
    case_dir: Path
    broker: str = DEFAULT_BROKER
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def audit_log(self) -> Path:
        return self.case_dir / "audit" / "gate-decisions.jsonl"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_messages(gate_name: str, options: list[str], context: str) -> list[dict]:
    system = (
        "You are an operator-gate decision agent for a DSAR (data subject "
        "access request) pipeline. You will be given a gate name, the set "
        "of allowed decisions, and the runtime context (counts, severity "
        "distribution, sample findings, etc.). Respond ONLY with valid "
        "JSON matching this schema: "
        '{"decision": "<one of the options>", '
        '"rationale": "2-4 sentences", "confidence": 0.0-1.0}. No prose, '
        "no markdown fences, no extra keys."
    )
    user = (
        f"Gate: {gate_name}\n"
        f"Allowed decisions: {', '.join(options)}\n\n"
        f"Context:\n{context}\n\n"
        f"Decide."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def decide(cfg: GateLLMConfig, gate_name: str, options: list[str], context: str) -> dict[str, Any]:
    """Submit a gate decision request and return the validated decision.

    Side effect: appends a prompt+response audit row to
    ``<case_dir>/audit/gate-decisions.jsonl``.
    """
    messages = _build_messages(gate_name, options, context)
    started = time.monotonic()
    body = json.dumps(
        {
            "model": cfg.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": cfg.max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        cfg.broker, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = json.load(resp)
    elapsed = time.monotonic() - started

    msg = raw["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning") or "").strip()

    if not content:
        raise RuntimeError(
            f"model returned no content (finish_reason="
            f"{raw['choices'][0]['finish_reason']}); "
            f"reasoning len={len(reasoning)}"
        )
    try:
        decision = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model content was not valid JSON: {exc}\n{content!r}")

    if decision.get("decision") not in options:
        raise RuntimeError(f"model picked {decision.get('decision')!r}; allowed: {options}")

    record = {
        "ts": _iso_now(),
        "gate": gate_name,
        "options": options,
        "model": raw.get("model", cfg.model),
        "elapsed_sec": round(elapsed, 2),
        "prompt": {
            "system": messages[0]["content"],
            "user": messages[1]["content"],
        },
        "response": {"reasoning": reasoning, "content": content},
        "decision": decision,
    }
    cfg.audit_log.parent.mkdir(parents=True, exist_ok=True)
    with cfg.audit_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return decision


def _parse_args(argv: list[str] | None = None) -> tuple[GateLLMConfig, argparse.Namespace]:
    p = argparse.ArgumentParser(
        prog="dsar-gate-llm",
        description=("Route a DSAR operator-gate decision through the local mlx-broker."),
    )
    p.add_argument(
        "--case-dir",
        required=True,
        type=Path,
        help="Engagement directory (mounted sparse bundle). Audit log "
        "appends to <case-dir>/audit/gate-decisions.jsonl.",
    )
    p.add_argument(
        "--broker",
        default=DEFAULT_BROKER,
        help=f"OpenAI-compat endpoint (default: {DEFAULT_BROKER})",
    )
    p.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Broker model alias (default: {DEFAULT_MODEL})"
    )
    p.add_argument(
        "--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Response token budget"
    )
    p.add_argument("gate_name", help="Short name for the gate (logged in audit)")
    p.add_argument(
        "options",
        help="Comma-separated allowed decision strings",
    )
    args = p.parse_args(argv)
    cfg = GateLLMConfig(
        case_dir=args.case_dir,
        broker=args.broker,
        model=args.model,
        max_tokens=args.max_tokens,
    )
    return cfg, args


def main(argv: list[str] | None = None) -> int:
    cfg, args = _parse_args(argv)
    options = [o.strip() for o in args.options.split(",") if o.strip()]
    context = sys.stdin.read().strip()
    if not context:
        print("error: no context on stdin", file=sys.stderr)
        return 2
    if not cfg.case_dir.exists():
        print(f"error: case-dir does not exist: {cfg.case_dir}", file=sys.stderr)
        return 2
    decision = decide(cfg, args.gate_name, options, context)
    print(decision["decision"])
    print(f"rationale: {decision['rationale']}", file=sys.stderr)
    print(f"confidence: {decision['confidence']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
