"""Tests for EventDispatcher — miscellaneous.

Covers: ToolResult dataclass, transcript writes, diary event, recall/identity
injection, cascade decision log, time awareness / [Now] block, recent-activity
time formatting.
"""

import asyncio
import logging
from datetime import date
from pathlib import Path

import pytest

from dollos.conversation_history import ConversationHistory
from dollos.scratchpad import Scratchpad
from dollos.dispatcher import EventDispatcher, ToolResult
from dollos.tool_outputs import ToolOutputStore
from dollos.events import ShellResultEvent, UserTextEvent
from dollos.ipc.messages import TextChunk, TurnEnd
from dollos.llm.adapter import StreamChunk
from dollos.prompts import PromptRenderer

from tests._dispatcher_helpers import (
    _FakeAdapter,
    _FakeCascadeLogger,
    _FakeMemSearch,
    _doll_identity,
    _drain,
    _make_dispatcher,
    _think_with_mood,
)


# ----- ToolResult dataclass -----


def test_tool_result_dataclass_fields():
    r = ToolResult(tool_name="X", success=False, detail="boom")
    assert r.tool_name == "X"
    assert r.success is False
    assert r.detail == "boom"


def test_tool_result_success_field_defaults_to_required():
    """success must be explicit (no default) — caller intent should be visible."""
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(ToolResult)}
    assert "success" in fields
    assert fields["success"].default is dataclasses.MISSING


# ----- Transcript / diary -----


@pytest.mark.asyncio
async def test_dispatcher_writes_user_text_transcript_after_turn(tmp_path: Path):
    """User text is written to transcript in finally, after the turn completes."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    disp = EventDispatcher(
        adapter=adapter,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=transcripts_root,
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    expected = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert "主人說：hi" in content
    assert "我說：ok" in content


@pytest.mark.asyncio
async def test_dispatcher_handles_diary_event(tmp_path: Path):
    """DiaryEvent flows through perceive/respond pipeline; perception
    tells Doll to write diary; daily.md ends up with diary section."""

    captured_user_message: list[str] = []

    class _CaptureAdapter:
        def __init__(self):
            self.calls = []

        async def stream_messages(self, **kw):
            self.calls.append({**kw, "messages": list(kw["messages"])} if "messages" in kw else kw)
            captured_user_message.append(kw["messages"][0]["content"])
            yield StreamChunk(
                text=(
                    '<tool_call>{"name":"WriteDiary","arguments":'
                    '{"content":"today felt good"}}</tool_call>'
                ),
                done=False,
            )
            yield StreamChunk(text="", done=True)

    from dollos.events import DiaryEvent
    adapter = _CaptureAdapter()
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    disp = EventDispatcher(
        adapter=adapter,
        renderer=PromptRenderer(), identity=_doll_identity("x"),
        memory_root=tmp_path, memsearch=ms,
        transcripts_root=transcripts_root,
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(DiaryEvent(response_sink=sink))
    while True:
        m = await sink.get()
        if m is None:
            break

    # The perception told Doll to write a diary
    assert "日記" in captured_user_message[0]
    # WriteDiary tool was actually called → daily file has diary section
    daily_file = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert daily_file.exists()
    assert "## 日記 (" in daily_file.read_text()


# ----- Cascade decision log -----


@pytest.mark.asyncio
async def test_dispatcher_logs_cascade_iter_per_iter(tmp_path: Path):
    """Cascade with one failing tool then a successful Say should log_iter twice."""
    adapter = _FakeAdapter(
        chunks=[
            # Iter 1: emit a tool that doesn't exist -> failure -> cascade
            StreamChunk(
                text=(
                    "<think>\nSEEN: hi\nINTENT: try\nREVIEW: -\n"
                    "MOOD: ok\nTOOL: Bogus\n</think>\n"
                    '<tool_call>{"name":"Bogus","arguments":{}}</tool_call>'
                ),
                done=True,
            ),
        ]
    )
    # Second adapter response (after cascade) — must drain via re-call:
    # _FakeAdapter replays the same `chunks` each call, so the second call
    # also produces the bogus tool. Cascade aborts on consecutive 3 fails;
    # we just need >=2 iters logged. Run until natural termination.
    fcl = _FakeCascadeLogger()
    disp = EventDispatcher(
        adapter=adapter,
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=fcl,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    # At least 2 iters logged (cascade ran more than once).
    assert len(fcl.iters) >= 2
    # All iters share the same turn_id.
    turn_ids = {row["turn_id"] for row in fcl.iters}
    assert len(turn_ids) == 1
    # Iter numbers increment.
    assert [row["iter"] for row in fcl.iters[:2]] == [1, 2]


@pytest.mark.asyncio
async def test_dispatcher_log_iter_includes_parsed_think_and_tool_calls(tmp_path: Path):
    """log_iter receives assistant_text + tool_calls + results from the iter."""
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text=(
                    "<think>\nSEEN: greet\nINTENT: reply\nREVIEW: -\n"
                    "MOOD: 開心\nTOOL: Say\n</think>\n"
                    '<tool_call>{"name":"Say","arguments":{"text":"hi"}}</tool_call>'
                ),
                done=True,
            ),
        ]
    )
    fcl = _FakeCascadeLogger()
    disp = EventDispatcher(
        adapter=adapter,
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=fcl,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=ConversationHistory(),
    )
    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert len(fcl.iters) >= 1
    first = fcl.iters[0]
    assert first["turn_id"].startswith("fake-turn-")
    assert first["iter"] == 1
    assert "SEEN: greet" in first["assistant_text"]
    assert first["tool_calls"] == [
        {"name": "Say", "arguments": {"text": "hi"}}
    ]
    # Say returns None -> no ToolResult; results list is empty.
    assert first["results"] == []
    assert isinstance(first["duration_ms"], int)


# ----- Time awareness -----


@pytest.mark.asyncio
async def test_dispatcher_injects_now_block_in_first_user_message(tmp_path: Path):
    """First user message starts with [Now]\\n + ISO date + 週X 早上/上午/下午/晚上/深夜."""
    import re as _re

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    disp = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    # [Scratchpad] is now the very first block (Task 3)
    assert user.startswith("[Scratchpad]\n")
    # [Now] must also be present
    assert "[Now]\n" in user
    # Date YYYY-MM-DD HH:MM:SS pattern
    assert _re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", user)
    # Chinese weekday + period descriptor
    assert _re.search(r"週[一二三四五六日]", user)
    assert any(p in user for p in ("深夜", "早上", "上午", "下午", "晚上"))
    # Order: [Scratchpad] before [Now] before [Memory context] before [Message]
    assert user.index("[Scratchpad]") < user.index("[Now]") < user.index("[Memory context]") < user.index("[Message]")


@pytest.mark.asyncio
async def test_dispatcher_recent_activity_renders_with_seconds(tmp_path: Path):
    """[Recent activity] entries from today render with HH:MM:SS prefix."""
    from datetime import datetime as _dt
    import re as _re

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    disp = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)
    today = _dt.now().replace(hour=14, minute=15, second=30, microsecond=0)
    disp._rolling = [(today, "主人查 pwd")]

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Recent activity]" in user
    # Today's entry: HH:MM:SS prefix only (no date).
    assert _re.search(r"- 14:15:30 主人查 pwd", user)


@pytest.mark.asyncio
async def test_dispatcher_recent_activity_uses_full_date_for_old_entries(
    tmp_path: Path,
):
    """Yesterday's rolling entry renders with YYYY-MM-DD HH:MM:SS prefix."""
    from datetime import datetime as _dt, timedelta as _td

    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
                done=False,
            ),
            StreamChunk(text="", done=True),
        ]
    )
    disp = _make_dispatcher(adapter=adapter, tmp_path=tmp_path)
    yesterday = _dt.now().replace(
        hour=10, minute=5, second=0, microsecond=0
    ) - _td(days=1)
    disp._rolling = [(yesterday, "old chat")]

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Recent activity]" in user
    expected_prefix = f"- {yesterday:%Y-%m-%d %H:%M:%S} old chat"
    assert expected_prefix in user


# ----- ShellResultEvent perception rendering -----


@pytest.mark.asyncio
async def test_shell_result_perception_includes_paging_hints(tmp_path: Path) -> None:
    """ShellResultEvent perception text includes output_id, total lines, and ReadToolOutput hint."""
    disp = _make_dispatcher(
        adapter=_FakeAdapter(chunks=[]),
        tmp_path=tmp_path,
    )
    sink: asyncio.Queue = asyncio.Queue()
    evt = ShellResultEvent(
        command="ls -la",
        status="ok",
        exit_code=0,
        output="line1\nline2\nline3",
        output_id="abc123",
        line_count=42,
        response_sink=sink,
    )
    doll_event = await disp._perceive(evt)
    p = doll_event.perception
    assert "output_id: abc123" in p
    assert "total lines: 42" in p
    assert "ReadToolOutput" in p
