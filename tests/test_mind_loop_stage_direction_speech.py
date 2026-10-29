"""A reply that is only a bracketed stage direction — "(nothing to say)",
"(silence)" — is not speech: it must not reach the sink, the turn's speech
buffer (transcript / trace) or recent_outputs (v1 tool-use acceptance
2026-10-28 發現 7: 3.7f rep 3 sent "(nothing to say)" out). A sentence that
merely contains parentheses is untouched."""
from __future__ import annotations

import asyncio

import pytest

from dollos.ipc.channel_registry import ChannelRegistry
from dollos.ipc.messages import AddressedText
from dollos.llm.model import SpeechDelta, ThinkDelta
from dollos.mind.mind_loop import MindLoop, is_stage_direction
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS
from tests._dispatcher_helpers import _make_mind_ctx
from tests._fake_model import FakeModel


@pytest.mark.parametrize("text", [
    "(nothing to say)",
    "(Nothing to do yet — I'll let the morning sit quiet.)",
    "  (silence)\n",
    "[no reply]",
    "（沉默）",
    "【不說話】",
    "(nothing to say).",
    "(沉默)。",
    "(silence)!",
    "（沉默）。 ",
    "[no reply]…",
    "(thinking)?!",
])
def test_stage_direction_detected(text):
    assert is_stage_direction(text)


@pytest.mark.parametrize("text", [
    "Got it (I'll ping you at 9:27).",
    "(laughs) Okay, fine.",
    "Sure, 3 (three) of them.",
    "(a) first, (b) second",
    "Fine (for now).",
    "(laughs) Okay.",
    "(a). (b).",
    ".(silence)",
    "()",
    "",
    "主人好。",
])
def test_normal_sentence_not_stage_direction(text):
    assert not is_stage_direction(text)


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _run(tmp_path, fragments):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(
        kind="ChannelMessage", t=1.0, data={"channel_id": "owner-dm", "text": "hi"},
    ))
    ctx = _make_mind_ctx(tmp_path, state=state)
    sink: asyncio.Queue = asyncio.Queue()
    ctx.sink_resolver.register(sink, locus="external", channel_id="owner-dm")
    registry = ChannelRegistry()
    registry.register("owner-dm", locus="external", kind="discord")
    ctx.channel_registry = registry
    llm = FakeModel([[ThinkDelta("t"), *(SpeechDelta(f) for f in fragments)]])
    loop = MindLoop(
        state=state, queue=queue, ctx=ctx, llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry={cls.__name__: cls for cls in MAIN_TOOLS},
    )
    await loop.iterate()
    return loop, state, _drain(sink)


@pytest.mark.asyncio
async def test_stage_direction_reply_is_not_spoken_or_recorded(tmp_path):
    loop, state, items = await _run(tmp_path, ["(nothing to say)"])
    assert not [c for c in items if isinstance(c, AddressedText)]
    assert loop._turn_speech == []
    assert not [o for o in state.recent_outputs if o.kind == "Speech"]


@pytest.mark.asyncio
async def test_stage_direction_dropped_real_sentence_kept(tmp_path):
    loop, state, items = await _run(tmp_path, ["(sighs)\n", "Fine (for now)."])
    texts = [c.text for c in items if isinstance(c, AddressedText)]
    assert texts == ["Fine (for now)."]
    assert loop._turn_speech == ["Fine (for now)."]
