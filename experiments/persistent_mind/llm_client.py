"""Minimal LLM client for the prototype — talks to llama-server at 127.0.0.1:8001."""
from __future__ import annotations

import json
import time

import httpx


class LLMClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8001",
        model: str = "unsloth/Qwen3.6",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as cx:
                r = await cx.get(f"{self.base_url}/v1/models")
                return r.status_code == 200
        except Exception:
            return False

    async def chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.6,
        max_tokens: int = 1024,
    ) -> tuple[str, dict]:
        """Non-streaming chat (simpler for prototype). Returns (text, stats)."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            # ask the model not to think (we want fast deterministic actions)
            "chat_template_kwargs": {"enable_thinking": False},
        }
        t0 = time.time()
        async with httpx.AsyncClient(timeout=self.timeout) as cx:
            r = await cx.post(f"{self.base_url}/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
        dt = time.time() - t0
        text = data["choices"][0]["message"]["content"]
        # strip <think>...</think> if present
        if "<think>" in text and "</think>" in text:
            pre, _, rest = text.partition("<think>")
            _, _, post = rest.partition("</think>")
            text = (pre + post).strip()
        usage = data.get("usage", {})
        stats = {
            "latency_s": round(dt, 2),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        return text, stats
