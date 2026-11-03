"""Two OpenAI chat-completions endpoints: the main model and the decision model.

DollOS does not know which model sits behind either URL. The only Strata dialect
field sent is `reasoning_effort`.
"""

from __future__ import annotations

import json

import httpx

from .config import DecisionConfig, LlmConfig


class ModelError(RuntimeError):
    pass


async def check(base_url: str, model: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as c:
        try:
            h = await c.get(f"{base_url}/health")
            m = await c.get(f"{base_url}/v1/models")
        except httpx.HTTPError as exc:
            raise ModelError(f"{base_url} unreachable: {exc}") from exc
    if h.status_code != 200:
        raise ModelError(f"{base_url}/health -> {h.status_code}")
    ids = [d.get("id") for d in m.json().get("data", [])]
    if model not in ids:
        raise ModelError(f"model {model!r} not served by {base_url} (has {ids})")


class MainModel:
    def __init__(self, cfg: LlmConfig) -> None:
        self.cfg = cfg

    async def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": self.cfg.max_tokens,
            "reasoning_effort": self.cfg.reasoning_effort,
        }
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.cfg.timeout_s, connect=5.0)) as c:
            r = await c.post(f"{self.cfg.base_url}/v1/chat/completions", json=body)
        if r.status_code != 200:
            raise ModelError(f"main model HTTP {r.status_code}: {r.text[:300]}")
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ModelError("main model hit max_tokens")
        return choice["message"]


class DecisionModel:
    """Small structured judgments. Self-reported scores, not calibrated probabilities."""

    def __init__(self, cfg: DecisionConfig) -> None:
        self.cfg = cfg

    async def decide(self, instructions: str, prompt: str, schema: dict) -> dict:
        body = {
            "model": self.cfg.model,
            "stream": False,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "decision", "schema": schema},
            },
        }
        async with httpx.AsyncClient(timeout=self.cfg.timeout_s) as c:
            r = await c.post(f"{self.cfg.base_url}/v1/chat/completions", json=body)
        if r.status_code != 200:
            raise ModelError(f"decision model HTTP {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelError(f"decision model returned non-JSON: {content[:200]}") from exc
