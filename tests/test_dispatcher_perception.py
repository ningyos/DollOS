"""Tests for EventDispatcher — _perceive branches for various event types.

Covers: UserText recall injection, SubagentResult, Scheduled, DailyPlan,
ShellResult, MonitorTriggered, MonitorExited, and _format_active_monitors_block.
"""

import asyncio
from pathlib import Path

import pytest

from dollos.dispatcher import EventDispatcher
from dollos.tool_outputs import ToolOutputStore
from dollos.events import UserTextEvent
from dollos.ipc.messages import TextChunk, TurnEnd
from dollos.llm.adapter import StreamChunk

from tests._dispatcher_helpers import (
    _FakeAdapter,
    _FakeCascadeLogger,
    _FakeInstinct,
    _FakeInnerVoice,
    _FakeMemSearch,
    _doll_identity,
    _drain,
    _make_dispatcher,
)


# ----- UserText recall injection -----


@pytest.mark.asyncio
async def test_recall_result_wraps_user_message_with_memory_context(tmp_path: Path):
    """IV.recall result is wrapped in [Memory context] block prepended to
    user message (RAG context pattern, 2026-05-08). Prefill stays empty."""
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("- user likes coffee")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hello world", response_sink=sink))

    await _drain(sink)

    assert iv.calls == ["hello world"]
    assert len(adapter.calls) == 1
    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Memory context]" in user
    assert "- user likes coffee" in user
    assert "[Message]" in user
    assert "hello world" in user
    assert "RECALL:" not in user


@pytest.mark.asyncio
async def test_empty_recall_still_emits_memory_context_block(tmp_path: Path):
    """Empty IV.recall result still produces an explicit (no relevant memory)
    line in the [Memory context] block."""
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("")  # no hits
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hi", response_sink=sink))

    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Memory context]" in user
    assert "(no relevant memory)" in user
    assert "[Message]" in user
    assert "hi" in user


# ----- SubagentResultEvent perception -----


@pytest.mark.asyncio
async def test_dispatcher_handles_subagent_result_event(tmp_path: Path):
    """SubagentResultEvent flows through perceive/respond. The first user
    message body contains all four subagent fields (task / status /
    summary / details) so Doll can react to them."""
    from dollos.events import SubagentResultEvent

    captured_user_message: list[str] = []

    class _CaptureAdapter:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_messages(self, **kw):
            self.calls.append(
                {**kw, "messages": list(kw["messages"])}
                if "messages" in kw
                else kw
            )
            captured_user_message.append(kw["messages"][0]["content"])
            yield StreamChunk(
                text='<tool_call>{"name":"Say","arguments":{"text":"ack"}}</tool_call>',
                done=False,
            )
            yield StreamChunk(text="", done=True)

    adapter = _CaptureAdapter()
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=__import__("dollos.prompts", fromlist=["PromptRenderer"]).PromptRenderer(),
        identity=_doll_identity("x"),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(
        SubagentResultEvent(
            subagent_id="abc1",
            task="search transcripts for coffee",
            status="ok",
            summary="found 3 hits",
            details="a.md, b.md, c.md",
            response_sink=sink,
        )
    )
    items = await _drain(sink)

    # The perception (which becomes [Message] body) lists all four fields.
    perception_body = captured_user_message[0]
    assert "你派出的 subagent 回來了" in perception_body
    assert "search transcripts for coffee" in perception_body
    assert "ok" in perception_body
    assert "found 3 hits" in perception_body
    assert "a.md, b.md, c.md" in perception_body
    # Doll responded with Say "ack" (we wired the adapter that way).
    assert any(isinstance(m, TextChunk) and m.text == "ack" for m in items)
    assert any(isinstance(m, TurnEnd) for m in items)


@pytest.mark.asyncio
async def test_subagent_result_event_uses_event_response_sink(tmp_path: Path):
    """_sink_of(SubagentResultEvent) returns event.response_sink."""
    from dollos.events import SubagentResultEvent

    sink: asyncio.Queue = asyncio.Queue()
    ev = SubagentResultEvent(
        subagent_id="x",
        task="t",
        status="ok",
        summary="s",
        details="d",
        response_sink=sink,
    )
    assert EventDispatcher._sink_of(ev) is sink


# ----- ScheduledEvent / DailyPlanEvent perception -----


@pytest.mark.asyncio
async def test_dispatcher_perceives_scheduled_event(tmp_path: Path):
    from datetime import time as _time

    from dollos.events import ScheduledEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(ScheduledEvent(
        entry_time=_time(7, 30),
        intent="morning ping",
        response_sink=sink,
    ))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "07:30:00" in user
    assert "morning ping" in user


@pytest.mark.asyncio
async def test_dispatcher_perceives_daily_plan_event(tmp_path: Path):
    from dollos.events import DailyPlanEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice("")
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(DailyPlanEvent(response_sink=sink))
    await _drain(sink)

    user = adapter.calls[0]["messages"][0]["content"]
    assert "WriteSchedule" in user


# ----- ShellResultEvent / MonitorTriggeredEvent / MonitorExitedEvent -----


@pytest.mark.asyncio
async def test_dispatcher_perceives_shell_result_event(tmp_path: Path):
    from dollos.events import ShellResultEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    sink: asyncio.Queue = asyncio.Queue()
    ev = ShellResultEvent(
        command="ls /tmp",
        status="ok",
        exit_code=0,
        output="a\nb\n",
        response_sink=sink,
    )
    doll_event = await dispatcher._perceive(ev)
    p = doll_event.perception
    assert "shell 命令" in p or "Shell 命令" in p
    assert "ls /tmp" in p
    assert "exit 0" in p


@pytest.mark.asyncio
async def test_dispatcher_perceives_monitor_triggered(tmp_path: Path):
    from dollos.events import MonitorTriggeredEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    sink: asyncio.Queue = asyncio.Queue()
    ev = MonitorTriggeredEvent(
        monitor_id="mon-1",
        command="nvidia-smi -l 5",
        line="91",
        suppressed_count=4,
        response_sink=sink,
    )
    doll_event = await dispatcher._perceive(ev)
    p = doll_event.perception
    assert "mon-1" in p
    assert "nvidia-smi -l 5" in p
    assert "91" in p
    assert "suppressed" in p.lower() or "壓" in p
    assert "4" in p


@pytest.mark.asyncio
async def test_dispatcher_perceives_monitor_exited(tmp_path: Path):
    from dollos.events import MonitorExitedEvent

    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    iv = _FakeInnerVoice()
    dispatcher = _make_dispatcher(adapter=adapter, inner_voice=iv, tmp_path=tmp_path)
    sink: asyncio.Queue = asyncio.Queue()
    ev = MonitorExitedEvent(
        monitor_id="mon-2",
        command="tail -F /var/log/syslog",
        status="removed",
        exit_code=None,
        total_matched=17,
        response_sink=sink,
    )
    doll_event = await dispatcher._perceive(ev)
    p = doll_event.perception
    assert "mon-2" in p
    assert "removed" in p or "已停止" in p or "kill" in p.lower()
    assert "17" in p


# ----- _format_active_monitors_block -----


def test_format_active_monitors_block():
    """Pure formatting check — given a snapshot, render the block."""
    from dollos.dispatcher import _format_active_monitors_block

    snap = [
        {
            "monitor_id": "mon-1",
            "command": "nvidia-smi -l 5",
            "match_regex": r"^[89][0-9]$",
            "rate_limit_s": 60,
            "suppressed_in_window": 11,
        },
        {
            "monitor_id": "mon-2",
            "command": "tail -F /var/log/syslog",
            "match_regex": None,
            "rate_limit_s": 0,
            "suppressed_in_window": 0,
        },
    ]
    block = _format_active_monitors_block(snap)
    assert "[Active monitors]" in block
    assert "mon-1" in block and "nvidia-smi" in block
    assert "11" in block
    assert "mon-2" in block
    assert _format_active_monitors_block([]) == ""
