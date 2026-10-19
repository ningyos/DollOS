"""FakeModel — a scripted ``ModelAdapter`` for tests.

Each call to ``stream()`` plays one scripted reply. A reply is either a list
of model events, or old-style raw text that is converted to events: the text
before ``</think>`` becomes one ``ThinkDelta``; every
``<tool_call>{"name": ..., "arguments": {...}}</tool_call>`` becomes a
``ToolCall`` (ids generated) sent as one ``ToolCalls`` after the stream; the
rest is one ``SpeechDelta``. A reply may also be an exception, raised when
that call starts; an exception inside an event list is raised at that point
of the stream (a stream cut after some output). Once the replies run out,
every further call plays the empty reply (no speech, no tool calls), so a
turn's tool loop ends.

Every call is recorded as a JSON snapshot taken when the call starts — not a
reference to the caller's list, which would make any "prefix unchanged"
assertion pass even if earlier messages were edited later.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Sequence

from dollos.llm.model import (
    End,
    ModelAdapter,
    ModelEvent,
    SpeechDelta,
    ThinkDelta,
    ToolCall,
    ToolCalls,
    Usage,
)

Reply = str | Sequence[ModelEvent] | BaseException

_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)


def events_from_text(text: str, first_call_id: int = 0) -> list[ModelEvent]:
    """Old-style raw text → events (see the module docstring)."""
    think, sep, rest = text.partition("</think>")
    if not sep:
        think, rest = "", text
    calls: list[ToolCall] = []
    speech_parts: list[str] = []
    pos = 0
    for m in _TOOL_CALL_RE.finditer(rest):
        speech_parts.append(rest[pos:m.start()])
        obj = json.loads(m.group(1))
        calls.append(ToolCall(
            id=f"call_{first_call_id + len(calls)}",
            name=obj["name"],
            arguments=json.dumps(obj.get("arguments", {}), ensure_ascii=False),
        ))
        pos = m.end()
    speech_parts.append(rest[pos:])
    # The server keeps content separate from reasoning; the newlines old
    # replies put after </think> are not speech.
    speech = "".join(speech_parts).lstrip()
    events: list[ModelEvent] = []
    if think:
        events.append(ThinkDelta(think))
    if speech:
        events.append(SpeechDelta(speech))
    if calls:
        events.append(ToolCalls(tuple(calls)))
    return events


def _end_for(events: Sequence[ModelEvent], usage: Usage) -> End:
    think = "".join(e.text for e in events if isinstance(e, ThinkDelta))
    speech = "".join(e.text for e in events if isinstance(e, SpeechDelta))
    calls = [c for e in events if isinstance(e, ToolCalls) for c in e.calls]
    message: dict = {"role": "assistant", "content": speech}
    if think:
        message["reasoning_content"] = think
    if calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments}}
            for c in calls
        ]
    return End(
        finish_reason="tool_calls" if calls else "stop",
        usage=usage,
        assistant_message=message,
        ttft_ms=None,
    )


class FakeModel(ModelAdapter):
    """Scripted ``ModelAdapter``.

    ``replies`` play in call order. ``repeat_last`` replays the last reply
    instead of the empty one once they run out. ``usages`` gives a per-call
    ``(prompt_tokens, completion_tokens)`` (the last one repeats), reported in
    ``End.usage`` and, like the real adapter, via ``on_usage`` with
    ``prior_tokens`` subtracted from the prompt. ``before_call(idx)`` runs as each call
    starts; ``delay_s`` sleeps before each event (cancel tests).
    """

    def __init__(
        self,
        replies: Sequence[Reply] = (),
        *,
        repeat_last: bool = False,
        usages: Sequence[tuple[int | None, int | None]] | None = None,
        before_call: Callable[[int], None] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._replies = list(replies)
        self._repeat_last = repeat_last
        self._usages = list(usages) if usages is not None else None
        self._before_call = before_call
        self._delay_s = delay_s
        self.calls: list[dict] = []
        self.events_yielded = 0
        self._next_call_id = 0

    @property
    def pass_count(self) -> int:
        return len(self.calls)

    @property
    def last_user(self) -> str | None:
        """Content of the last ``user`` message of the most recent call."""
        if not self.calls:
            return None
        users = [m for m in self.calls[-1]["messages"] if m["role"] == "user"]
        return users[-1]["content"] if users else None

    def _reply(self, idx: int, messages: list[dict]) -> Reply:
        if idx < len(self._replies):
            return self._replies[idx]
        if self._repeat_last and self._replies:
            return self._replies[-1]
        return []

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
        idx = len(self.calls)
        self.calls.append({
            "messages": json.loads(json.dumps(messages)),
            "tools": json.loads(json.dumps(tools)),
            "max_tokens": max_tokens,
            "think_budget": think_budget,
            "purpose": purpose,
            "conversation": conversation,
            "prior_tokens": prior_tokens,
        })
        if self._before_call is not None:
            self._before_call(idx)
        reply = self._reply(idx, messages)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, str):
            events = events_from_text(reply, self._next_call_id)
        else:
            events = list(reply)
        self._next_call_id += sum(
            len(e.calls) for e in events if isinstance(e, ToolCalls)
        )
        usage = Usage()
        if self._usages:
            prompt, completion = self._usages[min(idx, len(self._usages) - 1)]
            usage = Usage(prompt_tokens=prompt, completion_tokens=completion)
            if on_usage is not None:
                on_usage(
                    max(prompt - prior_tokens, 0) if prompt is not None else None,
                    completion,
                )
        if not any(isinstance(e, End) for e in events):
            events.append(_end_for(events, usage))
        for ev in events:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            if isinstance(ev, BaseException):
                raise ev
            self.events_yielded += 1
            yield ev

    async def check(self) -> None:
        return None
