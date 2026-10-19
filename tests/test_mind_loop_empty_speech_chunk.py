"""Regression: an owner Discord DM's raw output after `</think>` was
`"\\n\\n主人好。"` — the grammar's boilerplate closing newlines, arriving in a
SEPARATE stream chunk from `</think>` itself (token-by-token llama.cpp
streaming), leak past `ToolStreamParser`'s think-close newline-swallow (which
only eats newlines already sitting in its buffer at the instant `</think>` is
matched — see `src/dollos/tool_parser.py` `_feed_voice`'s `IN_THINK` branch)
and surface stuck to the front of the first real `SpeakChunk`.
`SentenceChunker.feed()` then splits that on the leading `\n` delimiter,
producing a whitespace-only "\n\n" chunk FIRST, ahead of the real reply.

Before the fix, `MindLoop._emit_sentence` (mind_loop.py ~1259) pushed BOTH
chunks to the sink unconditionally — the whitespace-only one first. Discord
rejected it with `400 code 50006: Cannot send an empty message`, which (Fix
B, in the bridge) tore down the whole daemon-WS + Discord-gateway connection,
dropping the real reply that followed. This test guards the daemon-side fix:
`_emit_sentence` must never emit a whitespace-only sentence, so the sink
receives exactly ONE AddressedText with the real reply text.

On the chat-completions path the same shape is a reply whose ``content``
starts with newlines in its own stream fragment, ahead of the real text.
"""
from __future__ import annotations

import asyncio

import pytest

from dollos.ipc.channel_registry import ChannelRegistry
from dollos.ipc.messages import AddressedText, TextChunk
from dollos.llm.model import SpeechDelta, ThinkDelta
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS
from tests._dispatcher_helpers import _make_mind_ctx
from tests._fake_model import FakeModel


class _ChunkedLLM(FakeModel):
    """Streams the reply as SEPARATE fragments, like the real server: the
    thinking, then a newline-only speech fragment, then the reply text — the
    newlines arrive on their own, ahead of the real reply."""

    def __init__(self, think: str, speech_fragments: list[str]) -> None:
        super().__init__([
            [ThinkDelta(think), *(SpeechDelta(f) for f in speech_fragments)]
        ])


def _drain_queue(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# The boilerplate "\n\n" and the reply arrive as their OWN speech fragments,
# exactly like the production incident.
_THINK = "owner said hi; greet back"
_SPEECH_FRAGMENTS = ["\n\n", "主人好。"]


@pytest.mark.asyncio
async def test_leading_newline_speech_chunk_never_reaches_sink_as_addressed_text(tmp_path):
    """External-origin turn (Discord DM shape): sink must receive exactly ONE
    AddressedText, with the real reply text — never a whitespace-only one."""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(
        kind="ChannelMessage", t=1.0, data={"channel_id": "owner-dm", "text": "hi"},
    ))

    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}

    ctx = _make_mind_ctx(tmp_path, state=state)
    sink: asyncio.Queue = asyncio.Queue()
    ctx.sink_resolver.register(sink, locus="external", channel_id="owner-dm")
    registry = ChannelRegistry()
    registry.register("owner-dm", locus="external", kind="discord")
    ctx.channel_registry = registry

    llm = _ChunkedLLM(_THINK, _SPEECH_FRAGMENTS)

    loop = MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry=tool_registry,
    )

    await loop.iterate()

    items = _drain_queue(sink)
    addressed = [c for c in items if isinstance(c, AddressedText)]

    assert not any(not a.text.strip() for a in addressed), (
        f"whitespace-only AddressedText leaked to sink: {addressed!r}"
    )
    assert len(addressed) == 1, f"expected exactly one AddressedText, got {addressed!r}"
    assert addressed[0].text == "主人好。"
    # No plain TextChunk should have leaked onto the external sink either.
    assert not any(isinstance(c, TextChunk) for c in items)
