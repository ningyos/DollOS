"""Tests for dispatcher conversation-history integration.

Covers:
- History messages are prepended between system and new user message on turn 2.
- Cascade-end snapshot is stored in history after a turn completes.
"""

import asyncio
from pathlib import Path

import pytest

from dollos.conversation_history import ConversationHistory
from dollos.dispatcher import EventDispatcher
from dollos.events import UserTextEvent
from dollos.llm.adapter import StreamChunk
from dollos.scratchpad import Scratchpad
from dollos.tool_outputs import ToolOutputStore

from tests._dispatcher_helpers import (
    _FakeCascadeLogger,
    _FakeMemSearch,
    _doll_identity,
    _drain,
)


def _make_dispatcher_with_history(
    *,
    adapter,
    history: ConversationHistory,
    tmp_path: Path,
):
    from dollos.prompts import PromptRenderer

    return EventDispatcher(
        adapter=adapter,
        renderer=PromptRenderer(),
        identity=_doll_identity(),
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        scratchpad=Scratchpad(),
        conversation_history=history,
    )


# ---------------------------------------------------------------------------
# Test 1: history messages are prepended on turn 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_prepends_history_messages_on_turn_2(tmp_path: Path):
    """On turn 2, recent history messages appear between the system prompt
    and the new user perception message in the messages list sent to LLM."""
    from dollos.llm.adapter import LLMAdapter

    captured: list[list[dict]] = []

    class _CapturingAdapter(LLMAdapter):
        async def stream_messages(self, *, system, messages, **kw):
            captured.append(list(messages))
            yield StreamChunk(text="", done=True)

        async def stream_completion(self, **kw):  # type: ignore[override]
            yield StreamChunk(text="", done=True)

    history = ConversationHistory(max_turns=5)
    history.add_turn([
        {"role": "user", "content": "turn 1 user message"},
        {"role": "assistant", "content": "turn 1 doll response"},
    ])

    dispatcher = _make_dispatcher_with_history(
        adapter=_CapturingAdapter(),
        history=history,
        tmp_path=tmp_path,
    )

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="turn 2 message", response_sink=sink))
    await _drain(sink)

    assert len(captured) >= 1, "LLM was not called"
    first_call_messages = captured[0]

    # Order: [history user, history assistant, new user]
    assert len(first_call_messages) >= 3, (
        f"Expected at least 3 messages (2 history + 1 new), got {len(first_call_messages)}: "
        f"{[m['role'] for m in first_call_messages]}"
    )
    assert first_call_messages[0]["content"] == "turn 1 user message"
    assert first_call_messages[0]["role"] == "user"
    assert first_call_messages[1]["content"] == "turn 1 doll response"
    assert first_call_messages[1]["role"] == "assistant"
    # Last message is the new turn 2 user perception
    last = first_call_messages[-1]
    assert last["role"] == "user"
    assert "turn 2 message" in last["content"]


# ---------------------------------------------------------------------------
# Test 2: empty history — no extra messages prepended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_no_history_prepend_on_first_turn(tmp_path: Path):
    """On the very first turn (empty history), messages list has exactly
    one entry: the new user perception message."""
    from dollos.llm.adapter import LLMAdapter

    captured: list[list[dict]] = []

    class _CapturingAdapter(LLMAdapter):
        async def stream_messages(self, *, system, messages, **kw):
            captured.append(list(messages))
            yield StreamChunk(text="", done=True)

        async def stream_completion(self, **kw):  # type: ignore[override]
            yield StreamChunk(text="", done=True)

    history = ConversationHistory(max_turns=5)  # empty
    dispatcher = _make_dispatcher_with_history(
        adapter=_CapturingAdapter(),
        history=history,
        tmp_path=tmp_path,
    )

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="first ever message", response_sink=sink))
    await _drain(sink)

    assert len(captured) >= 1
    first_call_messages = captured[0]
    # Only one message: the new user perception
    assert len(first_call_messages) == 1
    assert first_call_messages[0]["role"] == "user"
    assert "first ever message" in first_call_messages[0]["content"]


# ---------------------------------------------------------------------------
# Test 3: cascade-end snapshot stored in history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_stores_turn_in_history_on_cascade_end(tmp_path: Path):
    """After a cascade completes, history.turn_count() == 1 and the stored
    messages include both a user message and an assistant message."""
    from dollos.llm.adapter import LLMAdapter

    # Respond with a simple Say tool call so there's an assistant message
    say_chunk = StreamChunk(
        text='<tool_call>{"name":"Say","arguments":{"text":"hello"}}</tool_call>',
        done=False,
    )
    done_chunk = StreamChunk(text="", done=True)

    class _SimpleAdapter(LLMAdapter):
        async def stream_messages(self, **kw):
            yield say_chunk
            yield done_chunk

        async def stream_completion(self, **kw):  # type: ignore[override]
            yield done_chunk

    history = ConversationHistory(max_turns=5)
    dispatcher = _make_dispatcher_with_history(
        adapter=_SimpleAdapter(),
        history=history,
        tmp_path=tmp_path,
    )

    sink: asyncio.Queue = asyncio.Queue()
    dispatcher.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)

    assert history.turn_count() == 1, (
        f"Expected 1 turn stored, got {history.turn_count()}"
    )
    msgs = history.recent_messages()
    assert msgs[0]["role"] == "user", f"First stored message should be user, got {msgs[0]['role']}"
    assert any(m["role"] == "assistant" for m in msgs), (
        "Expected at least one assistant message in stored turn"
    )


# ---------------------------------------------------------------------------
# Test 4: second dispatch accumulates turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_accumulates_turns_across_two_dispatches(tmp_path: Path):
    """Running two sequential UserTextEvents results in history.turn_count() == 2."""
    from dollos.llm.adapter import LLMAdapter

    done_chunk = StreamChunk(text="", done=True)

    class _SimpleAdapter(LLMAdapter):
        async def stream_messages(self, **kw):
            yield done_chunk

        async def stream_completion(self, **kw):  # type: ignore[override]
            yield done_chunk

    history = ConversationHistory(max_turns=5)
    dispatcher = _make_dispatcher_with_history(
        adapter=_SimpleAdapter(),
        history=history,
        tmp_path=tmp_path,
    )

    for text in ("first", "second"):
        sink: asyncio.Queue = asyncio.Queue()
        dispatcher.dispatch(UserTextEvent(text=text, response_sink=sink))
        await _drain(sink)

    assert history.turn_count() == 2, (
        f"Expected 2 turns after two dispatches, got {history.turn_count()}"
    )
