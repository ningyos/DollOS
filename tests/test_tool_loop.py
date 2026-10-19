"""Tests for cascade.tool_loop shared dispatch + friendly error formatting."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
import respx
from pydantic import BaseModel, ValidationError

from dollos.agent_engine import run_agent
from dollos.cascade.tool_loop import dispatch_one, format_unknown_tool, format_validation_error, run_tool_cascade
from dollos.llm.model import End, SpeechDelta, ThinkDelta, Usage
from dollos.llm.tool_schema import tool_schemas
from dollos.tools import SUB_TOOLS, ReadToolOutput, Recall, SetFocus, Shell
from tests._fake_model import FakeModel


def test_format_unknown_tool_lists_available():
    msg = format_unknown_tool("Foo", {"Shell": Shell, "Recall": Recall})
    assert "Foo" in msg
    assert "Shell" in msg and "Recall" in msg


def test_format_validation_error_names_field_not_raw_wall():
    """限制超界(limit ge=1) → 友善訊息含工具名+欄位名+給定值，不含 pydantic 原始牆。"""
    try:
        ReadToolOutput.model_validate({"id": "x", "offset": 0, "limit": 0})
    except ValidationError as e:
        msg = format_validation_error(e, "ReadToolOutput")
    assert "ReadToolOutput" in msg
    assert "limit" in msg
    assert "0" in msg
    # 不是 pydantic 原始錯誤牆（不含 URL / 'validation error(s) for'）
    assert "https://" not in msg
    assert "validation error" not in msg.lower()


def test_tool_error_messages_are_english():
    """Plan 2026-10-19 Task 7: the daemon's own words in a tool result are
    English (Gura talks English; the prompt's framing follows)."""
    assert format_unknown_tool("Foo", {"Shell": Shell, "Recall": Recall}) == (
        "Unknown tool 'Foo'. Available tools: Recall, Shell")
    try:
        ReadToolOutput.model_validate({"id": "x", "offset": 0, "limit": 0})
    except ValidationError as e:
        msg = format_validation_error(e, "ReadToolOutput")
    assert msg.startswith("Tool ReadToolOutput got bad arguments: limit: ")
    assert msg.endswith(" (you gave 0)")


@pytest.mark.asyncio
async def test_dispatch_one_unknown_tool_friendly(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one("Nope", {}, ctx, {"Shell": Shell})
    assert r is not None and r.success is False
    assert "Nope" in r.detail and "Shell" in r.detail


@pytest.mark.asyncio
async def test_dispatch_one_validation_friendly(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    registry = {"ReadToolOutput": ReadToolOutput}
    r = await dispatch_one(
        "ReadToolOutput", {"id": "x", "offset": 0, "limit": 0}, ctx, registry
    )
    assert r is not None and r.success is False
    assert "limit" in r.detail
    assert "validation error" not in r.detail.lower()


@pytest.mark.asyncio
async def test_dispatch_one_success_returns_detail(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one(
        "SetFocus", {"text": "writing the plan"}, ctx, {"SetFocus": SetFocus}
    )
    assert r is not None and r.success is True
    assert "writing the plan" in r.detail
    assert ctx.mind_state.focus == "writing the plan"


@pytest.mark.asyncio
async def test_dispatch_one_runtime_error_returns_failed_result(tmp_path):
    from tests._dispatcher_helpers import _make_mind_ctx
    from pydantic import BaseModel

    class _Boom(BaseModel):
        async def run(self, ctx):
            raise RuntimeError("kaboom")

    ctx = _make_mind_ctx(tmp_path)
    r = await dispatch_one("Boom", {}, ctx, {"Boom": _Boom})
    assert r is not None and r.success is False
    assert "runtime error" in r.detail and "kaboom" in r.detail


# ---------------------------------------------------------------------------
# run_tool_calls: one reply's native tool calls → results + tool messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tool_calls_in_order_with_one_tool_message_each():
    from dollos.cascade.tool_loop import ToolResult, run_tool_calls
    from dollos.llm.model import ToolCall

    seen: list[tuple[str, dict]] = []

    async def _dispatch(name, args):
        seen.append((name, args))
        if name == "Quiet":
            return None
        if name == "Bad":
            return ToolResult(tool_name=name, success=False, detail="it broke")
        return ToolResult(tool_name=name, success=True, detail=f"did {args['x']}")

    calls = [
        ToolCall(id="a", name="Do", arguments='{"x": 1}'),
        ToolCall(id="b", name="Quiet", arguments="{}"),
        ToolCall(id="c", name="Bad", arguments="{}"),
    ]
    outcomes = await run_tool_calls(calls, _dispatch)

    assert seen == [("Do", {"x": 1}), ("Quiet", {}), ("Bad", {})]
    assert [o.message for o in outcomes] == [
        {"role": "tool", "tool_call_id": "a", "content": "did 1"},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
        {"role": "tool", "tool_call_id": "c", "content": "it broke"},
    ]
    assert [o.result.status for o in outcomes] == ["ok", "ok", "failed"]
    assert [o.call.id for o in outcomes] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_run_tool_calls_bad_arguments_fail_without_running():
    from dollos.cascade.tool_loop import run_tool_calls
    from dollos.llm.model import ToolCall

    async def _dispatch(name, args):  # pragma: no cover - must not run
        raise AssertionError("dispatched despite bad arguments")

    outcomes = await run_tool_calls(
        [ToolCall(id="a", name="Do", arguments='{"x": '),
         ToolCall(id="b", name="Do", arguments='["x"]')],
        _dispatch,
    )
    assert [o.result.success for o in outcomes] == [False, False]
    assert "not valid JSON" in outcomes[0].message["content"]
    assert "JSON object" in outcomes[1].message["content"]


def test_unbound_result_status():
    from dollos.cascade.tool_loop import ToolResult

    r = ToolResult(tool_name="Shell", success=False, detail="no", unbound=True)
    assert r.status == "rejected_unbound"


# ---------------------------------------------------------------------------
# run_tool_cascade on the ModelAdapter
# ---------------------------------------------------------------------------


def _call(name: str, **arguments) -> str:
    """One old-style tool call; FakeModel turns it into a native ToolCall."""
    return "<tool_call>" + json.dumps({"name": name, "arguments": arguments}) + "</tool_call>"


class _Quiet(BaseModel):
    """Side-effect tool: run() returns None."""

    async def run(self, ctx) -> None:
        return None


async def _cascade(model, tmp_path, *, tools=(Shell, Recall), sink=None, **kw):
    from tests._dispatcher_helpers import _make_mind_ctx

    tools = list(tools)
    messages = [{"role": "user", "content": "go"}]
    await run_tool_cascade(
        model=model,
        system="SYS",
        messages=messages,
        tools=tools,
        tools_by_name={cls.__name__: cls for cls in tools},
        ctx=_make_mind_ctx(tmp_path),
        sink=sink,
        max_tokens=512,
        think_budget=64,
        purpose=kw.pop("purpose", "workflow"),
        **kw,
    )
    return messages


@pytest.mark.asyncio
async def test_consecutive_fails_alternating_tools_trigger_abort(tmp_path):
    """I3 regression: alternating-tool failures across iterations accumulate per-tool
    and still trigger the 3-strike stuck-tool abort.

    Old bug: consecutive_fails dict replaced wholesale on new-tool failure, so
    fail(Shell), fail(Recall), fail(Shell), ... never reached count 3 for either.
    Fix: per-tool counts preserved; Shell reaches 3 after iterations 1, 3, 5.
    """
    from dollos.ipc.messages import ErrorMsg

    # iter 1: Shell fails (Shell:1)
    # iter 2: Recall fails (Shell:1, Recall:1)
    # iter 3: Shell fails (Shell:2, Recall:1)
    # iter 4: Recall fails (Shell:2, Recall:2)
    # iter 5: Shell fails (Shell:3) → abort fires; iter 6 must NOT be reached
    model = FakeModel([
        _call("Shell"), _call("Recall"), _call("Shell"), _call("Recall"),
        _call("Shell"),
        _call("Shell"),  # iter 6 — must never be consumed
    ])
    sink: asyncio.Queue = asyncio.Queue()

    await _cascade(model, tmp_path, sink=sink)

    # Cascade must abort after exactly 5 LLM calls (Shell reaches count 3).
    assert model.pass_count == 5, (
        f"Expected 5 LLM calls (abort at Shell:3), got {model.pass_count}"
    )

    # An ErrorMsg naming Shell must be in the sink.
    error_msgs = []
    while not sink.empty():
        msg = sink.get_nowait()
        if isinstance(msg, ErrorMsg):
            error_msgs.append(msg)
    assert error_msgs, "No ErrorMsg was emitted on stuck-tool abort"
    assert any("Shell" in m.message for m in error_msgs), (
        f"ErrorMsg did not name Shell: {[m.message for m in error_msgs]}"
    )


@pytest.mark.asyncio
async def test_cascade_request_shape(tmp_path):
    """System is the first message; tools go as sorted schemas; budget and
    purpose are forwarded as given."""
    model = FakeModel(["</think>done"])
    await _cascade(model, tmp_path, purpose="consolidation")

    [call] = model.calls
    assert call["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "go"},
    ]
    assert call["tools"] == tool_schemas([Shell, Recall])
    assert call["max_tokens"] == 512
    assert call["think_budget"] == 64
    assert call["purpose"] == "consolidation"


@pytest.mark.asyncio
async def test_cascade_sends_results_back_append_only(tmp_path):
    """Pass 2 = pass 1's messages + the assistant message verbatim (with its
    reasoning) + one ``tool`` message per call, in call order."""
    model = FakeModel([
        "weighing it</think>" + _call("_Quiet") + _call("Shell"),
        "</think>all done",
    ])
    messages = await _cascade(model, tmp_path, tools=[_Quiet, Shell])

    first, second = model.calls
    assert second["messages"][:len(first["messages"])] == first["messages"]
    assistant, quiet, shell = second["messages"][len(first["messages"]):]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "weighing it"
    ids = [tc["id"] for tc in assistant["tool_calls"]]
    assert quiet == {"role": "tool", "tool_call_id": ids[0], "content": "ok"}
    assert shell["role"] == "tool" and shell["tool_call_id"] == ids[1]
    assert "Shell" in shell["content"]  # validation error text goes back
    # The caller's list holds the whole conversation, final reply included.
    assert messages[-1] == {"role": "assistant", "content": "all done"}


@pytest.mark.asyncio
async def test_cascade_continues_after_side_effect_only_pass(tmp_path):
    """A pass whose only call returns None still gets a next pass."""
    model = FakeModel([_call("_Quiet"), "</think>after"])
    await _cascade(model, tmp_path, tools=[_Quiet])
    assert model.pass_count == 2


@pytest.mark.asyncio
async def test_cascade_ends_on_a_pass_without_tool_calls(tmp_path):
    model = FakeModel(["</think>just text", _call("_Quiet")])
    await _cascade(model, tmp_path, tools=[_Quiet])
    assert model.pass_count == 1


@pytest.mark.asyncio
async def test_cascade_stops_at_max_passes(tmp_path, caplog):
    """An agent that keeps calling tools and never Reports stops at 16."""
    model = FakeModel([_call("_Quiet")], repeat_last=True)
    with caplog.at_level(logging.WARNING, logger="dollos.cascade.tool_loop"):
        await _cascade(model, tmp_path, tools=[_Quiet])
    assert model.pass_count == 16
    assert any("16" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cascade_length_ends_with_warning(tmp_path, caplog):
    """A reply cut by max_tokens has no tool calls left (the adapter dropped
    them): the cascade logs and ends."""
    cut = End(
        finish_reason="length", usage=Usage(),
        assistant_message={"role": "assistant", "content": ""},
        ttft_ms=None, dropped_tool_calls=1,
    )
    model = FakeModel([[ThinkDelta("long"), cut], _call("_Quiet")])
    with caplog.at_level(logging.WARNING, logger="dollos.cascade.tool_loop"):
        await _cascade(model, tmp_path, tools=[_Quiet])
    assert model.pass_count == 1
    assert any("max_tokens" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cascade_prior_tokens_is_previous_pass_total(tmp_path):
    model = FakeModel(
        [_call("_Quiet"), _call("_Quiet"), "</think>done"],
        usages=[(100, 20), (150, 30), (200, 5)],
    )
    await _cascade(model, tmp_path, tools=[_Quiet])
    assert [c["prior_tokens"] for c in model.calls] == [0, 120, 180]


@pytest.mark.asyncio
async def test_cascade_on_iter_end_gets_reasoning_and_speech(tmp_path):
    seen: list[tuple] = []

    def _on_iter_end(iter_num, reasoning, speech, tool_calls, results, duration_ms):
        seen.append((iter_num, reasoning, speech, tool_calls, [r.status for r in results]))

    model = FakeModel(["hmm</think>say" + _call("_Quiet"), "</think>end"])
    await _cascade(model, tmp_path, tools=[_Quiet], on_iter_end=_on_iter_end)

    assert seen[0][:3] == (1, "hmm", "say")
    assert [tc["name"] for tc in seen[0][3]] == ["_Quiet"]
    assert seen[0][3][0]["arguments"] == "{}"
    assert seen[0][4] == ["ok"]
    assert seen[1][:3] == (2, "", "end")


# ---------------------------------------------------------------------------
# run_agent
# ---------------------------------------------------------------------------


async def _agent(model, tmp_path, **kw):
    from dollos.prompts import PromptRenderer
    from dollos.tool_outputs import ToolOutputStore
    from tests._dispatcher_helpers import _FakeMemSearch

    memsearch = kw.pop("memsearch", _FakeMemSearch())
    report = await run_agent(
        task="do the thing",
        system="SYS",
        model=model,
        renderer=PromptRenderer(),
        memory_root=tmp_path / "memory",
        memsearch=memsearch,
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        think_budget=64,
        purpose=kw.pop("purpose", "workflow"),
        **kw,
    )
    return report, memsearch


@pytest.mark.asyncio
async def test_agent_report_with_another_tool_ends_after_that_pass(tmp_path):
    """Report plus another call in one reply: both run, then the agent ends."""
    model = FakeModel([
        _call("Report", status="ok", summary="s", details="d")
        + _call("NoteMemory", text="remember this"),
        _call("Shell", command="echo never"),
    ])
    report, memsearch = await _agent(model, tmp_path)
    assert report == {"status": "ok", "summary": "s", "details": "d"}
    assert model.pass_count == 1
    assert memsearch.indexed, "the call after Report did not run"


@pytest.mark.asyncio
async def test_agent_offers_its_tools_as_schemas(tmp_path):
    model = FakeModel([_call("Report", status="ok", summary="s", details="d")])
    await _agent(model, tmp_path, purpose="evolution")
    assert model.calls[0]["tools"] == tool_schemas(SUB_TOOLS)
    assert model.calls[0]["purpose"] == "evolution"


_SSE = {"content-type": "text/event-stream"}


def _sse_reply(call_id: str, name: str, arguments: dict, prompt: int, completion: int) -> bytes:
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}}]},
        {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}],
         "usage": {"prompt_tokens": prompt, "completion_tokens": completion}},
    ]
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_agent_calls_are_recorded_with_purpose_and_new_tokens(tmp_path):
    """Through the real adapter: each LLMCallRecord carries the agent's
    purpose, and new_prompt_tokens counts only what the pass appended."""
    from dollos.llm.chat_completions import ChatCompletionsAdapter
    from dollos.telemetry.llm_calls import TelemetryRecorder

    recorder = TelemetryRecorder(tmp_path / "telemetry")
    model = ChatCompletionsAdapter(
        base_url="http://strata.test:8080", model="m", timeout_s=5.0, recorder=recorder,
    )
    replies = iter([
        _sse_reply("c1", "SetFocus", {"text": "x"}, prompt=100, completion=20),
        _sse_reply("c2", "Report", {"status": "ok", "summary": "s", "details": "d"},
                   prompt=150, completion=10),
    ])
    with respx.mock:
        route = respx.post("http://strata.test:8080/v1/chat/completions").mock(
            side_effect=lambda request: httpx.Response(200, headers=_SSE, content=next(replies))
        )
        report, _ = await _agent(model, tmp_path, purpose="consolidation")

    assert report == {"status": "ok", "summary": "s", "details": "d"}
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)
    assert second["messages"][0] == {"role": "system", "content": "SYS"}
    assert second["messages"][2]["tool_calls"][0]["id"] == "c1"
    assert second["messages"][3]["role"] == "tool"
    r1, r2 = recorder.read_today()
    assert (r1.call_purpose, r2.call_purpose) == ("consolidation", "consolidation")
    assert (r1.new_prompt_tokens, r2.new_prompt_tokens) == (100, 30)
