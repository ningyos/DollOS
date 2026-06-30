"""B1: episodic transcript recapture — live-loop integration tests."""
from __future__ import annotations

import asyncio
import logging
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


def _make_loop(tmp_path, *, state, ctx, queue, stream):
    return MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_FakeLLM(stream),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry={cls.__name__: cls for cls in MAIN_TOOLS},
    )


@pytest.mark.asyncio
async def test_user_turn_written_to_transcript(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "你好嗎"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, queue=queue, stream=_speak_only_stream("好啊"))

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    assert "主人說：你好嗎" in content
    # transcript file was indexed
    assert _today_transcript(ctx) in ms.indexed


@pytest.mark.asyncio
async def test_doll_turn_written_as_single_joined_line(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "嗨"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    # Two full sentences in the spoken segment.
    loop = _make_loop(
        tmp_path, state=state, ctx=ctx, queue=queue,
        stream=_speak_only_stream("第一句話。第二句話。"),
    )

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    doll_lines = [ln for ln in content.split("\n") if "我說：" in ln]
    # exactly ONE doll line (turn-level, not per-sentence)
    assert len(doll_lines) == 1
    assert "第一句話。" in doll_lines[0]
    assert "第二句話。" in doll_lines[0]


@pytest.mark.asyncio
async def test_system_turn_writes_no_user_line(tmp_path):
    """純系統 perception(非 UserSpoke)+ Doll 主動說話 → 只有 doll 行,無 user 行。"""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="ScheduledMoment", t=1.0, data={"text": "鬧鐘"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, queue=queue, stream=_speak_only_stream("早安"))

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    assert "主人說：" not in content      # no user line
    assert "我說：早安" in content        # doll line present


@pytest.mark.asyncio
async def test_user_and_doll_lines_paired_in_order(tmp_path):
    """一般對話 turn → user 行在先、doll 行在後。"""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "今天好嗎"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, queue=queue, stream=_speak_only_stream("很好喔"))

    await loop.iterate()

    lines = [ln for ln in _today_transcript(ctx).read_text().split("\n") if ln]
    assert "主人說：今天好嗎" in lines[0]
    assert "我說：很好喔" in lines[1]


@pytest.mark.asyncio
async def test_transcript_write_failure_does_not_crash_loop(tmp_path, caplog):
    """index_file 拋例外 → iterate() 不 crash,turn 仍正常完成,且有 ERROR log。"""
    class _RaisingMemSearch(_FakeMemSearch):
        async def index_file(self, path):
            raise RuntimeError("boom")

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "嗨"}))
    ms = _RaisingMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, queue=queue, stream=_speak_only_stream("好啊"))

    with caplog.at_level(logging.ERROR, logger="dollos.mind.mind_loop"):
        await loop.iterate()  # must NOT raise

    assert state.iter_count == 1  # turn completed despite transcript failure
    assert any("transcript write" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_user_multiline_text_written_as_single_physical_line(tmp_path):
    """UserSpoke text 含裸換行 → transcript 寫出單一物理行,無孤兒行。"""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "第一行\n第二行"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, queue=queue, stream=_speak_only_stream("好的"))

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    user_lines = [ln for ln in content.split("\n") if "主人說：" in ln]
    # exactly ONE physical line for the user bullet
    assert len(user_lines) == 1
    # both parts of the original text are present, joined by a space
    assert "第一行" in user_lines[0]
    assert "第二行" in user_lines[0]
    # no bare newline inside the bullet itself
    assert "\n" not in user_lines[0]


@pytest.mark.asyncio
async def test_no_transcript_when_system_turn_and_doll_silent(tmp_path):
    """純系統 turn + Doll 也沒說話 → 不寫任何 transcript 行 (spec §7)。"""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="ScheduledMoment", t=1.0, data={"text": "鬧鐘"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    # think-only stream: no SpeakChunk emitted after </think>
    silent_stream = "SEEN: x\nINTENT: y\nREVIEW: ok\nMOOD: warm\nTOOL: none\n</think>\n\n"
    loop = _make_loop(tmp_path, state=state, ctx=ctx, queue=queue, stream=silent_stream)

    await loop.iterate()

    assert not _today_transcript(ctx).exists()
