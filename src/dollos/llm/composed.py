"""ComposedLLMAdapter — combines a Provider with a PromptTemplate."""

from collections.abc import AsyncIterator

from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.llm.templates import PromptTemplate
from dollos.llm.transport import Provider


class ComposedLLMAdapter(LLMAdapter):
    """Combine a Provider with a PromptTemplate to satisfy LLMAdapter.

    The template formats (system, user, prefill) into a single prompt string;
    the provider sends that string to its backend and streams the response.
    """

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
    ) -> AsyncIterator[StreamChunk]:
        prompt = self._template.render(system=system, user=user, prefill=prefill)
        async for chunk in self._provider.stream(
            prompt=prompt, stop=stop, max_tokens=max_tokens
        ):
            yield chunk
