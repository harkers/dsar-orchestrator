"""mlx-broker HTTP client — OpenAI-compatible chat completions.

Stays on the box: default base URL is ``http://127.0.0.1:8090`` (the
mlx-broker LaunchAgent on zen). No auth headers, no external APIs.

For a future remote mlx-broker mode, override via ``MLX_BROKER_URL``
env var; the URL still has to point at an OpenAI-compatible endpoint
the operator controls.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from dsar_orchestrator.exceptions import DSARPipelineError

DEFAULT_BROKER_URL = "http://127.0.0.1:8090"
DEFAULT_MODEL_ALIAS = "tools"  # Hermes-4-70B-4bit; good at structured output
DEFAULT_TIMEOUT_S = 120


class LLMUnreachable(DSARPipelineError):
    """mlx-broker is not responding at the expected URL."""


class LLMBadResponse(DSARPipelineError):
    """mlx-broker returned a malformed response."""


@dataclass
class ChatResponse:
    text: str
    model_alias: str
    resolved_model: str


def chat(
    *,
    system: str,
    user: str,
    model_alias: str | None = None,
    base_url: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> ChatResponse:
    """Send one chat completion to mlx-broker. Returns the assistant
    text plus model-resolution metadata for the audit log.

    No client document text should be passed via ``user``. The
    analyser only sends structured audit metadata; this client doesn't
    enforce that, but the design relies on it.
    """
    base_url = base_url or os.environ.get("MLX_BROKER_URL", DEFAULT_BROKER_URL)
    model_alias = model_alias or os.environ.get("DSAR_ANALYSER_MODEL", DEFAULT_MODEL_ALIAS)

    payload = {
        "model": model_alias,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        raise LLMUnreachable(
            f"mlx-broker at {base_url} unreachable: {e}. "
            f"Check `launchctl print gui/$(id -u)/com.mlx-broker` and "
            f"`curl {base_url}/health`."
        ) from e

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMBadResponse(f"mlx-broker returned non-JSON: {e}") from e

    if "choices" not in body or not body["choices"]:
        raise LLMBadResponse(f"mlx-broker response missing choices: {body!r}")

    text = body["choices"][0].get("message", {}).get("content", "")
    if not text:
        raise LLMBadResponse(f"mlx-broker response had empty content: {body!r}")

    return ChatResponse(
        text=text,
        model_alias=model_alias,
        resolved_model=body.get("model", model_alias),
    )
