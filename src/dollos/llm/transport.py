"""LLM transport — HTTP / endpoint conventions / response parsing.

A Provider talks to a specific LLM server (llama.cpp, vLLM, OpenAI-compat,
Anthropic, ...) and yields StreamChunk objects. It takes a fully-rendered
prompt string; prompt formatting is PromptTemplate's job.
"""

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from dollos.llm.adapter import StreamChunk

logger = logging.getLogger(__name__)


class Provider(ABC):
    """Abstract LLM transport."""

    @property
    @abstractmethod
    def supports_prefill(self) -> bool:
        """True iff this provider's endpoint can take an open assistant
        turn (i.e. the caller can give a partial assistant message and have
        the model continue from there). Critical for VoM."""

    @abstractmethod
    async def stream(
        self,
        *,
        prompt: str,
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream tokens. Caller owns prompt formatting. `grammar` is a GBNF
        string used to constrain sampling when the backend supports it; pass
        None for unconstrained sampling."""
        ...


class LlamaCppProvider(Provider):
    """POST /completion to a llama-server with SSE streaming."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 60.0,
        presence_penalty: float = 1.3,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._presence_penalty = presence_penalty

    @property
    def supports_prefill(self) -> bool:
        return True  # llama.cpp /completion always supports prefill via raw prompt

    async def stream(
        self,
        *,
        prompt: str,
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        grammar: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        body = {
            "prompt": prompt,
            "stream": True,
            "n_predict": max_tokens,
            # Default ChatML stop kept here for v1 (see spec §10 Open Question).
            # Will be moved to PromptTemplate when a non-ChatML template lands.
            "stop": stop if stop is not None else ["<|im_end|>"],
            "cache_prompt": True,
            "presence_penalty": self._presence_penalty,
        }
        if grammar is not None:
            body["grammar"] = grammar
        url = f"{self._base_url}/completion"
        timeout = httpx.Timeout(self._timeout_s, connect=5.0)

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
