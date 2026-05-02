"""llama.cpp /completion endpoint adapter.

Supports prefill via prompt concatenation: the adapter renders a ChatML-formatted
prompt where the assistant role is opened, the <think> block is opened, and
the prefill text is appended inside <think>. The model then continues from
there. This is the prefill mechanism described in
grammar_injection_techreport.md §2.3.
"""

import json
import logging
from collections.abc import AsyncIterator

import httpx

from dollos.llm.adapter import LLMAdapter, StreamChunk

logger = logging.getLogger(__name__)


def _render_chatml(system: str, user: str, prefill: str) -> str:
    """Render Qwen-style ChatML prompt with <think> block opened for prefill.

    The assistant role opens with <think>\\n (Qwen3.x thinking-model
    convention); prefill is appended INSIDE the <think> block. Callers should
    pass prefill as the recall/lessons/goal content WITHOUT a leading <think>
    tag (the renderer adds it). Plan 3 (VoM) populates this prefill.

    Assumes a thinking-capable Qwen-family model. Non-thinking-model support
    is out of scope for Plan 1; Plan 4 (multi-LLM adapter) will revisit.
    """
    parts = [
        "<|im_start|>system",
        system,
        "<|im_end|>",
        "<|im_start|>user",
        user,
        "<|im_end|>",
        "<|im_start|>assistant",
        "<think>",
        "",
    ]
    rendered = "\n".join(parts)
    if prefill:
        rendered += prefill
    return rendered


class LlamaCppAdapter(LLMAdapter):
    """Adapter for self-hosted llama.cpp `/completion` endpoint."""

    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        prompt = _render_chatml(system=system, user=user, prefill=prefill)
        body = {
            "prompt": prompt,
            "stream": True,
            "n_predict": max_tokens,
            "stop": stop or ["<|im_end|>"],
            "cache_prompt": True,
        }
        url = f"{self.base_url}/completion"
        timeout = httpx.Timeout(self.timeout_s, connect=5.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("non-JSON SSE line: %r", payload)
                        continue
                    yield StreamChunk(
                        text=data.get("content", ""),
                        done=bool(data.get("stop", False)),
                    )
                    if data.get("stop"):
                        return
