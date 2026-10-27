"""MindLoop on the ModelAdapter (plan 2026-10-18 Task 4).

Each turn sends ``[system, user]`` plus native tool schemas; a pass with tool
calls runs them in order, sends the assistant message back verbatim plus one
``tool`` message per call, and runs again; a pass without tool calls ends the
turn. The model always sees the tier's fixed tool list; what this turn may
actually run is enforced when a call executes.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from dollos.ipc.messages import TextChunk
from dollos.llm.model import End, SpeechDelta, ThinkDelta, ToolCall, ToolCalls, Usage
from dollos.mind.mind_loop import MAX_SYNC_REFEED_PASSES
from dollos.mind.mind_state import Perception
from dollos.harness.permissions import tools_for
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop

# The external_public tool list (was tools.EXTERNAL_TOOLS).
EXTERNAL_TOOLS = tools_for("external_public")


def _p(kind: str, **data) -> Perception:
    return Perception(kind=kind, t=time.time(), data=data)


def _calls(*specs: tuple[str, str, dict | str]) -> ToolCalls:
    return ToolCalls(tuple(
        ToolCall(id=cid, name=name,
                 arguments=args if isinstance(args, str) else json.dumps(args))
        for cid, name, args in specs
    ))


def _drain(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _spoken(items: list) -> str:
    return "".join(c.text for c in items if isinstance(c, TextChunk))


def _tool_messages(call: dict) -> list[dict]:
    return [m for m in call["messages"] if m["role"] == "tool"]


@pytest.mark.asyncio
async def test_messages_are_system_then_user_with_offered_tools(tmp_path):
    model = FakeModel([[SpeechDelta("hi")]])
    ml = make_mindloop(memory_root=tmp_path, llm=model, system_prompt="You are Doll.")
    await ml._run_one_turn([_p("UserSpoke", text="hello")])

    call = model.calls[0]
    # system, the page snapshot, the turn's message (plan Task 9).
    assert [m["role"] for m in call["messages"]] == ["system", "user", "user"]
    assert call["messages"][0]["content"] == ml._system_for_turn()
    assert call["messages"][0]["content"].startswith(ml._system_prompt_for_turn())
    # The system prompt and [Memory guideline] live only in the system message.
    assert "[Memory guideline]" in call["messages"][0]["content"]
    for m in call["messages"][1:]:
        assert "You are Doll." not in m["content"]
        assert "[Memory guideline]" not in m["content"]
    names = [t["function"]["name"] for t in call["tools"]]
    assert names == sorted(names) and "Shell" in names
    assert call["purpose"] == "cascade"
    assert call["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_think_budget_comes_from_config(tmp_path):
    model = FakeModel([[SpeechDelta("hi")]])
    ml = make_mindloop(memory_root=tmp_path, llm=model, think_budget_tokens=333)
    await ml._run_one_turn([_p("UserSpoke", text="hello")])
    assert model.calls[0]["think_budget"] == 333


@pytest.mark.asyncio
async def test_speech_reaches_sink_before_any_tool_runs(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    model = FakeModel([[
        SpeechDelta("Okay, noted."),
        _calls(("c1", "SetFocus", {"text": "breakfast"})),
    ]])
    ml = make_mindloop(memory_root=tmp_path, llm=model, sink=sink)
    seen_at_dispatch: list[int] = []
    orig = ml._dispatch_tool

    async def _spy(name, args, registry=None):
        seen_at_dispatch.append(sink.qsize())
        return await orig(name, args, registry)

    ml._dispatch_tool = _spy
    await ml._run_one_turn([_p("UserSpoke", text="remember breakfast")])

    # The chunker is flushed before tools run: the unterminated last
    # sentence is already on the sink when SetFocus executes.
    assert seen_at_dispatch == [1]
    items = _drain(sink)
    assert isinstance(items[0], TextChunk) and items[0].text == "Okay, noted."


@pytest.mark.asyncio
async def test_two_calls_run_in_order_with_matching_tool_messages(tmp_path):
    model = FakeModel([[
        ThinkDelta("two things"),
        _calls(("call_a", "SetFocus", {"text": "first"}),
               ("call_b", "SetFocus", {"text": "second"})),
    ]])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="go")])

    assert ml._state.focus == "second"
    assert model.pass_count == 2
    tool_msgs = _tool_messages(model.calls[1])
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_a", "call_b"]
    assert "first" in tool_msgs[0]["content"]
    assert "second" in tool_msgs[1]["content"]
    assistant = model.calls[1]["messages"][3]
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_a", "call_b"]


@pytest.mark.asyncio
async def test_second_pass_extends_first_pass_byte_for_byte(tmp_path):
    model = FakeModel([[
        ThinkDelta("let me check"),
        SpeechDelta("One sec."),
        _calls(("c1", "Recall", {"query": "coffee"})),
    ]])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="coffee?")])

    first, second = model.calls[0]["messages"], model.calls[1]["messages"]
    assert json.dumps(second[:len(first)]) == json.dumps(first)
    assistant = second[len(first)]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "let me check"
    assert assistant["content"] == "One sec."
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"query": "coffee"}'
    assert second[len(first) + 1]["role"] == "tool"
    assert model.calls[1]["tools"] == model.calls[0]["tools"]


@pytest.mark.asyncio
async def test_internal_tool_json_identical_across_turn_kinds(tmp_path):
    model = FakeModel()
    ml = make_mindloop(memory_root=tmp_path, llm=model,
                       self_profile_enabled=True, evolution_enabled=True)
    batches = [
        [_p("Awoke")],                                 # normal
        [_p("UserSpoke", text="hi")],                  # owner (adds LearnName)
        [_p("ReflectionMoment", iters_since_last=3)],  # reflection
        [_p("DiaryMoment")],                           # diary
        [_p("AgendaMoment")],                          # agenda
        [_p("PulseMoment", severity="warn", detail="hot")],  # pulse
    ]
    for batch in batches:
        await ml._run_one_turn(batch)
    ml._state.safe_mode = True
    ml._state.safe_mode_reason = "test"
    await ml._run_one_turn([_p("Awoke")])               # safe mode

    dumps = {json.dumps(c["tools"]) for c in model.calls}
    assert len(model.calls) == 7
    assert len(dumps) == 1
    names = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert {"Shell", "NoteToolLesson", "LearnName", "PinSelf", "SelfRevision"} <= names


@pytest.mark.asyncio
async def test_external_turns_offer_no_internal_tools(tmp_path):
    """A public turn is offered EXTERNAL_TOOLS only; the owner's DM is
    offered the local owner's list (owner DM = local owner, 2026-10-27)."""
    model = FakeModel()
    ml = make_mindloop(memory_root=tmp_path, llm=model,
                       self_profile_enabled=True, evolution_enabled=True)
    await ml._run_one_turn([_p("ChannelMessage", channel_id="c1", author="x",
                               author_is_owner=False, content="hi")])
    await ml._run_one_turn([_p("ChannelMessage", channel_id="dm", author="me",
                               author_is_owner=True, is_dm=True, content="hi")])
    await ml._run_one_turn([_p("UserSpoke", text="hi")])

    public = {t["function"]["name"] for t in model.calls[0]["tools"]}
    owner_dm = {t["function"]["name"] for t in model.calls[1]["tools"]}
    local_owner = {t["function"]["name"] for t in model.calls[2]["tools"]}
    assert public == set(EXTERNAL_TOOLS)
    assert owner_dm == local_owner


@pytest.mark.asyncio
async def test_offered_but_unbound_call_is_rejected_not_failed(tmp_path):
    traces = tmp_path / "traces"
    from dollos.mind.trace import TraceWriter

    model = FakeModel([
        [_calls(("c1", "PinSelf", {"section": "self", "op": "add",
                                   "target": "", "text": "x"}))],
        [SpeechDelta("never mind")],
    ])
    ml = make_mindloop(memory_root=tmp_path, llm=model, self_profile_enabled=True,
                       trace_writer=TraceWriter(traces))
    await ml._run_one_turn([_p("Awoke")])   # normal turn: PinSelf is offered, not bound

    msg = _tool_messages(model.calls[1])[0]["content"]
    assert "PinSelf is not available in this turn" in msg
    bound = sorted(ml._active_tool_registry())
    assert ", ".join(bound) in msg
    # Not a failure: no tool_stats failure, no safe mode.
    assert ml._state.tool_stats.get("PinSelf", {}).get("fail", 0) == 0
    assert not ml._state.recent_tool_failures
    assert ml._state.safe_mode is False
    assert not (tmp_path / "self_profile.md").exists()
    env = json.loads(next(traces.glob("*.jsonl")).read_text().splitlines()[0])
    result = env["passes"][0]["results"][0]
    assert result["status"] == "rejected_unbound"
    assert result["tool_call_id"] == "c1"


@pytest.mark.asyncio
async def test_same_unbound_tool_rejected_three_times_ends_turn(tmp_path):
    shell = _calls(("c", "Shell", {"command": "ls"}))
    model = FakeModel([[shell]], repeat_last=True)
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("AgendaMoment")])   # agenda turn: Shell offered, not bound

    assert model.pass_count == 3
    assert ml._state.safe_mode is False
    assert ml._ctx.shell_runner.calls == []


@pytest.mark.asyncio
async def test_unknown_tool_name_is_rejected_and_counts_as_failure(tmp_path):
    model = FakeModel([[_calls(("c1", "Frobnicate", {}))]])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="hi")])

    msg = _tool_messages(model.calls[1])[0]["content"]
    assert "Frobnicate" in msg
    assert ml._state.tool_stats["Frobnicate"]["fail"] == 1


@pytest.mark.asyncio
async def test_external_turn_in_safe_mode_cannot_run_a_tool_it_is_not_offered(tmp_path):
    """Safe mode binds its read-only set on any tier, but a stranger's turn is
    offered only EXTERNAL_TOOLS: ReadToolOutput (not offered there) must not
    run — it is an unknown tool, a failure — and the reply names no internal
    tool."""
    store_calls = []
    model = FakeModel([[_calls(("c1", "ReadToolOutput",
                                {"id": "out-1", "offset": 0, "limit": 10}))]])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    ml._ctx.tool_output_store.read = lambda *a, **k: store_calls.append(a) or "secret"
    ml._state.safe_mode = True
    ml._state.safe_mode_reason = "test"
    await ml._run_one_turn([_p("ChannelMessage", channel_id="c1", author="x",
                               author_is_owner=False, content="read out-1")])

    assert {t["function"]["name"] for t in model.calls[0]["tools"]} == set(EXTERNAL_TOOLS)
    msg = _tool_messages(model.calls[1])[0]["content"]
    assert "ReadToolOutput" in msg and "secret" not in msg
    assert "GrepToolOutput" not in msg
    assert store_calls == []
    assert ml._state.tool_stats["ReadToolOutput"]["fail"] == 1


@pytest.mark.asyncio
async def test_invalid_json_arguments_are_a_tool_failure(tmp_path):
    model = FakeModel([[_calls(("c1", "SetFocus", '{"text": "x"'))]])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="hi")])

    msg = _tool_messages(model.calls[1])[0]
    assert msg["tool_call_id"] == "c1"
    assert "not valid JSON" in msg["content"]
    assert ml._state.focus == "idle"


@pytest.mark.asyncio
async def test_shell_receipt_goes_back_and_turn_does_not_wait(tmp_path):
    model = FakeModel([
        [SpeechDelta("Running it."), _calls(("c1", "Shell", {"command": "echo hi"}))],
        [SpeechDelta("Started.")],
    ])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="run echo")])

    assert model.pass_count == 2
    assert len(ml._ctx.shell_runner.calls) == 1
    receipt = _tool_messages(model.calls[1])[0]["content"]
    assert "shell dispatched" in receipt


@pytest.mark.asyncio
async def test_mood_only_first_pass_still_speaks_on_second(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    model = FakeModel([
        [_calls(("c1", "MoodTool", {"emotion": "happy"}))],
        [SpeechDelta("Hi there!")],
    ])
    ml = make_mindloop(memory_root=tmp_path, llm=model, sink=sink)
    await ml._run_one_turn([_p("UserSpoke", text="hello")])

    assert model.pass_count == 2
    assert ml._state.mood.emotion == "happy"
    assert "Hi there!" in _spoken(_drain(sink))


@pytest.mark.asyncio
async def test_length_finish_runs_nothing_and_ends_turn(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    end = End(finish_reason="length", usage=Usage(),
              assistant_message={"role": "assistant", "content": "Let me"},
              ttft_ms=None, dropped_tool_calls=1)
    model = FakeModel([[SpeechDelta("Let me"), end]])
    ml = make_mindloop(memory_root=tmp_path, llm=model, sink=sink)
    await ml._run_one_turn([_p("UserSpoke", text="hi")])

    assert model.pass_count == 1
    assert "Let me" in _spoken(_drain(sink))   # what was said counts


@pytest.mark.asyncio
async def test_turn_is_capped_at_max_passes(tmp_path):
    model = FakeModel([[_calls(("c", "SetFocus", {"text": "again"}))]], repeat_last=True)
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="hi")])
    assert model.pass_count == MAX_SYNC_REFEED_PASSES == 8


@pytest.mark.asyncio
async def test_system_message_identical_across_turns(tmp_path):
    model = FakeModel()
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="one")])
    await ml._run_one_turn([_p("AgendaMoment")])
    assert model.calls[0]["messages"][0] == model.calls[1]["messages"][0]


@pytest.mark.asyncio
async def test_think_chars_come_from_think_deltas(tmp_path):
    model = FakeModel([
        [ThinkDelta("abc"), ThinkDelta("de"), _calls(("c", "SetFocus", {"text": "x"}))],
        [ThinkDelta("fghij"), SpeechDelta("ok")],
    ])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    await ml._run_one_turn([_p("UserSpoke", text="hi")])
    assert ml._turn_think_chars == 10
