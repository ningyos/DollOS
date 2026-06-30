"""B1: episodic transcript recapture — live-loop integration tests."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._dispatcher_helpers import _make_mind_ctx, _FakeMemSearch
from tests.test_mind_loop import _FakeLLM
from dollos.tools import MAIN_TOOLS


def _today_transcript(ctx):
    return ctx.transcripts_root / f"{date.today():%Y-%m-%d}.md"


def _speak_only_stream(text: str) -> str:
    # voice_first wire: think block then spoken text, no tool call.
    return (
        "SEEN: x\nINTENT: y\nREVIEW: ok\nMOOD: warm\nTOOL: none\n"
        "</think>\n\n" + text
    )


def _make_loop(tmp_path, *, state, ctx, stream):
    return MindLoop(
        state=state,
        queue=_QUEUE_HOLDER.pop(),
        ctx=ctx,
        llm=_FakeLLM(stream),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry={cls.__name__: cls for cls in MAIN_TOOLS},
    )


# Tiny indirection so each test builds its own queue before _make_loop.
_QUEUE_HOLDER: list = []


@pytest.mark.asyncio
async def test_user_turn_written_to_transcript(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "你好嗎"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    _QUEUE_HOLDER.append(queue)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, stream=_speak_only_stream("好啊"))

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    assert "主人說：你好嗎" in content
    # transcript file was indexed
    assert _today_transcript(ctx) in [__import__("pathlib").Path(p) for p in ms.indexed]


@pytest.mark.asyncio
async def test_doll_turn_written_as_single_joined_line(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "嗨"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    _QUEUE_HOLDER.append(queue)
    # Two full sentences in the spoken segment.
    loop = _make_loop(
        tmp_path, state=state, ctx=ctx,
        stream=_speak_only_stream("第一句話。第二句話。"),
    )

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    doll_lines = [ln for ln in content.split("\n") if "我說：" in ln]
    # exactly ONE doll line (turn-level, not per-sentence)
    assert len(doll_lines) == 1
    assert "第一句話。" in doll_lines[0]
    assert "第二句話。" in doll_lines[0]
