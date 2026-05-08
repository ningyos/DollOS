"""ComposedLLMAdapter — combines a Provider with a PromptTemplate."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    ) -> AsyncIterator[StreamChunk]:
        prompt = self._template.render(
            system=system, user=user, prefill=prefill, tools=tools
        )
        async for chunk in self._provider.stream(
            prompt=prompt, stop=stop, max_tokens=max_tokens, grammar=grammar
        ):
            yield chunk
