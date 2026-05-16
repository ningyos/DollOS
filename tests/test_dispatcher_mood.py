"""Tests for EventDispatcher — mood state (step 19).

Covers: MOOD: parsing from think block, mood file writes, mood block injection,
mood block position, mood update across turns.
"""

import asyncio
from pathlib import Path

import pytest

from dollos.scratchpad import Scratchpad
from dollos.dispatcher import EventDispatcher
from dollos.tool_outputs import ToolOutputStore
from dollos.events import UserTextEvent
from dollos.ipc.messages import TextChunk
from dollos.llm.adapter import StreamChunk
from dollos.prompts import PromptRenderer

from tests._dispatcher_helpers import (
    _FakeAdapter,
    _FakeCascadeLogger,
    _FakeInstinct,
    _FakeInnerVoice,
    _FakeMemSearch,
    _doll_identity,
    _drain,
    _make_dispatcher,
    _think_with_mood,
)


@pytest.mark.asyncio
async def test_dispatcher_initial_mood_is_default(tmp_path: Path):
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    assert disp._current_mood == "平靜，剛醒來"


@pytest.mark.asyncio
async def test_dispatcher_injects_mood_block(tmp_path: Path):
    """First user message includes [Mood]\\n<current mood> block."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Mood]\n平靜，剛醒來" in user


@pytest.mark.asyncio
async def test_dispatcher_mood_block_position(tmp_path: Path):
    """[Now] < [Mood] < [Recent activity] < [Memory context] < [Message]."""
    from datetime import datetime as _dt
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice("- foo")
    disp = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    today = _dt.now().replace(hour=12, minute=0, second=0, microsecond=0)
    disp._rolling = [(today, "did stuff")]

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    i_now = user.index("[Now]")
    i_mood = user.index("[Mood]")
    i_ra = user.index("[Recent activity]")
    i_mc = user.index("[Memory context]")
    i_msg = user.index("[Message]")
    assert i_now < i_mood < i_ra < i_mc < i_msg


@pytest.mark.asyncio
async def test_dispatcher_parses_mood_from_last_assistant_message(tmp_path: Path):
    """Big model writes MOOD: in <think>; dispatcher parses it from the
    last assistant message after cascade exits."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text=_think_with_mood("新心情"), done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert disp._current_mood == "新心情"


@pytest.mark.asyncio
async def test_dispatcher_no_mood_update_when_assistant_lacks_mood_line(
    tmp_path: Path,
):
    """If last assistant message has no MOOD: line, current mood unchanged."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert disp._current_mood == "平靜，剛醒來"
    assert not (tmp_path / "mood").exists() or not list((tmp_path / "mood").iterdir())


@pytest.mark.asyncio
async def test_dispatcher_appends_mood_to_file_when_parsed(tmp_path: Path):
    import re as _re
    from datetime import date as _date
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text=_think_with_mood("新心情"), done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    mood_file = tmp_path / "mood" / f"{_date.today():%Y-%m-%d}.md"
    assert mood_file.exists()
    body = mood_file.read_text()
    assert _re.search(r"## \(\d{2}:\d{2}:\d{2}\) 新心情", body)


@pytest.mark.asyncio
async def test_dispatcher_indexes_mood_file_with_memsearch(tmp_path: Path):
    from datetime import date as _date
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text=_think_with_mood("新心情"), done=False),
            StreamChunk(text="", done=True),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    mood_file = tmp_path / "mood" / f"{_date.today():%Y-%m-%d}.md"
    assert mood_file in ms.indexed


@pytest.mark.asyncio
async def test_dispatcher_mood_block_uses_updated_mood_in_subsequent_turn(
    tmp_path: Path,
):
    class _ScriptedAdapter:
        def __init__(self, emits: list[str]):
            self.calls: list[dict] = []
            self._emits = list(emits)

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])})
            emit = self._emits.pop(0) if self._emits else ""
            yield StreamChunk(text=emit, done=False)
            yield StreamChunk(text="", done=True)

    adapter = _ScriptedAdapter([
        _think_with_mood("第一輪心情"),
        _think_with_mood("第二輪心情"),
    ])
    iv = _FakeInnerVoice()
    inst = _FakeInstinct()
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter, inner_voice=iv, instinct=inst,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
    )

    for text in ("first", "second"):
        sink: asyncio.Queue = asyncio.Queue()
        disp.dispatch(UserTextEvent(text=text, response_sink=sink))
        await _drain(sink)

    second_user = adapter.calls[1]["messages"][0]["content"]
    assert "[Mood]\n第一輪心情" in second_user
