"""Tests for cascade.tool_loop shared dispatch + friendly error formatting."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from dollos.cascade.tool_loop import dispatch_one, format_unknown_tool, format_validation_error, run_tool_cascade
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.tools import ReadToolOutput, Recall, SetFocus, Shell


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
# I3 regression: consecutive_fails per-tool accumulation
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedAdapter(LLMAdapter):
    """Minimal scripted adapter for cascade-level tests."""

    scripts: list[list[StreamChunk]] = field(default_factory=list)
    call_count: int = field(default=0, init=False)

    async def stream_completion(self, **_) -> AsyncIterator[StreamChunk]:  # pragma: no cover
        yield StreamChunk(text="", done=True)

    async def stream_messages(self, **_) -> AsyncIterator[StreamChunk]:
        self.call_count += 1
        chunks = self.scripts.pop(0) if self.scripts else [StreamChunk(text="", done=True)]
        for c in chunks:
            yield c


def _bad_call(tool_name: str) -> list[StreamChunk]:
    """Two-chunk sequence emitting one invalid tool call (missing required args)."""
    return [
        StreamChunk(
            text=f'<tool_call>{{"name":"{tool_name}","arguments":{{}}}}</tool_call>',
            done=False,
        ),
        StreamChunk(text="", done=True),
    ]


@pytest.mark.asyncio
async def test_consecutive_fails_alternating_tools_trigger_abort(tmp_path):
    """I3 regression: alternating-tool failures across iterations accumulate per-tool
    and still trigger the 3-strike stuck-tool abort.

    Old bug: consecutive_fails dict replaced wholesale on new-tool failure, so
    fail(Shell), fail(Recall), fail(Shell), ... never reached count 3 for either.
    Fix: per-tool counts preserved; Shell reaches 3 after iterations 1, 3, 5.
    """
    from dollos.ipc.messages import ErrorMsg
    from tests._dispatcher_helpers import _make_mind_ctx

    # iter 1: Shell fails (Shell:1)
    # iter 2: Recall fails (Shell:1, Recall:1)
    # iter 3: Shell fails (Shell:2, Recall:1)
    # iter 4: Recall fails (Shell:2, Recall:2)
    # iter 5: Shell fails (Shell:3) → abort fires; iter 6 must NOT be reached
    adapter = _ScriptedAdapter(scripts=[
        _bad_call("Shell"),
        _bad_call("Recall"),
        _bad_call("Shell"),
        _bad_call("Recall"),
        _bad_call("Shell"),
        _bad_call("Shell"),  # iter 6 — must never be consumed
    ])

    ctx = _make_mind_ctx(tmp_path)
    sink: asyncio.Queue = asyncio.Queue()

    await run_tool_cascade(
        adapter=adapter,
        system="test",
        messages=[{"role": "user", "content": "go"}],
        tools=[Shell, Recall],
        tools_by_name={"Shell": Shell, "Recall": Recall},
        ctx=ctx,
        grammar="",
        sink=sink,
        max_tokens=512,
    )

    # Cascade must abort after exactly 5 LLM calls (Shell reaches count 3).
    assert adapter.call_count == 5, (
        f"Expected 5 LLM calls (abort at Shell:3), got {adapter.call_count}"
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
