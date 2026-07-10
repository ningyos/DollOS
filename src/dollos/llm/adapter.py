"""Abstract LLM adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass(frozen=True)
class StreamChunk:
    """A single streamed chunk from the LLM."""

    text: str
    done: bool = False


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
        tools: list[type[BaseModel]] | None = None,
        grammar: str | None = None,
        purpose: str = "cascade",
        on_usage: Callable[[int | None, int | None], None] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion. `tools` is forwarded to the template; transports
        ignore it (the prompt encodes tool definitions as text). `grammar` is
        a GBNF string forwarded to the provider's sampler when supported (None
        = unconstrained sampling). `on_usage` — invoked once per call in the
        transport `finally` with (prompt_tokens, completion_tokens); either
        may be None when the backend omits usage."""
        ...

    @abstractmethod
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
        """Stream a completion from a multi-message conversation history.

        Used by the dispatcher cascade to preserve the full
        user → assistant(think+tool_call) → user(<tool_response>) → assistant
        alternation within a single turn. Each `messages` entry is
        `{"role": "user"|"assistant", "content": str}`. The template always
        opens a fresh assistant `<think>` turn at the end regardless of the
        last message's role.

        Single-shot non-cascade callers keep using `stream_completion`.

        `on_usage` — invoked once per call in the transport `finally` with
        (prompt_tokens, completion_tokens); either may be None when the
        backend omits usage."""
        ...
