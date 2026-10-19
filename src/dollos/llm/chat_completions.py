"""ChatCompletionsAdapter — ModelAdapter over OpenAI-compatible
``/v1/chat/completions`` with native tool calling (Strata, llama-server
``--jinja``).

Stream order on Strata: an empty role chunk (sent before prefill, so it does
not count for TTFT) → ``reasoning_content`` → ``content`` → ``tool_calls``
(first fragment per index carries id and name, the rest only argument
pieces) → a final chunk with ``finish_reason``, ``usage``, ``timings`` →
``data: [DONE]``. ``reasoning_budget_tokens`` and ``timings`` are
Strata/llama.cpp fields; other servers ignore or omit them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable

import httpx

from dollos.llm.model import (
    End,
    ModelAdapter,
    ModelError,
    ModelEvent,
    SpeechDelta,
    ThinkDelta,
    ToolCall,
    ToolCalls,
    Usage,
)
from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder

logger = logging.getLogger(__name__)

_CHECK_TIMEOUT_S = 10.0


class _Reply:
    """Accumulates one streamed reply."""

    def __init__(self) -> None:
        self.think: list[str] = []
        self.speech: list[str] = []
        self.tools: dict[int, dict] = {}  # index -> {"id", "name", "arguments"}
        self.finish_reason: str | None = None
        self.usage: dict = {}
        self.timings: dict = {}
        self.done = False
        self.first_token_t: float | None = None
        self.first_content_t: float | None = None

    def feed(self, data: dict) -> list[ModelEvent]:
        if data.get("error") is not None:
            err = data["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise ModelError("server", f"error in stream: {msg}")
        if data.get("usage"):
            self.usage = data["usage"]
        if data.get("timings"):
            self.timings = data["timings"]
        out: list[ModelEvent] = []
        for choice in data.get("choices") or []:
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                self._mark()
                self.think.append(delta["reasoning_content"])
                out.append(ThinkDelta(delta["reasoning_content"]))
            if delta.get("content"):
                self._mark()
                if self.first_content_t is None:
                    self.first_content_t = time.monotonic()
                self.speech.append(delta["content"])
                out.append(SpeechDelta(delta["content"]))
            for frag in delta.get("tool_calls") or []:
                self._mark()
                call = self.tools.setdefault(
                    frag["index"], {"id": "", "name": "", "arguments": ""}
                )
                fn = frag.get("function") or {}
                if not call["id"] and frag.get("id"):
                    call["id"] = frag["id"]
                if not call["name"] and fn.get("name"):
                    call["name"] = fn["name"]
                call["arguments"] += fn.get("arguments") or ""
        return out

    def _mark(self) -> None:
        if self.first_token_t is None:
            self.first_token_t = time.monotonic()

    def finish(self, start_t: float) -> tuple[list[ToolCall], End]:
        calls = [
            ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
            for _, c in sorted(self.tools.items())
        ]
        dropped = 0
        if calls and self.finish_reason == "length":
            # The last call is cut mid-JSON; run none of them (simple rule).
            dropped = len(calls)
            logger.warning(
                "reply hit max_tokens: dropped %d tool call(s) (%s)",
                dropped, ", ".join(c.name for c in calls),
            )
            calls = []
        elif calls and self.finish_reason != "tool_calls":
            raise ModelError(
                "server",
                f"tool call fragments with finish_reason={self.finish_reason!r}",
            )

        u, t = self.usage, self.timings
        prompt_tokens = u.get("prompt_tokens")
        cache_n, prompt_n, prompt_ms = t.get("cache_n"), t.get("prompt_n"), t.get("prompt_ms")
        # When the think cap fires the server re-reads the prompt after its cap
        # line, and the timings describe that read: they no longer add up.
        think_capped = (
            None not in (cache_n, prompt_n, prompt_tokens)
            and cache_n + prompt_n != prompt_tokens
        )
        if think_capped:
            cache_n = prompt_n = prompt_ms = None
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=u.get("completion_tokens"),
            cache_n=cache_n,
            prompt_n=prompt_n,
            prompt_ms=prompt_ms,
            predicted_per_second=t.get("predicted_per_second"),
            draft_n=t.get("draft_n"),
            draft_n_accepted=t.get("draft_n_accepted"),
        )

        message: dict = {"role": "assistant", "content": "".join(self.speech)}
        if self.think:
            message["reasoning_content"] = "".join(self.think)
        if calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls
            ]
        ttft_ms = (
            (self.first_token_t - start_t) * 1000.0 if self.first_token_t is not None else None
        )
        end = End(
            finish_reason=self.finish_reason,
            usage=usage,
            assistant_message=message,
            ttft_ms=ttft_ms,
            dropped_tool_calls=dropped,
            think_capped=think_capped,
        )
        return calls, end


class ChatCompletionsAdapter(ModelAdapter):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_s: float = 300.0,
        max_concurrency: int = 1,
        presence_penalty: float = 1.3,
        recorder: TelemetryRecorder | None = None,
        max_context_tokens: int = 131_072,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._presence_penalty = presence_penalty
        self._recorder = recorder
        self._max_context_tokens = max_context_tokens
        # One semaphore for every call: the server has a single compute slot.
        self._sem = asyncio.Semaphore(max_concurrency)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def stream(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        think_budget: int,
        purpose: str,
        on_usage: Callable[[int | None, int | None], None] | None,
        conversation: tuple[str, int] | None = None,
        prior_tokens: int = 0,
    ) -> AsyncIterator[ModelEvent]:
        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            # max_tokens counts thinking too, so the think budget goes on top.
            "max_tokens": max_tokens + think_budget,
            "reasoning_budget_tokens": think_budget,
            "presence_penalty": self._presence_penalty,
        }
        if tools:
            body["tools"] = tools
        url = f"{self._base_url}/v1/chat/completions"
        timeout = httpx.Timeout(self._timeout_s, connect=5.0)

        start_t = time.monotonic()
        reply = _Reply()
        calls: list[ToolCall] = []
        end: End | None = None
        error: str | None = None
        try:
            async with self._sem:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "POST", url, json=body, headers=self._headers()
                    ) as resp:
                        if resp.status_code >= 400:
                            text = (await resp.aread()).decode("utf-8", "replace")
                            kind = "server" if resp.status_code < 500 else "transport"
                            raise ModelError(kind, f"HTTP {resp.status_code} from {url}: {text}")
                        async for line in resp.aiter_lines():
                            if not line or line.startswith(":"):
                                continue
                            payload = line.removeprefix("data:").strip()
                            if payload == "[DONE]":
                                reply.done = True
                                break
                            try:
                                data = json.loads(payload)
                            except json.JSONDecodeError as e:
                                raise ModelError("server", f"non-JSON SSE line: {line!r}") from e
                            for ev in reply.feed(data):
                                yield ev
                if not reply.done:
                    raise ModelError("transport", f"stream from {url} ended before [DONE]")
            calls, end = reply.finish(start_t)
        except ModelError as e:
            error = f"{e.kind}: {e}"[:300]
            raise
        except httpx.TimeoutException as e:
            error = f"transport: timeout ({type(e).__name__})"
            raise ModelError("transport", f"timeout talking to {url}: {e!r}") from e
        except httpx.HTTPError as e:
            error = f"transport: {type(e).__name__}"
            raise ModelError("transport", f"cannot reach {url}: {e!r}") from e
        except Exception as e:
            error = f"error: {type(e).__name__}"
            raise
        finally:
            # Runs on success, error, early close and cancel alike. An
            # interrupted call is not a model failure: error stays None and
            # finish_reason stays None.
            await self._account(
                reply, start_t, error, purpose, on_usage, conversation, prior_tokens, end
            )
        if calls:
            yield ToolCalls(tuple(calls))
        yield end

    async def _account(
        self,
        reply: _Reply,
        start_t: float,
        error: str | None,
        purpose: str,
        on_usage: Callable[[int | None, int | None], None] | None,
        conversation: tuple[str, int] | None,
        prior_tokens: int,
        end: End | None,
    ) -> None:
        prompt_tokens = reply.usage.get("prompt_tokens")
        completion_tokens = reply.usage.get("completion_tokens")
        new_prompt_tokens = (
            max(prompt_tokens - prior_tokens, 0) if prompt_tokens is not None else None
        )
        if on_usage is not None:
            try:
                on_usage(new_prompt_tokens, completion_tokens)
            except Exception:
                logger.exception("on_usage callback raised (continuing)")
        if self._recorder is None:
            return

        def ms(t: float | None) -> float | None:
            return (t - start_t) * 1000.0 if t is not None else None

        usage = end.usage if end is not None else Usage()
        rec = LLMCallRecord(
            ts=time.time(),
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ttft_ms=ms(reply.first_token_t),
            latency_total_ms=ms(time.monotonic()),
            error=error,
            context_pct=(
                prompt_tokens / self._max_context_tokens * 100.0
                if prompt_tokens is not None else None
            ),
            call_purpose=purpose,
            finish_reason=end.finish_reason if end is not None else None,
            cache_n=usage.cache_n,
            prompt_n=usage.prompt_n,
            prompt_ms=usage.prompt_ms,
            predicted_per_second=usage.predicted_per_second,
            first_content_ms=ms(reply.first_content_t),
            think_capped=end.think_capped if end is not None else None,
            conversation=conversation[0] if conversation is not None else None,
            epoch=conversation[1] if conversation is not None else None,
            new_prompt_tokens=new_prompt_tokens,
        )
        try:
            await self._recorder.record(rec)
        except Exception:
            logger.exception("recorder.record raised (continuing)")

    async def check(self) -> None:
        where = f"{self._base_url} (configured model {self._model!r})"
        try:
            async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT_S) as client:
                health = await client.get(f"{self._base_url}/health", headers=self._headers())
                status = _json_or_text(health)
                if not (isinstance(status, dict) and status.get("status") == "ok"):
                    raise RuntimeError(
                        f"model server not ready at {where}: "
                        f"/health returned HTTP {health.status_code}: {status}"
                    )
                models = await client.get(f"{self._base_url}/v1/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise RuntimeError(f"cannot reach model server at {where}: {e!r}") from e
        listing = _json_or_text(models)
        ids = (
            [m.get("id") for m in listing.get("data") or []]
            if isinstance(listing, dict) else []
        )
        if self._model not in ids:
            raise RuntimeError(
                f"model server at {where} does not serve that model: "
                f"/v1/models returned HTTP {models.status_code}, ids {ids or listing}"
            )


def _json_or_text(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text
