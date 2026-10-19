"""ModelAdapter — the one interface every model call goes through.

A call streams events: ``ThinkDelta`` / ``SpeechDelta`` as they arrive, then
at most one ``ToolCalls`` after the stream has finished, then exactly one
``End``. ``End.assistant_message`` is the message to send back verbatim on
the next pass, so the conversation stays append-only (server cache prefix).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ThinkDelta:
    text: str


@dataclass(frozen=True)
class SpeechDelta:
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, as the server sent it; never re-encoded


@dataclass(frozen=True)
class ToolCalls:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class Usage:
    """Server-reported numbers for one call; None = not reported.

    ``cache_n`` / ``prompt_n`` / ``prompt_ms`` are also None when the think
    cap fired: the server re-reads the prompt after inserting its cap line,
    and the timings describe that second read, not this prompt.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_n: int | None = None
    prompt_n: int | None = None
    prompt_ms: float | None = None
    predicted_per_second: float | None = None
    draft_n: int | None = None
    draft_n_accepted: int | None = None


@dataclass(frozen=True)
class End:
    finish_reason: str | None
    usage: Usage
    assistant_message: dict
    ttft_ms: float | None
    # Tool calls discarded because the reply hit max_tokens mid-call.
    dropped_tool_calls: int = 0
    think_capped: bool = False


ModelEvent = ThinkDelta | SpeechDelta | ToolCalls | End

ModelErrorKind = Literal["transport", "server"]


class ModelError(Exception):
    """A failed model call.

    ``transport``: unreachable, timeout, 5xx before the stream, or the stream
    cut before ``[DONE]`` — the brain is down. ``server``: 4xx before the
    stream (message carries the server's text) or an error inside the stream.
    """

    def __init__(self, kind: ModelErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: ModelErrorKind = kind


def tool_message(call_id: str, text: str) -> dict:
    """The ``tool`` message answering one call. The server template matches
    results to calls by order only; the id is kept for our own records."""
    return {"role": "tool", "tool_call_id": call_id, "content": text}


class ModelAdapter(ABC):
    @abstractmethod
    def stream(
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
        """Stream one call. ``max_tokens`` is the budget for speech and tool
        calls; ``think_budget`` is added on top and caps the thinking.

        ``conversation`` is ``(key, epoch)``, recorded only. ``prior_tokens``
        is the previous call's ``prompt_tokens + completion_tokens`` in the
        same conversation; ``on_usage(new_prompt_tokens, completion_tokens)``
        receives ``max(prompt_tokens - prior_tokens, 0)`` so the token
        accounting charges only what this call appended."""

    @abstractmethod
    async def check(self) -> None:
        """Boot check. Raises RuntimeError when the server is not usable."""
