"""ComposedLLMAdapter — combines a Provider with a PromptTemplate."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import TYPE_CHECKING

from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.llm.templates import PromptTemplate
from dollos.llm.transport import Provider

if TYPE_CHECKING:
    from pydantic import BaseModel


class ComposedLLMAdapter(LLMAdapter):
    """Combine a Provider with a PromptTemplate to satisfy LLMAdapter."""

    def __init__(self, provider: Provider, template: PromptTemplate) -> None:
        self._provider = provider
        self._template = template

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type[BaseModel]] | None = None,
        grammar: str | None = None,
        purpose: str = "cascade",
        on_usage: Callable[[int | None, int | None], None] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        prompt = self._template.render(
            system=system, user=user, prefill=prefill, tools=tools
        )
        # aclosing() ensures GeneratorExit propagates synchronously into the
        # provider stream on early exit (break-on-done, cancel, exception) —
        # without it a plain `async for` leaves the inner generator dangling
        # until asyncio's GC finalizer runs, which fires `on_usage` too late
        # (after the drain gate already read the turn accumulators as None).
        async with aclosing(
            self._provider.stream(
                prompt=prompt, stop=stop, max_tokens=max_tokens, grammar=grammar,
                purpose=purpose, on_usage=on_usage,
            )
        ) as s:
            async for chunk in s:
                yield chunk

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type[BaseModel]] | None = None,
        grammar: str | None = None,
        purpose: str = "cascade",
        on_usage: Callable[[int | None, int | None], None] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        prompt = self._template.render_messages(
            system=system, messages=messages, tools=tools
        )
        # aclosing() — see stream_completion() above for why this matters.
        async with aclosing(
            self._provider.stream(
                prompt=prompt, stop=stop, max_tokens=max_tokens, grammar=grammar,
                purpose=purpose, on_usage=on_usage,
            )
        ) as s:
            async for chunk in s:
                yield chunk
