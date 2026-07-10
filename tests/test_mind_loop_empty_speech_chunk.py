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
"""
from __future__ import annotations

import asyncio

import pytest

from dollos.ipc.channel_registry import ChannelRegistry
from dollos.ipc.messages import AddressedText, TextChunk
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS
from tests._dispatcher_helpers import _make_mind_ctx


class _Chunk:
    def __init__(self, text, done):
        self.text = text
        self.done = done


class _ChunkedLLM:
    """Streams `segments` as SEPARATE chunks — the last one carries
    `done=True`. Mirrors real llama.cpp token-by-token streaming closely
    enough to reproduce the bug: unlike `tests/test_mind_loop.py`'s
    `_FakeLLM` / `tests/test_mind_loop_origin.py`'s `_SeqLLM` (both yield the
    WHOLE reply as one chunk, which lets `ToolStreamParser` swallow the
    think-close boilerplate newlines before they ever reach a SpeakChunk),
    this one splits the think-close marker from its trailing newlines and
    the reply into three separate `feed()` calls — reproducing the leak.
    """

    def __init__(self, segments: list[str]) -> None:
        self._segments = list(segments)

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade",
        on_usage=None,
    ):
        n = len(self._segments)
        for i, seg in enumerate(self._segments):
            yield _Chunk(text=seg, done=(i == n - 1))

    async def stream_messages(
        self, system, messages, max_tokens=1024, grammar=None,
        purpose="cascade", stop=None, tools=None, on_usage=None,
    ):
        # Terminal pass: close think, emit nothing, no tool -> cascade breaks.
        yield _Chunk(text="TOOL: none\n</think>\n\n", done=True)


def _drain_queue(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# The think header ends EXACTLY at "</think>" with no trailing newline in the
# same chunk — the boilerplate "\n\n" and the reply arrive in their OWN
# subsequent chunks, exactly like the production incident.
_THINK_SEGMENTS = [
    "SEEN: owner said hi\n"
    "INTENT: greet\n"
    "TOOL: none\n"
    "REVIEW: ok\n"
    "MOOD: warm\n"
    "</think>",
    "\n\n",
    "主人好。",
]


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

    llm = _ChunkedLLM(_THINK_SEGMENTS)

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
