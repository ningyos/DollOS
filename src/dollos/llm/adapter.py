"""Abstract LLM adapter interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class StreamChunk:
    """A single streamed chunk from the LLM."""

    text: str
    """The new text added by this chunk."""

    done: bool = False
    """True iff this is the final chunk for the turn."""


class LLMAdapter(ABC):
    """Abstract interface for LLM backends.

    All concrete adapters MUST support prefill — assistant-side text that the
    model continues from. This is critical for VoM (see grammar_injection_techreport.md).
    """

    @abstractmethod
    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion.

        Args:
            system: system prompt
            user: user message
            prefill: assistant prefix tokens (already attributed to assistant role)
            stop: optional stop sequences
            max_tokens: hard cap on generated tokens

        Yields:
            StreamChunk objects until done=True is yielded.
        """
        ...
