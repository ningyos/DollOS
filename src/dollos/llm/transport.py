"""LLM transport — HTTP / endpoint conventions / response parsing.

A Provider talks to a specific LLM server (llama.cpp, vLLM, OpenAI-compat,
Anthropic, ...) and yields StreamChunk objects. It takes a fully-rendered
prompt string; prompt formatting is PromptTemplate's job.
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from dollos.llm.adapter import StreamChunk
from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder

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
        purpose: str = "cascade",
    ) -> AsyncIterator[StreamChunk]:
        """Stream tokens. Caller owns prompt formatting. `grammar` is a GBNF
        string used to constrain sampling when the backend supports it; pass
        None for unconstrained sampling. `purpose` is a free-form tag stored
        in telemetry for cognition vitals (e.g. "cascade", "subagent")."""
        ...


class LlamaCppProvider(Provider):
    """POST /completion to a llama-server with SSE streaming."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 60.0,
        presence_penalty: float = 1.3,
        recorder: TelemetryRecorder | None = None,
        model_alias: str | None = None,
        max_context_tokens: int = 131_072,
        max_concurrency: int = 2,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._presence_penalty = presence_penalty
        self._recorder = recorder
        self._model_alias = model_alias
        self._max_context_tokens = max_context_tokens
        self._sem = asyncio.Semaphore(max_concurrency)

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
        purpose: str = "cascade",
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

        start_t = time.monotonic()
        ttft_ms: float | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        first_token_seen = False
        error_kind: str | None = None
        last_data: dict | None = None

        await self._sem.acquire()
        try:
            try:
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
                            last_data = data
                            if not first_token_seen and data.get("content"):
                                ttft_ms = (time.monotonic() - start_t) * 1000.0
                                first_token_seen = True
                            yield StreamChunk(
                                text=data.get("content", ""),
                                done=bool(data.get("stop", False)),
                            )
                            if data.get("stop"):
                                break
            except httpx.TimeoutException:
                error_kind = "timeout"
                raise
            except httpx.HTTPStatusError as e:
                error_kind = f"http_{e.response.status_code}"
                raise
            except httpx.HTTPError as e:
                error_kind = f"http_error:{type(e).__name__}"
                raise
            except Exception as e:
                error_kind = f"error:{type(e).__name__}"
                raise
            finally:
                total_ms = (time.monotonic() - start_t) * 1000.0
                if last_data is not None:
                    # llama.cpp final SSE payload (when stop=true) includes
                    # `tokens_evaluated` (prompt) and `tokens_predicted` (completion).
                    pe = last_data.get("tokens_evaluated")
                    pp = last_data.get("tokens_predicted")
                    if isinstance(pe, int):
                        prompt_tokens = pe
                    if isinstance(pp, int):
                        completion_tokens = pp
                if self._recorder is not None:
                    context_pct: float | None = None
                    if prompt_tokens is not None and self._max_context_tokens:
                        context_pct = prompt_tokens / self._max_context_tokens * 100.0
                    rec = LLMCallRecord(
                        ts=time.time(),
                        model=self._model_alias,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ttft_ms=ttft_ms,
                        latency_total_ms=total_ms,
                        error=error_kind,
                        context_pct=context_pct,
                        call_purpose=purpose,
                    )
                    try:
                        await self._recorder.record(rec)
                    except Exception:
                        logger.exception("recorder.record raised (continuing)")
        finally:
            self._sem.release()
