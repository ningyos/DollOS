"""P1 Task 3(a): _run_one_turn finally emits TurnEndAddressed for external
origins, but the global None (→ TurnEnd) sentinel for internal origins."""
from __future__ import annotations

import asyncio

import pytest

from dollos.ipc.channel_registry import ChannelRegistry
from dollos.ipc.messages import AddressedText, TurnEndAddressed
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS
from tests._dispatcher_helpers import _make_mind_ctx


class _Chunk:
    def __init__(self, text, done):
        self.text = text
        self.done = done


class _SeqLLM:
    def __init__(self, returns: list[str]):
        self._returns = list(returns)
        self._i = 0

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade"
    ):
        text = self._returns[self._i]
        self._i += 1
        yield _Chunk(text=text, done=True)

    async def stream_messages(
        self, system, messages, max_tokens=1024, grammar=None,
        purpose="cascade", stop=None, tools=None,
    ):
        yield _Chunk(text="TOOL: none\n</think>\n\n", done=True)


def _speech_stream(seen: str, reply: str) -> str:
    return (
        f"SEEN: {seen}\nINTENT: greet\nTOOL: none\nREVIEW: ok\nMOOD: warm\n"
        "</think>\n\n"
        f"{reply}"
    )


def _drain(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


@pytest.mark.asyncio
async def test_external_turn_emits_turn_end_addressed(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    channel_id = "mcp:conn1:call1"
    queue.put(Perception(
        kind="ChannelMessage", t=1.0,
        data={"channel_id": channel_id, "author": "Claude",
              "author_is_owner": False, "is_dm": True, "channel_kind": "mcp",
              "content": "hi"},
    ))

    ctx = _make_mind_ctx(tmp_path, state=state)
    sink: asyncio.Queue = asyncio.Queue()
    # _make_mind_ctx does NOT wire a channel_registry (ctx.channel_registry
    # defaults to None — mind_ctx.py:76), so build one and assign it, exactly
    # like test_mind_loop_origin.py:198-200. Register in BOTH the registry (so
    # locus_of(origin) == "external") and the sink_resolver (so
    # sink_resolver(origin) routes here).
    registry = ChannelRegistry()
    registry.register(channel_id, locus="external", kind="mcp")
    ctx.channel_registry = registry
    ctx.sink_resolver.register(sink, locus="external", channel_id=channel_id)

    loop = MindLoop(
        state=state, queue=queue, ctx=ctx,
        llm=_SeqLLM([_speech_stream("Claude said hi", "hello Claude")]),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry={cls.__name__: cls for cls in MAIN_TOOLS},
    )
    await loop.iterate()

    items = _drain(sink)
    # last item for an external turn must be the addressed turn-end, NOT None
    assert any(isinstance(i, TurnEndAddressed) and i.channel_id == channel_id
               for i in items), items
    assert None not in items
    assert any(isinstance(i, AddressedText) for i in items)


@pytest.mark.asyncio
async def test_internal_turn_emits_global_turn_end_none(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))

    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, sink=sink, state=state)

    loop = MindLoop(
        state=state, queue=queue, ctx=ctx,
        llm=_SeqLLM([_speech_stream("owner said hi", "hey")]),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry={cls.__name__: cls for cls in MAIN_TOOLS},
    )
    await loop.iterate()

    items = _drain(sink)
    assert None in items, items          # internal turn: global None sentinel
    assert not any(isinstance(i, TurnEndAddressed) for i in items)


# --- F2: mcp origin → external_public tier (spec §G Task 3 / §F2) -----------
# The tier derivation is driven SOLELY by author_is_owner (+ is_dm), so a
# kind="mcp" ChannelMessage lands on external_public exactly like the already-
# tested discord external path. We still assert it here for the mcp payload so
# the spec-required mcp-level invariant is not merely implied. The downstream
# EXTERNAL_TOOLS narrowing (mind_loop.py:836-843: ChannelMessage ∈
# _EXTERNAL_KINDS + external_public ⇒ registry hard-narrowed, Shell/Workflow/
# Monitor structurally unreachable) is shared, tier-driven code already
# covered by the existing external-path tests — a peer connecting can never
# widen the toolset because the narrowing keys on this tier, not on kind.
def test_mcp_channel_message_is_external_public_tier():
    p = Perception(
        kind="ChannelMessage", t=1.0,
        data={"channel_id": "mcp:c1:call1", "author": "Claude",
              "author_is_owner": False, "is_dm": True, "channel_kind": "mcp",
              "content": "hi"},
    )
    assert MindLoop._derive_origin_tier([p]) == "external_public"


def test_mcp_peer_never_reaches_external_dm_even_if_dm():
    # is_dm=True but author_is_owner=False (a peer is NEVER the owner, §E) ⇒
    # must stay external_public, never external_dm (which would leak the
    # owner's full private-memory retrieval to an unverified AI).
    p = Perception(
        kind="ChannelMessage", t=1.0,
        data={"channel_id": "mcp:c1:call1", "author": "Claude",
              "author_is_owner": False, "is_dm": True, "channel_kind": "mcp",
              "content": "hi"},
    )
    assert MindLoop._derive_origin_tier([p]) != "external_dm"
