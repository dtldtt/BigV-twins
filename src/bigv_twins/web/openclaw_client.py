"""Async OpenClaw chat-completions client wrapper.

Reads the gateway token from `~/.openclaw/openclaw.json` on first call (cached).
Exposes `stream_chat()` which yields assistant text deltas as they arrive.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncIterator

import httpx

from bigv_twins.config import settings

log = logging.getLogger("bigv_twins.web.openclaw_client")

_TOKEN: str | None = None


def get_token() -> str:
    global _TOKEN
    if _TOKEN is None:
        cfg_path = Path(settings.openclaw_config_path)
        if not cfg_path.exists():
            raise RuntimeError(f"openclaw config not found: {cfg_path}")
        cfg = json.loads(cfg_path.read_text())
        token = (cfg.get("gateway") or {}).get("auth", {}).get("token")
        if not token:
            raise RuntimeError(
                "gateway.auth.token not set in ~/.openclaw/openclaw.json"
            )
        _TOKEN = token
    return _TOKEN


async def stream_chat(
    messages: list[dict],
    *,
    model: str = "openclaw/main",
) -> AsyncIterator[str]:
    """POST to /v1/chat/completions with stream=true, yield each text delta.

    The OpenClaw agent loop handles tool calls (bigv-twins.search etc.) transparently;
    we only see the final assistant text stream.
    """
    url = f"{settings.openclaw_base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    timeout = httpx.Timeout(settings.openclaw_agent_timeout_s, connect=5.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                body_text = (await resp.aread()).decode("utf-8", errors="replace")
                log.error("openclaw returned %s: %s", resp.status_code, body_text[:500])
                raise RuntimeError(
                    f"openclaw {resp.status_code}: {body_text[:200]}"
                )
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("non-JSON SSE line: %r", payload[:200])
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta
